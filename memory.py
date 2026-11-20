"""
BuddyMemory - Minimal persistent memory for Buddy OS.
Zero required dependencies. SQLite + keyword fallback.
"""
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Dict


class BuddyMemory:
    def __init__(self, workspace_dir: str = "./workspace/memory"):
        self.workspace = Path(workspace_dir)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "memory.sqlite"
        self.wm_path = self.workspace / "working_memory.json"
        self._init_db()
        self.working_memory = self._load_wm()
        self._encoder = None
        self._encoder_ok = None

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                id TEXT PRIMARY KEY,
                type TEXT,
                content TEXT,
                created TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _load_wm(self) -> List[Dict]:
        if self.wm_path.exists():
            try:
                return json.loads(self.wm_path.read_text())
            except Exception:
                pass
        return []

    def _save_wm(self):
        self.wm_path.write_text(json.dumps(self.working_memory, indent=2))

    def _get_embedding(self, text: str) -> List[float]:
        if self._encoder_ok is False:
            return []
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._encoder = SentenceTransformer('all-MiniLM-L6-v2')
                self._encoder_ok = True
            except Exception:
                self._encoder_ok = False
                return []
        try:
            return self._encoder.encode(text).tolist()
        except Exception:
            return []

    def store_exchange(self, user_msg: str, assistant_msg: str):
        """Store a chat exchange."""
        self._store("episodic", f"User: {user_msg}")
        self._store("episodic", f"Buddy: {assistant_msg[:500]}")
        self.working_memory.append({"role": "user", "content": user_msg})
        self.working_memory.append({"role": "assistant", "content": assistant_msg})
        if len(self.working_memory) > 40:
            self.working_memory = self.working_memory[-40:]
        self._save_wm()

    def store_mission(self, command: str, outcome: str, success: bool = True):
        """Store a mission outcome."""
        status = "completed" if success else "failed"
        self._store("episodic", f"Mission {status}: {command[:200]} — {outcome[:300]}")

    def _store(self, type_: str, content: str):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), type_, content, datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()

    def recall(self, query: str, k: int = 5) -> List[str]:
        """Retrieve relevant memories. Falls back to keyword search if no embeddings."""
        # Try semantic search first
        embedding = self._get_embedding(query)
        if embedding:
            try:
                import chromadb
                client = chromadb.PersistentClient(path=str(self.workspace / "chroma"))
                collection = client.get_or_create_collection("traces")
                collection.add(
                    ids=[str(uuid.uuid4())],
                    embeddings=[embedding],
                    metadatas=[{"query": "temp"}]
                )
                results = collection.query(query_embeddings=[embedding], n_results=k)
                # This is a simplified recall - in production you'd index all traces
                # For now, fall through to keyword search which always works
            except Exception:
                pass

        # Keyword fallback (always works)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        words = [w for w in query.lower().split() if len(w) > 2]
        if words:
            like_clause = " OR ".join(["content LIKE ?"] * len(words))
            cursor.execute(
                f"SELECT content FROM traces WHERE {like_clause} ORDER BY created DESC LIMIT ?",
                [f"%{w}%" for w in words] + [k * 2]
            )
        else:
            cursor.execute("SELECT content FROM traces ORDER BY created DESC LIMIT ?", (k * 2,))
        rows = cursor.fetchall()
        conn.close()

        # Deduplicate and rank by relevance
        seen = set()
        results = []
        for row in rows:
            content = row[0]
            if content not in seen:
                seen.add(content)
                score = sum(1 for w in words if w in content.lower())
                results.append((score, content))
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:k]]

    def inject_into_prompt(self, system_prompt: str, memories: List[str]) -> str:
        """Format memories and append to system prompt."""
        if not memories:
            return system_prompt
        mem_block = "\n## RELEVANT PAST MEMORIES\n" + "\n".join(f"- {m}" for m in memories)
        return system_prompt + mem_block

    def get_working_memory(self) -> List[Dict]:
        """Get recent conversation for context window."""
        return self.working_memory

    def count(self) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM traces")
        count = c.fetchone()[0]
        conn.close()
        return count

    def save(self):
        self._save_wm()
