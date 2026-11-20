#!/usr/bin/env python3
"""Buddy OS Autonomous Engine v9.1 - one engine, local brain, obeys commands.

v9.1 fixes (on top of v9):
  * MEMORY: task mode now gets the conversation history (it got none); history survives restarts and the
    model is told which part is from an earlier session; every task outcome is saved AND read back
    ("RECENT WORK"); user messages are no longer stored twice.
  * LOOP: a plain-text answer is accepted as the final answer (it used to be rejected until the step limit);
    repeated identical commands are stopped; multi-line DONE answers are kept (only line 1 was);
    limits/errors produce an honest report of what really happened instead of a generic message.
  * READ: understands .docx and .pdf, and refuses binary files instead of returning garbage.
  * CHAT: no tools, so it must not claim work was done; it sees the real log of the last tasks.
  * SKILL: really inspects a skill (was a stub). BUDDY_MAX_STEPS / BUDDY_MAX_SECONDS env overrides.

Merges the old autonomous_engine.py (v4) and agent_core.py into one file.

What it does
  * Decides CHAT vs TASK with a word-boundary classifier (no more "hi" matching "history").
  * CHAT  -> one plain local-LLM reply. Never executes anything.
  * TASK  -> autonomous tool loop: READ / GREP / LIST / WRITE / RUN / DONE.
             Files you mention (api.py, C:\\...\\x.json) are pre-read and injected.
             If the model says "I can't read files", it is corrected and forced to use READ.
             Python code blocks the model forgets to WRITE are auto-saved, auto-run, and
             fed back so it can self-heal with the full error in front of it.
  * Missing pip packages are installed automatically (cv2 -> opencv-python, etc.).
  * Local only. No tier='best', no cloud routing. Talks to Ollama at localhost:11434.
  * Timeouts really kill the whole process tree (Windows taskkill /T).
  * Every overwrite is backed up to workspace/.backups/. A STOP file in the project
    root halts the agent. A tiny blocklist stops drive-wiping commands only.

Public API kept from v4: AutonomousEngine(skills_registry, persist_path, llm_client,
soul_path), .chat(), .execute_task(), .status(), .list_skills(), .learn_from_task(),
.shutdown(), .classify_request().
"""
from __future__ import annotations

import ast
import atexit
import contextlib
import glob
import html
import json
import logging
import os
import random
import re
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Optional subsystems - a broken optional module must never kill the engine
# ---------------------------------------------------------------------------
try:
    from core.advanced_memory import AdvancedMemory
    ADVANCED_MEM_AVAILABLE = True
except Exception:  # ImportError, SyntaxError, missing deps ...
    ADVANCED_MEM_AVAILABLE = False
    AdvancedMemory = None

try:
    from core.verification_loops import VerificationLoops
    from core.rollback_manager import RollbackManager
    from core.observability import Observability
    VERIFICATION_AVAILABLE = True
except Exception:
    VERIFICATION_AVAILABLE = False
    VerificationLoops = RollbackManager = Observability = None

logger = logging.getLogger("buddy_os.universal_ai")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_ENGINE_FILE = Path(__file__).resolve()


def _find_root() -> Path:
    env = os.environ.get("BUDDY_ROOT")
    if env:
        return Path(env).resolve()
    here = _ENGINE_FILE.parent
    if here.name.lower() == "core":
        return here.parent
    if (here / "core").is_dir():
        return here
    return here.parent


ROOT = _find_root()

JUNK_DIRS = {
    "__pycache__", "node_modules", ".venv", "venv", "env", ".git", ".idea",
    ".vscode", "dist", "build", ".backups", ".snapshots", "site-packages",
}
TEXT_EXTS = {
    ".py", ".md", ".json", ".txt", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".bat", ".ps1", ".js", ".ts", ".html", ".css", ".env",
}

PIP_MAP = {
    "cv2": "opencv-python", "PIL": "Pillow", "yaml": "pyyaml", "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn", "dotenv": "python-dotenv", "dateutil": "python-dateutil",
    "skimage": "scikit-image", "docx": "python-docx", "pptx": "python-pptx",
    "fitz": "pymupdf", "serial": "pyserial", "OpenSSL": "pyopenssl", "git": "gitpython",
    "jwt": "pyjwt", "magic": "python-magic", "Crypto": "pycryptodome",
    "win32com": "pywin32", "win32api": "pywin32", "attr": "attrs",
}
LOCAL_HINTS = {"core", "agent", "agents", "buddy", "workspace", "ultimate_buddy",
               "tools", "skills", "services", "eva"}

