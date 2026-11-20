import os
import sqlite3
import subprocess
import time
import hashlib
import json
import threading
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List



def _find_root() -> Path:
    """Project root. Order: BUDDY_ROOT, then the nearest folder (here or up to 3 levels up) that holds one of
    the root-only files below, then a folder containing workspace/. Never just "the nearest workspace/":
    stray core/workspace folders are created by older code and used to fool this check."""
    env = os.environ.get("BUDDY_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent
    candidates = (here, *list(here.parents)[:3])
    markers = ("soul.md", "capabilities.md", "run_buddy.py", "api.py", ".env", "dashboard.html")
    for p in candidates:
        try:
            names = {x.name.lower() for x in p.iterdir()}
        except OSError:
            continue
        if any(m in names for m in markers):
            return p
    for p in candidates:
        if (p / "workspace").is_dir():
            return p
    return here


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = _find_root()
DB_PATH = ROOT_DIR / "knowledge_base" / "buddy_graph.db"
GROWTH_TRIGGER_THRESHOLD = 50   # counts user AND assistant rows, i.e. ~25 exchanges
GROWTH_RETRY_COOLDOWN = 600     # seconds to wait after a failed/missing growth run
GROWTH_TIMEOUT = 3600           # first full scan of a big workspace can be slow


class BuddyMemory:
    """
    Persistent memory and performance tracker for Buddy OS.
    Thread-safe logging, correct stats, and growth trigger.
    """

    def __init__(self, session_id: Optional[str] = None):
        self.session_id = session_id or self._generate_session_id()
        self.current_task: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()
        self._last_task_type = "general_chat"
        self._growth_running = False
        self._last_growth_attempt = 0.0
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")  # growth_engine writes to the same file
        except sqlite3.DatabaseError:
            pass
        self._ensure_tables()
        self.interactions_since_last_growth = self._load_growth_counter()
        self._announce_database()

    @staticmethod
    def _candidate_dbs():
        seen, out = set(), []
        for base in (ROOT_DIR, BASE_DIR, BASE_DIR.parent, Path.cwd()):
            p = (base / "knowledge_base" / "buddy_graph.db").resolve()
            if p.exists() and p not in seen:
                seen.add(p)
                out.append(p)
        return out

    @staticmethod
    def _describe_db(path: Path) -> Dict[str, Any]:
        try:
            con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
            rows = con.execute("SELECT COUNT(*), COUNT(DISTINCT session_id), MAX(timestamp) FROM agent_memory").fetchone()
            con.close()
            last = datetime.fromtimestamp(rows[2]).strftime("%Y-%m-%d %H:%M") if rows[2] else "never"
            return {"path": str(path), "mb": round(path.stat().st_size / 1e6, 1), "messages": rows[0],
                    "sessions": rows[1], "last_message": last}
        except sqlite3.Error as e:
            return {"path": str(path), "error": str(e)}

    def _announce_database(self):
        if os.environ.get("BUDDY_QUIET"):
            return
        mine = self._describe_db(DB_PATH.resolve())
        print(f"[MEMORY] database: {mine.get('path')} - {mine.get('messages', '?')} messages in "
              f"{mine.get('sessions', '?')} sessions, last message {mine.get('last_message', '?')}")
        for other in self._candidate_dbs():
            if other != DB_PATH.resolve():
                d = self._describe_db(other)
                if d.get("messages", 0) > mine.get("messages", 0):
                    print(f"[MEMORY] WARNING: another database has more history: {d['path']} "
                          f"({d['messages']} messages, last {d['last_message']}). If Buddy seems to have forgotten "
                          f"things, copy it over {DB_PATH} (stop Buddy first) or set BUDDY_ROOT. "
                          f"Run `python buddy_memory.py --where` for details.")

    def _generate_session_id(self) -> str:
        ts = time.time()
        return f"sess_{int(ts)}_{hashlib.sha256(str(ts).encode()).hexdigest()[:8]}"

    def _ensure_tables(self):
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT,
                content TEXT,
                task_type TEXT,
                model_used TEXT,
                success INTEGER,
                execution_time REAL,
                timestamp REAL,
                feedback TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_session ON agent_memory(session_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_timestamp ON agent_memory(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_task ON agent_memory(task_type)")
        self.conn.commit()

    def _load_growth_counter(self) -> int:
        # Persist counter in a small metadata table
        c = self.conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        c.execute("SELECT value FROM memory_meta WHERE key = 'interactions_since_growth'")
        row = c.fetchone()
        if row:
            try:
                return int(row[0])
            except:
                return 0
        return 0

    def _write_counter_locked(self):
        """Caller must hold self._lock."""
        self.conn.execute("""
            INSERT OR REPLACE INTO memory_meta (key, value)
            VALUES ('interactions_since_growth', ?)
        """, (str(self.interactions_since_last_growth),))
        self.conn.commit()

    def _save_growth_counter(self):
        with self._lock:
            self._write_counter_locked()

    def _bump_counter(self, n: int = 1):
        with self._lock:
            self.interactions_since_last_growth += n
            self._write_counter_locked()

    # -------------------------------
    # TASK TYPE DETECTION
    # -------------------------------
    def detect_task_type(self, prompt: str) -> str:
        import re
        p = prompt.lower()
        keywords = {
            "coding": ["code", "function", "script", "program", "debug", "error", "import", "class", "def", "python",
                       "javascript", "js", "typescript", "ts", "html", "css", "sql", "api", "endpoint", "route", "bug",
                       "fix", "refactor", "compile", "build", "deploy", "git", "github", "repo"],
            "writing": ["write", "essay", "article", "blog", "story", "email", "letter", "draft", "compose", "content",
                        "copy", "description", "summary", "paraphrase", "rewrite", "translate", "poem", "novel",
                        "chapter", "dialogue"],
            "research": ["research", "find", "search", "look up", "information", "data", "study", "analyze", "report",
                         "paper", "arxiv", "wiki", "wikipedia", "source", "reference", "citation", "fact", "statistics",
                         "trend", "market", "competitor", "compare"],
            "math": ["calculate", "compute", "math", "formula", "equation", "solve", "number", "sum", "average",
                     "percentage", "statistics", "probability", "algebra", "calculus", "geometry", "convert", "unit"],
            "image_generation": ["image", "picture", "photo", "draw", "generate image", "create image", "art", "design",
                                 "logo", "icon", "diagram", "chart", "graph", "visual", "render", "stable diffusion",
                                 "dalle", "midjourney"],
            "video_generation": ["video", "animation", "movie", "clip", "footage", "edit video", "generate video", "wan",
                                 "animate", "motion"],
            "system_admin": ["system", "settings", "config", "configuration", "install", "setup", "environment", "path",
                             "variable", "permission", "access", "password", "login", "account", "user", "admin",
                             "server", "docker", "container", "kubernetes", "k8s"]
        }

        scores = {}
        for task, words in keywords.items():
            score = 0
            for word in words:
                if re.search(rf'\b{re.escape(word)}\b', p):
                    score += 1
            scores[task] = score

        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else "general_chat"

    # -------------------------------
    # LOGGING CORE
    # -------------------------------
    def _log_interaction(self, role: str, content: str, task_type: str, model: str,
                         success: Optional[int], execution_time: float = 0, feedback: str = ""):
        with self._lock:
            c = self.conn.cursor()
            c.execute("""
                INSERT INTO agent_memory (session_id, role, content, task_type, model_used, success, execution_time, timestamp, feedback)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self.session_id, role, content, task_type, model,
                success, execution_time, time.time(), feedback
            ))
            self.conn.commit()

    def log_user_message(self, prompt: str, model: str = "unknown"):
        task_type = self.detect_task_type(prompt)
        self._last_task_type = task_type
        self._log_interaction("user", prompt, task_type, model, None, 0)
        self._bump_counter()

    def log_assistant_message(self, response: str, model: str, success: bool = True,
                              execution_time: float = 0, feedback: str = ""):
        # If we have a current task, use its task_type
        task_type = (self.current_task.get('task_type', self._last_task_type)
                     if self.current_task else self._last_task_type)
        self._log_interaction("assistant", response, task_type, model,
                              int(success), execution_time, feedback)
        self._bump_counter()

    def log_interaction(self, prompt: str, response: str, model: str,
                        success: bool = True, execution_time: float = 0, feedback: str = ""):
        """Convenience method to log both user and assistant messages together."""
        self.log_user_message(prompt, model)
        self.log_assistant_message(response, model, success, execution_time, feedback)

    # -------------------------------
    # GROWTH CYCLE TRIGGERING
    # -------------------------------
    def should_trigger_growth(self) -> bool:
        if self._growth_running:
            return False
        if time.time() - self._last_growth_attempt < GROWTH_RETRY_COOLDOWN:
            return False
        return self.interactions_since_last_growth >= GROWTH_TRIGGER_THRESHOLD

    @staticmethod
    def _find_growth_script() -> Optional[Path]:
        for p in (BASE_DIR / "growth_engine.py", ROOT_DIR / "growth_engine.py",
                  ROOT_DIR / "core" / "growth_engine.py"):
            if p.exists():
                return p
        return None

    def _run_growth(self, script: Path) -> bool:
        ok = False
        try:
            env = os.environ.copy()
            env["BUDDY_ROOT"] = str(ROOT_DIR)
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True, timeout=GROWTH_TIMEOUT, env=env
            )
            if result.stdout:
                print(result.stdout)
            if result.returncode == 0:
                ok = True
            else:
                print(f"[ERROR] Growth engine failed (exit {result.returncode}): {result.stderr[-2000:]}")
        except subprocess.TimeoutExpired:
            print(f"[ERROR] Growth cycle exceeded {GROWTH_TIMEOUT}s and was stopped.")
        except Exception as e:
            print(f"[ERROR] Growth cycle failed: {e}")
        finally:
            with self._lock:
                self._growth_running = False
                if ok:  # only reset the counter when the cycle really succeeded
                    self.interactions_since_last_growth = 0
                    try:
                        self._write_counter_locked()
                    except sqlite3.Error:
                        pass
        return ok

    def trigger_growth(self, force: bool = False, wait: bool = False):
        """Start a growth cycle. Returns True if one was started (or, with wait=True, finished OK).

        By default it runs in a background thread so chat is never blocked."""
        if not force and not self.should_trigger_growth():
            return False
        script = self._find_growth_script()
        if script is None:
            print("[ERROR] growth_engine.py not found next to buddy_memory.py or in the project root.")
            self._last_growth_attempt = time.time()
            return False
        with self._lock:
            if self._growth_running:
                return False
            self._growth_running = True
            self._last_growth_attempt = time.time()
        print("\n[MEMORY] Triggering growth cycle...")
        if wait:
            return self._run_growth(script)
        threading.Thread(target=self._run_growth, args=(script,), daemon=True, name="buddy-growth").start()
        return True

    # -------------------------------
    # STATS & QUERYING
    # -------------------------------
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            c = self.conn.cursor()
            c.execute("SELECT COUNT(*) FROM agent_memory WHERE session_id = ?", (self.session_id,))
            session_count = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM agent_memory")
            total_count = c.fetchone()[0]
            c.execute("SELECT task_type, COUNT(*) FROM agent_memory WHERE role = 'assistant' GROUP BY task_type")
            task_breakdown = {row[0]: row[1] for row in c.fetchall()}
            c.execute("SELECT AVG(success) FROM agent_memory WHERE success IS NOT NULL")
            avg_success = c.fetchone()[0] or 0.0

        return {
            "session_id": self.session_id,
            "session_interactions": session_count,
            "total_interactions": total_count,
            "task_breakdown": task_breakdown,
            "overall_success_rate": round(avg_success * 100, 1) if avg_success else 0.0,
            "interactions_since_growth": self.interactions_since_last_growth
        }

    def get_recent_context(self, limit: int = 10, all_sessions: bool = True) -> str:
        """Last `limit` messages, oldest first.

        By default this spans sessions: every restart starts a NEW session id, and the old code only
        looked at the current one, so a freshly started Buddy remembered nothing. Messages from an
        earlier session are marked with a divider so the model knows when they happened."""
        with self._lock:
            c = self.conn.cursor()
            if all_sessions:
                c.execute("""
                    SELECT role, content, task_type, session_id, timestamp FROM agent_memory
                    ORDER BY id DESC LIMIT ?
                """, (limit,))
            else:
                c.execute("""
                    SELECT role, content, task_type, session_id, timestamp FROM agent_memory
                    WHERE session_id = ? ORDER BY id DESC LIMIT ?
                """, (self.session_id, limit))
            rows = c.fetchall()
        lines = ["## RECENT CONVERSATION HISTORY"]
        prev = object()
        for role, content, task_type, sess, ts in reversed(rows):
            if sess != prev:
                when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "unknown time"
                lines.append("--- this session ---" if sess == self.session_id else f"--- earlier session ({when}) ---")
                prev = sess
            prefix = "User" if role == "user" else "Buddy"
            snippet = content[:200] + "..." if len(content) > 200 else content
            lines.append(f"{prefix} [{task_type}]: {snippet}")
        return "\n".join(lines)

    def search_similar_tasks(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        task_type = self.detect_task_type(query)
        with self._lock:
            c = self.conn.cursor()
            c.execute("""
                SELECT content, success, execution_time, feedback FROM agent_memory
                WHERE task_type = ? AND role = 'assistant' AND success = 1
                ORDER BY timestamp DESC LIMIT ?
            """, (task_type, limit))
            rows = c.fetchall()
        return [
            {"content": row[0], "success": row[1], "execution_time": row[2], "feedback": row[3]}
            for row in rows
        ]

    def close(self):
        self.conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Buddy Memory Logger")
    parser.add_argument("--stats", "-s", action="store_true", help="Show memory stats")
    parser.add_argument("--test", "-t", action="store_true", help="Run a test interaction")
    parser.add_argument("--grow", "-g", action="store_true", help="Trigger growth cycle")
    parser.add_argument("--query", "-q", type=str, help="Search similar past tasks")
    parser.add_argument("--recent", "-r", action="store_true", help="Show what Buddy would remember right now")
    parser.add_argument("--where", "-w", action="store_true", help="List every memory database found and what is in it")
    args = parser.parse_args()

    if args.where:
        os.environ["BUDDY_QUIET"] = "1"
    mem = BuddyMemory()

    if args.where:
        print(f"In use: {DB_PATH.resolve()}")
        for d in BuddyMemory._candidate_dbs():
            print("  " + json.dumps(BuddyMemory._describe_db(d)))
    elif args.recent:
        print(mem.get_recent_context(20))
    elif args.stats:
        print(json.dumps(mem.get_stats(), indent=2))
    elif args.test:
        print("[TEST] Logging test interactions...")
        mem.log_interaction("Write a Python function to calculate fibonacci", "def fib(n):...", model="llama3.1", success=True)
        mem.log_interaction("Generate an image of a cat", "Image generated successfully", model="wan2.1", success=True)
        mem.log_interaction("Debug this error: NameError", "You need to define the variable first", model="qwen3", success=False)
        print(json.dumps(mem.get_stats(), indent=2))
        print("\n[TEST] Recent context:")
        print(mem.get_recent_context())
    elif args.grow:
        mem.trigger_growth(force=True, wait=True)
    elif args.query:
        results = mem.search_similar_tasks(args.query)
        print(f"Found {len(results)} similar successful tasks:")
        for r in results:
            print(f"  - {r['content'][:100]}... (success={r['success']}, time={r['execution_time']:.2f}s)")
    else:
        print("Buddy Memory Logger v1.0")
        print("Use --stats, --test, --grow, or --query")

    mem.close()