_DANGEROUS = [
    r"\bformat\s+[a-z]:",
    r"\b(?:rd|rmdir)\b[^\n]*\s[\"']?[a-z]:[\\/]?[\"']?\s*$",
    r"\b(?:del|erase)\b[^\n]*\s[\"']?[a-z]:[\\/]+\*?",
    r"\brm\s+-[a-z]*r[a-z]*\s+(?:/|~|\*|\$home)(?:\s|$)",
    r"\bremove-item\b[^\n]*\s[\"']?[a-z]:[\\/]?[\"']?\s*(?:-|$)",
    r"\b(?:del|erase|rd|rmdir|remove-item|rm)\b[^\n]*[\\/]windows(?:[\\/]|\b)",
    r"\bdiskpart\b", r"\bbcdedit\b", r"\bmkfs(?:\.\w+)?\b",
    r"\bdd\s+if=[^\n]*\sof=/dev/", r":\(\)\s*\{",
]
_DANGEROUS_RE = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS]


def _protected_dirs() -> List[str]:
    if os.name == "nt":
        vals = [os.environ.get(k) for k in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)")]
        return [v.lower().rstrip("\\/") for v in vals if v]
    return ["/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev"]


PROTECTED = _protected_dirs()

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_think(text: str) -> str:
    text = text or ""
    if "</think>" in text.lower():
        parts = re.split(r"</think>", text, flags=re.IGNORECASE)
        after_think = parts[-1].strip()
        if after_think:
            return after_think
        return re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()
    
    cleaned = _THINK_RE.sub("", text).strip()
    return cleaned if cleaned else re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()


_CMD_RE = re.compile(
    r"^[ \t>*`\-\[\]]*\**(READ|GREP|LIST|WRITE|RUN|TEST|SKILLS|SKILL|AGENT|PLAN|HARDWARE|TRADING|DONE)\**[ \t]*:\**[ \t]*(.*)$", re.MULTILINE)


def _parse_command(resp: str) -> Optional[Tuple[str, str, str]]:
    m = _CMD_RE.search(resp)
    if not m:
        return None
    return m.group(1), m.group(2).strip().strip("`").strip(), resp[m.end():]


_FILE_RE = re.compile(
    r"(?<![\w.])((?:[A-Za-z]:)?[\w\-./\\]+\.(?:py|md|json|txt|yaml|yml|toml|ini|cfg|bat|ps1|js|ts|html|css|env|docx|pdf|csv))\b")

_TASK_VERBS = (
    r"build|create|make|write|generate|fix|repair|refactor|rewrite|update|upgrade|add|remove|"
    r"delete|install|uninstall|run|execute|launch|test|verify|check|scan|audit|analy[sz]e|debug|"
    r"deploy|read|open|grep|search|find|list|edit|change|modify|patch|implement|convert|download|"
    r"scrape|move|copy|rename|compile|package|push|pull|clone|append|replace|clean|"
    r"optimi[sz]e|review|summari[sz]e"
)
_POLITE = (
    r"^(?:(?:hi|hey|hello|yo|ok|okay|please|pls|kindly|now|just|then|also|so|alright|buddy|eva)[,!.\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+(?:want|need)\s+you\s+to\s+|"
    r"go\s+ahead\s+and\s+|let'?s\s+)?"
)
_TASK_START_RE = re.compile(_POLITE + r"(?:" + _TASK_VERBS + r")\b", re.IGNORECASE)
_TASK_ANY_RE = re.compile(r"\b(?:" + _TASK_VERBS + r")\b", re.IGNORECASE)
_TASK_CHAIN_RE = re.compile(r"\b(?:and|then|also)\s+(?:" + _TASK_VERBS + r")\b", re.IGNORECASE)
_USE_SKILL_RE = re.compile(r"^\s*use\s+\S+.*\bto\b", re.IGNORECASE)
_CHAT_START_RE = re.compile(
    r"^(?:hi|hey|hello|yo|sup|thanks|thank\s+you|thx|ok|okay|cool|bye|good\s+(?:morning|afternoon|evening|night)|"
    r"how\s+are\s+you|what'?s\s+up|what|why|how|who|when|where|which|is|are|do|does|did|"
    r"tell\s+me|explain|describe|talk)\b", re.IGNORECASE)
_QUESTION_START_RE = re.compile(
    r"^(?:what|why|how|who|when|where|which|is|are|do|does|did|tell\s+me|explain|describe)\b", re.IGNORECASE)


def classify(text: str) -> str:
    """Return 'chat' or 'task'."""
    t = (text or "").strip()
    if not t:
        return "chat"
    no_url = re.sub(r"https?://\S+", " ", t)
    if "```" in t or _FILE_RE.search(no_url):
        return "task"
    if _TASK_START_RE.match(t) or _TASK_CHAIN_RE.search(t) or _USE_SKILL_RE.match(t):
        return "task"
    words = len(t.split())
    if words >= 6 and not _QUESTION_START_RE.match(t) and _TASK_ANY_RE.search(t):
        return "task"
    if _CHAT_START_RE.match(t):
        return "chat"
    return "task" if words > 25 else "chat"


def _docx_text(path: Path) -> str:
    """Plain text of a .docx (no extra packages: a .docx is a zip containing XML)."""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    lines = ["".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S)) for p in paras]
    return html.unescape("\n".join(lines))


def _pdf_text(path: Path) -> str:
    for mod, fn in (("pypdf", "PdfReader"), ("PyPDF2", "PdfReader")):
        try:
            lib = __import__(mod)
            reader = getattr(lib, fn)(str(path))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)
        except ImportError:
            continue
    try:
        import fitz  # pymupdf
        with fitz.open(str(path)) as doc:
            return "\n".join(pg.get_text() for pg in doc)
    except ImportError:
        raise RuntimeError("reading PDFs needs a package: pip install pypdf")


def _iter_files(base, exts=None, skip_dirs=(), limit=30000):
    n = 0
    skip = {str(Path(s)) for s in skip_dirs}
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in JUNK_DIRS and str(Path(dirpath) / d) not in skip]
        for f in files:
            if exts and Path(f).suffix.lower() not in exts:
                continue
            n += 1
            if n > limit:
                return
            yield Path(dirpath) / f


# ---------------------------------------------------------------------------
# Memory / personality
# ---------------------------------------------------------------------------
class BuddyMemoryLegacy:
    def __init__(self, persist_path=None):
        self.facts: Dict[str, Any] = {}
        self.user_preferences = {"style": "unknown", "favorite_stack": [], "dislikes": [], "projects": {}}
        self.persist_path = Path(persist_path) if persist_path else ROOT / "workspace" / "buddy_brain.json"
        self._load()

    def _load(self):
        if self.persist_path.exists():
            try:
                data = json.loads(self.persist_path.read_text(encoding="utf-8"))
                self.facts = data.get("facts", {})
                prefs = data.get("prefs", {})
                if isinstance(prefs, dict):
                    self.user_preferences.update(prefs)
            except Exception as e:
                logger.warning(f"Legacy brain load failed: {e}")

    def save(self):
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"facts": self.facts, "prefs": self.user_preferences,
                    "saved_at": datetime.now().isoformat()}
            self.persist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Legacy brain save failed: {e}")

    def learn_fact(self, key, value):
        self.facts[key] = value
        self.save()


class BuddyPersonality:
    def __init__(self):
        self.mood = "neutral"
        self.energy = 1.0
        self.interaction_count = 0


class _SimpleMemory:
    """Fallback memory when AdvancedMemory (chromadb etc.) is unavailable."""

    def __init__(self, workspace_dir):
        self.workspace_dir = Path(workspace_dir)
        self.facts: Dict[str, Any] = {}
        self.notes: Dict[str, list] = {}
        self._load()

    def _load(self):
        mw = self.workspace_dir / "simple_memory.json"
        if mw.exists():
            try:
                data = json.loads(mw.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.facts = data.get("facts", {})
                    self.notes = data.get("notes", {})
            except Exception:
                pass

    def save(self):
        try:
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
            (self.workspace_dir / "simple_memory.json").write_text(
                json.dumps({"facts": self.facts, "notes": self.notes}, indent=2), encoding="utf-8")
        except Exception:
            pass

    def add_note(self, note, category):
        self.notes.setdefault(category, []).append({"note": note, "ts": time.time()})
        self.notes[category] = self.notes[category][-200:]
        self.save()

    def recall(self, query, k=5):
        q = query.lower()
        res = [i for items in self.notes.values() for i in items if q in i["note"].lower()]
        res.sort(key=lambda x: x["ts"], reverse=True)
        return res[:k]

    def shutdown(self):
        self.save()


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------
class AutonomousEngine:
    def __init__(self, skills_registry=None, persist_path=None, llm_client=None, soul_path=None,
                 model: Optional[str] = None, max_steps: int = 12, max_seconds: int = 900):
        self.root = ROOT
        persist = Path(persist_path) if persist_path else None
        self.workspace = Path(persist.parent if persist else self.root / "workspace").resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.stop_file = self.root / "STOP"
        self.log_file = self.workspace / "agent_log.jsonl"
        self.backup_dir = self.workspace / ".backups"

        self.llm_client = llm_client
        self.model = model or os.environ.get("BUDDY_MODEL")
        try:
            self.max_steps = int(os.environ.get("BUDDY_MAX_STEPS", max_steps))
            self.max_seconds = int(os.environ.get("BUDDY_MAX_SECONDS", max_seconds))
        except ValueError:
            self.max_steps, self.max_seconds = max_steps, max_seconds
        self.session_id = time.strftime("%Y%m%d_%H%M%S")  # changes on every start; stored with each message
        self.run_timeout = 60
        self._lock = threading.Lock()
        self._last_task = ""

        if skills_registry is None:
            try:
                from core.skills_registry import get_registry
                skills_registry = get_registry()
            except Exception as e:
                logger.warning(f"Skills registry failed to load: {e}")
        self.skills = skills_registry
        self.available_skills = self._skill_names()

        self.legacy = BuddyMemoryLegacy(persist_path=persist or self.workspace / "buddy_brain.json")
        self.advanced_memory = None
        if ADVANCED_MEM_AVAILABLE and AdvancedMemory is not None:
            try:
                self.advanced_memory = AdvancedMemory(
                    workspace_dir=str(self.workspace), session_id="default",
                    consolidation_interval=300, enable_consolidation=True)
            except Exception as e:
                logger.warning(f"AdvancedMemory failed, using simple memory: {e}")
        if self.advanced_memory is None:
            self.advanced_memory = _SimpleMemory(self.workspace)

        self.working_memory_path = self.workspace / "conversation_history.json"
        raw = self._load_working_memory()
        self.working_memory: List[Dict[str, Any]] = raw if isinstance(raw, list) else []
        self.personality = BuddyPersonality()

        self.verifier = self.rollback_mgr = self.observability = None
        if VERIFICATION_AVAILABLE:
            try:
                self.verifier = VerificationLoops(self.workspace)
                self.rollback_mgr = RollbackManager(self.workspace)
                self.observability = Observability(self.workspace)
            except Exception as e:
                logger.warning(f"Verification/Rollback/Observability unavailable: {e}")

        self.soul_content = self._load_soul(soul_path)
        atexit.register(self.shutdown)
        logger.info(f"Autonomous Engine v9 ready: root={self.root} skills={len(self.available_skills)} "
                    f"memory_msgs={len(self.working_memory)}")

    def _skill_names(self) -> List[str]:
        try:
            raw = self.skills.list_skills() if self.skills else []
            return [s if isinstance(s, str) else str(getattr(s, "name", s)) for s in raw]
        except Exception:
            return []

    def _load_soul(self, soul_path) -> str:
        candidates = []
        if soul_path:
            candidates.append(Path(soul_path))
        candidates += [self.root / "SOUL.md", self.root / "soul.md", Path("SOUL.md"), Path("soul.md")]
        for c in candidates:
            try:
                if c.exists():
                    return c.read_text(encoding="utf-8", errors="ignore")[:6000]
            except Exception:
                continue
        return "You are Buddy OS, a helpful AI assistant capable of building anything."

    def _load_working_memory(self):
        if self.working_memory_path.exists():
            try:
                return json.loads(self.working_memory_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_working_memory(self):
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            tmp = self.working_memory_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.working_memory, f, indent=2)
            tmp.replace(self.working_memory_path)
        except Exception:
            pass

    def _add_to_working_memory(self, role, content):
        with self._lock:
            self.working_memory.append({"role": role, "content": str(content)[:6000], "ts": time.time(),
                                        "sid": self.session_id})
            self.working_memory = self.working_memory[-200:]
            self._save_working_memory()

    def _history_messages(self, n=12, exclude_last=False, newest_chars=3000, older_chars=700) -> List[Dict[str, str]]:
        """Last `n` user/assistant messages for the prompt, oldest first. Recent ones keep more text."""
        window = [m for m in self.working_memory if m.get("role") in ("user", "assistant") and m.get("content")]
        if exclude_last and window:
            window = window[:-1]
        window = window[-n:]
        out = []
        for i, m in enumerate(window):
            limit = newest_chars if i >= len(window) - 4 else older_chars
            out.append({"role": m["role"], "content": str(m["content"])[:limit]})
        return out

    def _recent_work_block(self, k=5) -> str:
        """The real log of recent tasks (written by execute_task, read back here)."""
        keys = sorted(x for x in self.legacy.facts if str(x).startswith("task_"))[-k:]
        lines = []
        for key in keys:
            v = self.legacy.facts.get(key)
            if not isinstance(v, dict):
                continue
            try:
                when = datetime.fromtimestamp(float(str(key)[5:])).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                when = "earlier"
            out = v.get("outcome")
            if isinstance(out, dict):
                status, result = out.get("status", "?"), out.get("result", "")
            else:
                status, result = "?", str(out or "")
            lines.append(f"- {when} [{status}] {str(v.get('task', ''))[:100]} -> {str(result)[:220]}")
        return "\n".join(lines)

    def _memory_context(self) -> str:
        out = ""
        recent = [m for m in self.working_memory[-12:] if m.get("role") in ("user", "assistant")]
        earlier = [m for m in recent if m.get("sid") != self.session_id]
        if earlier:
            when = datetime.fromtimestamp(earlier[-1].get("ts", time.time())).strftime("%Y-%m-%d %H:%M")
            out += ("\n\n## MEMORY\nYou were restarted. Part of the conversation history that follows is from an "
                    f"EARLIER session (last activity {when}). It is real - you do remember it.")
        work = self._recent_work_block()
        if work:
            out += "\n\n## RECENT WORK (real log of tasks you ran - trust it over guesses)\n" + work
        return out

    def _log(self, entry: Dict[str, Any]):
        try:
            entry["ts"] = datetime.now().isoformat(timespec="seconds")
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def _get_client(self):
        if self.llm_client is not None:
            return self.llm_client
        try:
            from openai import OpenAI
            self.llm_client = OpenAI(
                base_url=os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                api_key="ollama", timeout=300)
            self.model = self.model or "qwen3-coder:30b"
            return self.llm_client
        except Exception as e:
            raise RuntimeError(f"No llm_client given and local Ollama client could not be created: {e}")

    def _call_llm(self, messages, temperature=0.3, max_tokens=4096, client=None) -> str:
        c = client or self._get_client()
        if hasattr(c, "chat") and hasattr(c.chat, "completions"):
            kw = dict(model=getattr(c, "model", None) or self.model, messages=messages, temperature=temperature, max_tokens=max_tokens)
            if os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").startswith(("http://localhost", "http://127.0.0.1")):
                think = os.environ.get("BUDDY_OLLAMA_THINK", "false").strip().lower() in ("1", "true", "yes", "on")
                kw["extra_body"] = {"think": think}
            last_err = None
            for attempt in range(2):
                try:
                    resp = c.chat.completions.create(**kw)
                    return resp.choices[0].message.content or ""
                except Exception as e:
                    last_err = e
                    if attempt == 0:
                        time.sleep(1.5)
            raise last_err
        raise RuntimeError("llm_client has no recognizable interface")

    def _now(self) -> str:
        return datetime.now().strftime("%A, %B %d, %Y %I:%M %p")

    def _env_text(self) -> str:
        if os.name == "nt":
            return ("Platform: Windows. Shell: cmd.exe. Use ONLY Windows commands (dir, type, findstr, del, where). "
                    "Never use ls, cat, grep, rm, pwd, chmod. `python` and `pip` already point to the right interpreter.")
        return "Platform: Linux/macOS shell."

    def _capabilities(self) -> str:
        for p in (self.root / "CAPABILITIES.md", Path("CAPABILITIES.md")):
            try:
                if p.exists():
                    return "\n\n## YOUR CAPABILITIES (existing modules - reuse them)\n" + \
                        p.read_text(encoding="utf-8", errors="ignore")[:2500]
            except Exception:
                pass
        return ""

    def _chat_system_prompt(self) -> str:
        return (f"{self.soul_content}\n\nIt is currently {self._now()}. You live in real time.\n"
                "Reply naturally, directly and briefly. Do not output tool commands in casual conversation."
                + self._memory_context() +
                "\n\n## CHAT MODE RULES\nIn this mode you have NO tools. Never say you created, ran, tested, read or "
                "verified anything. If asked what happened in earlier work, answer only from RECENT WORK above; if it "
                "is not there, say you do not know. For real changes, tell the user to give you a task "
                "(for example: fix core/x.py).")

    def _task_system_prompt(self) -> str:
        skills = "\n".join("- " + s for s in self.available_skills[:20]) or "- none loaded"
        return f"""{self.soul_content}

## ENVIRONMENT
{self._env_text()}
Project root: {self.root}
Workspace (default place for NEW files and where RUN executes): {self.workspace}
Current date/time: {self._now()}

## YOU HAVE REAL, FULL ACCESS TO THIS COMPUTER'S PROJECT FILES
You can read, search, list, write and run things on this machine right now, through the commands below.
NEVER say you cannot access, read or open files. NEVER ask the user to paste a file. If you need a file, use READ.
Files the user mentions are already injected as [PRE-READ ...] blocks - treat them as the real file content.

## COMMANDS - exactly ONE per reply, at the START of a line
  READ: <path>             read a text, .docx or .pdf file (optional line range: READ: <path> | 200-400)
  GREP: <text>             case-insensitive search across project source files
  LIST: <glob or folder>   e.g. LIST: core\\*.py
  WRITE: <path>            then, in the SAME reply, ONE fenced code block with the COMPLETE new file content
  RUN: <command>           ONE literal shell command (python x.py, pip install x, git status, dir ...)
  TEST: <command>          run a verification command (pytest, unittest, compileall, ...)
  SKILLS: <filter>         list registered skills; empty filter lists all names
  SKILL: <exact name>      inspect skill path and documentation, WITHOUT executing it
  DONE: <answer>           finish - put your final answer / summary after DONE:

## RULES
- To fix or change a file you already read, you MUST send `WRITE: <the same path>` immediately followed by
  ONE fenced code block with the complete corrected file. NEVER send a bare code block with no WRITE: line.
- Always send COMPLETE files when you WRITE (no "..." placeholders).
- After writing code, RUN it to verify. If it fails, read the error, fix it, and run again. Only DONE when it works.
- Never claim you created, ran or fixed something unless a tool output in THIS conversation shows it. If something
  failed or you could not finish, say so plainly in your DONE: answer.
- Do not repeat a command you already ran; use its earlier output or try something different.
- When you have the answer, reply `DONE:` followed by the full answer (it may be several lines).

## AVAILABLE SKILLS
{skills}{self._capabilities()}{self._memory_context()}"""

    def _clean_path(self, raw: str) -> str:
        return (raw or "").strip().strip('"').strip("'").strip("`").strip()

    def _resolve_existing(self, raw: str) -> Optional[Path]:
        raw = self._clean_path(raw)
        if not raw:
            return None
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        try:
            if p.is_absolute():
                return p if p.exists() else None
            for base in (self.root, self.workspace):
                c = base / p
                if c.exists():
                    return c
        except Exception:
            pass
        return None

    def _resolve_target(self, raw: str) -> Path:
        raw = self._clean_path(raw)
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if p.is_absolute():
            return p
        return self.root / p

    def _tool_read(self, arg: str) -> str:
        parts = arg.split("|", 1)
        path_str = parts[0].strip()
        p = self._resolve_existing(path_str)
        if not p or not p.is_file():
            p2 = self.workspace / path_str
            if p2.is_file():
                p = p2
            else:
                return f"Error: File not found: {path_str}"
        try:
            suffix = p.suffix.lower()
            if suffix == ".docx":
                content = _docx_text(p)
            elif suffix == ".pdf":
                content = _pdf_text(p)
            else:
                if b"\x00" in p.read_bytes()[:4096]:
                    return (f"Error: {p.name} is a binary file ({suffix or 'no extension'}); READ only works on "
                            "text files, .docx and .pdf.")
                content = p.read_text(encoding="utf-8", errors="ignore")
            if len(parts) > 1:
                rng = parts[1].strip()
                if "-" in rng:
                    s, e = rng.split("-", 1)
                    lines = content.splitlines()
                    start = int(s.strip()) - 1 if s.strip().isdigit() else 0
                    end = int(e.strip()) if e.strip().isdigit() else len(lines)
                    content = "\n".join(lines[start:end])
            if len(content) > 15000:
                content = content[:15000] + "\n\n[... truncated at 15,000 chars ...]"
            return f"=== FILE: {p} ===\n{content}"
        except Exception as e:
            return f"Error reading {p}: {e}"

    def _tool_skill(self, name: str) -> str:
        name = (name or "").strip()
        if not name:
            return "Error: give the exact skill name (see SKILLS:)."
        try:
            skill = self.skills.get(name) if self.skills else None
        except Exception:
            skill = None
        if not skill:
            close = [x for x in self.available_skills if name.lower() in x.lower()][:5]
            return f"Error: no skill named '{name}'." + (f" Did you mean: {', '.join(close)}" if close else "")
        path = Path(str(getattr(skill, "path", "")))
        doc = ""
        for fn in ("SKILL.md", "README.md", "readme.md"):
            f = path / fn
            if f.exists():
                doc = f.read_text(encoding="utf-8", errors="ignore")[:2500]
                break
        return f"Skill '{name}'\nPath: {path}\n\n{doc or '(no SKILL.md / README found)'}"

    def _tool_grep(self, query: str) -> str:
        q = query.strip().strip('"').strip("'")
        if not q:
            return "Error: Empty search query"
        results = []
        try:
            pattern = re.compile(q, re.IGNORECASE)
            for f in _iter_files(self.root, skip_dirs=[self.workspace / ".backups"]):
                if f.name.startswith(".") or f.suffix.lower() not in TEXT_EXTS:
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    for i, line in enumerate(text.splitlines(), 1):
                        if pattern.search(line):
                            rel = f.relative_to(self.root)
                            results.append(f"{rel}:{i}: {line.strip()}")
                            if len(results) >= 40:
                                return "\n".join(results) + "\n[... truncated at 40 matches ...]"
                except Exception:
                    continue
        except Exception as e:
            return f"Grep error: {e}"
        return "\n".join(results) if results else f"No matches found for '{q}'."

    def _tool_list(self, pattern: str) -> str:
        pat = pattern.strip()
        if not pat or pat in (".", "*"):
            target = self.root
            glob_pat = "*"
        else:
            p = Path(pat)
            if p.is_absolute():
                target = p.parent
                glob_pat = p.name
            else:
                target = self.root / p.parent
                glob_pat = p.name

        if not target.exists():
            return f"Error: Directory not found: {target}"

        try:
            out = []
            for item in sorted(target.glob(glob_pat)):
                if item.name in JUNK_DIRS or item.name.startswith("."):
                    continue
                rel = item.relative_to(self.root) if item.is_relative_to(self.root) else item
                kind = "DIR " if item.is_dir() else "FILE"
                out.append(f"[{kind}] {rel}")
                if len(out) >= 100:
                    out.append("... [truncated at 100 items]")
                    break
            return "\n".join(out) if out else "No matching files found."
        except Exception as e:
            return f"List error: {e}"

    def _tool_write(self, path_str: str, body: str) -> str:
        p = self._resolve_target(path_str)
        p_str_lower = str(p.resolve()).lower()
        for prot in PROTECTED:
            if p_str_lower.startswith(prot):
                return f"ERROR: Writing to protected system directory '{prot}' is blocked."

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists():
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                backup_name = f"{p.name}.{int(time.time())}.bak"
                backup_path = self.backup_dir / backup_name
                backup_path.write_bytes(p.read_bytes())
            
            p.write_text(body, encoding="utf-8")
            return f"Successfully wrote {len(body)} bytes to {p}."
        except Exception as e:
            return f"Error writing {p}: {e}"

    def _check_safety(self, cmd: str) -> Optional[str]:
        for r in _DANGEROUS_RE:
            if r.search(cmd):
                return f"Blocked by safety guard: command matches dangerous pattern."
        return None

    def _tool_run(self, cmd: str) -> str:
        cmd = cmd.strip()
        if not cmd:
            return "Error: Empty command"
        err = self._check_safety(cmd)
        if err:
            return err

        try:
            cwd = self.workspace if cmd.startswith("pytest") else self.root
            res = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.run_timeout
            )
            stdout = res.stdout or ""
            stderr = res.stderr or ""
            out = f"Exit code: {res.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            if len(out) > 8000:
                out = out[:8000] + "\n[... truncated ...]"
            return out
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {self.run_timeout} seconds."
        except Exception as e:
            return f"Execution error: {e}"

    def chat(self, message: str) -> str:
        """Public method for standard chat messages."""
        self._add_to_working_memory("user", message)
        if classify(message) != "chat":
            return self.execute_task(message, _from_chat=True)
        msgs = [{"role": "system", "content": self._chat_system_prompt()}]
        msgs.extend(self._history_messages(n=12))
        try:
            raw_reply = self._call_llm(msgs, temperature=0.7, max_tokens=4096)
        except Exception as e:
            reply = f"I could not reach the local model ({type(e).__name__}: {e}). Is Ollama running?"
            self._add_to_working_memory("assistant", reply)
            return reply
        reply = _strip_think(raw_reply).strip() or raw_reply.strip() or "Model returned an empty response."
        self._add_to_working_memory("assistant", reply)
        return reply

    @staticmethod
    def _summarise(cmd: str, out: str) -> str:
        first = (out or "").strip().splitlines()[0] if (out or "").strip() else "(no output)"
        return first[:140]

    def _run_command(self, cmd: str, arg: str, rest: str) -> str:
        if cmd == "READ":
            return self._tool_read(arg)
        if cmd == "GREP":
            return self._tool_grep(arg)
        if cmd == "LIST":
            return self._tool_list(arg)
        if cmd == "WRITE":
            cb = re.search(r"```(?:python|py|json|yaml|yml|md|txt)?\s*\n(.*?)```", rest, re.DOTALL)
            if cb:
                return self._tool_write(arg, cb.group(1))
            return "Error: WRITE command requires a fenced code block immediately following it in the same response."
        if cmd in ("RUN", "TEST"):
            return self._tool_run(arg)
        if cmd == "SKILLS":
            skills = [x for x in self.available_skills if not arg or arg.lower() in x.lower()]
            return "Available skills:\n" + "\n".join(f"- {x}" for x in skills)
        if cmd == "SKILL":
            return self._tool_skill(arg)
        return (f"Unknown or disabled command: {cmd}. Valid commands: READ, GREP, LIST, WRITE, RUN, TEST, "
                "SKILLS, SKILL, DONE.")

    def execute_task(self, task_description: str, _from_chat: bool = False) -> str:
        """Autonomous tool loop. Always returns an honest report: what was finished, or what actually happened."""
        if not _from_chat:  # chat() already stored the message
            self._add_to_working_memory("user", task_description)
        self._last_task = task_description

        pre_injected = ""
        for fpath in _FILE_RE.findall(task_description)[:3]:
            res = self._tool_read(fpath)
            if not res.startswith("Error"):
                pre_injected += f"\n\n[PRE-READ {fpath}]\n{res}"

        messages = [{"role": "system", "content": self._task_system_prompt()}]
        messages.extend(self._history_messages(n=12, exclude_last=True))  # conversation so far (was missing)
        messages.append({"role": "user", "content": f"Task: {task_description}{pre_injected}"})

        trace: List[str] = []      # honest record of what really happened
        seen: Dict[Tuple[str, str], int] = {}
        no_cmd = 0
        step = 0
        start = time.time()
        status, final_answer = "incomplete", ""
        last_text = ""

        while step < self.max_steps:
            step += 1
            if time.time() - start > self.max_seconds:
                status = "timeout"
                break
            if self.stop_file.exists():
                status, final_answer = "stopped", "Task halted: STOP file detected in root directory."
                break
            try:
                response = _strip_think(self._call_llm(messages, temperature=0.2, max_tokens=4096))
            except Exception as e:
                status = "llm_error"
                trace.append(f"model call failed: {type(e).__name__}: {e}")
                break
            last_text = response
            messages.append({"role": "assistant", "content": response})

            parsed = _parse_command(response)
            if not parsed:
                if "DONE:" in response:
                    final_answer, status = response.split("DONE:", 1)[1].strip(), "done"
                    break
                no_cmd += 1
                if response.strip() and no_cmd >= 2:  # a plain answer twice in a row: that IS the answer
                    final_answer, status = response.strip(), "done"
                    break
                messages.append({"role": "user", "content": (
                    "Reminder: start your reply with ONE command line (READ:, GREP:, LIST:, WRITE:, RUN:, TEST:, "
                    "SKILLS:, SKILL:) or DONE: <final answer>. If you already have the answer, reply DONE: and then it.")})
                continue
            no_cmd = 0

            cmd, arg, rest = parsed
            if cmd == "DONE":
                final_answer = (arg + ("\n" + rest.strip() if rest.strip() else "")).strip() or "Task marked done (no summary given)."
                status = "done"
                break

            key = (cmd, arg)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= 4:
                status = "stuck"
                trace.append(f"stopped: {cmd} {arg[:80]} was issued {seen[key]} times")
                break
            tool_output = self._run_command(cmd, arg, rest)
            if seen[key] >= 2:
                tool_output += "\n\nNOTE: you already ran this exact command. Do not repeat it; use the earlier output, try something different, or finish with DONE:."
            summary = self._summarise(cmd, tool_output)
            trace.append(f"{cmd} {arg[:90]} -> {summary}")
            self._log({"step": step, "cmd": cmd, "arg": arg[:200], "result": summary})
            messages.append({"role": "user", "content": f"Tool output [{cmd} {arg}]:\n{tool_output}"})

        if status in ("done", "stopped"):
            report = final_answer
        else:
            why = {"timeout": f"it ran out of time ({self.max_seconds}s)",
                   "stuck": "it got stuck repeating the same command",
                   "llm_error": "the model call failed",
                   "incomplete": f"it reached the step limit ({self.max_steps}) without finishing"}[status]
            report = f"I did NOT finish this task: {why}."
            if trace:
                report += "\n\nWhat actually happened:\n" + "\n".join(f"- {t}" for t in trace[-12:])
            if last_text.strip():
                report += f"\n\nMy last message was:\n{last_text.strip()[:500]}"

        stored = report + (f"\n[work log: {'; '.join(trace[-8:])}]" if trace else "")
        self._add_to_working_memory("assistant", stored)
        self._log({"task": task_description, "steps": step, "status": status, "result": report[:500]})
        try:
            self.learn_from_task(task_description[:300], {"status": status, "steps": step,
                                                         "result": report[:300], "work": trace[-5:]})
        except Exception:
            pass
        return report

    def status(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "workspace": str(self.workspace),
            "model": self.model,
            "skills_count": len(self.available_skills),
            "memory_messages": len(self.working_memory),
            "session_id": self.session_id,
        }

    def list_skills(self) -> List[str]:
        return self.available_skills

    def learn_from_task(self, task, outcome):
        self.legacy.facts[f"task_{time.time()}"] = {"task": task, "outcome": outcome}
        old = sorted(x for x in self.legacy.facts if str(x).startswith("task_"))[:-100]  # keep the last 100 only
        for x in old:
            self.legacy.facts.pop(x, None)
        self.legacy.save()

    def shutdown(self):
        try:
            self.legacy.save()
            if hasattr(self.advanced_memory, "shutdown"):
                self.advanced_memory.shutdown()
            self._save_working_memory()
        except Exception:
            pass

    def classify_request(self, text: str) -> str:
        return classify(text)