"""
BUDDY GROWTH ENGINE v1.3.3

v1.3.3: files starting with a UTF-8 BOM are now indexed (they were silently skipped).
v1.3.2: evaluator/evolver only look at assistant rows (user rows have success=NULL, which capped the
  success rate at ~50% and forced evolution every cycle); 'uploads' folder is never ingested.
v1.3.1 fixes on top of v1.3: METADATA written as valid Python (was JSON true/false -> NameError),
  skill ids include source file (same-named functions no longer collide), temporary sys.path +
  module cache in forged skills, load errors now recorded as skill failures, API double-count restored.

Changes vs v1.2:
  - Configurable evolution, pruning, and evaluation thresholds at top of file
  - AST complexity check in _is_skill_worthy (requires >= 4 statements and >= 25-char docstring)
  - Real importlib execution wiring in auto-forged skills (replaces static discovery stubs)
  - Built-in KnowledgeGraph.record_skill_outcome() telemetry helper for live usage/success tracking
  - CLI --evolve flag to trigger an evolution cycle immediately

Run:            python growth_engine.py
Full rescan:    python growth_engine.py --full
Continuous:     python growth_engine.py --continuous
Stats only:     python growth_engine.py --stats
Force evolve:   python growth_engine.py --evolve
Override root:  set BUDDY_ROOT=C:/path/to/project
"""

import os, sys, json, sqlite3, hashlib, re, ast, time, importlib.util
from datetime import datetime
from pathlib import Path
from collections import defaultdict


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


BASE_DIR = _find_root()
WORKSPACE_DIR = BASE_DIR / "workspace"
KNOWLEDGE_DIR = BASE_DIR / "knowledge_base"
SKILLS_DIR = BASE_DIR / "Skills"
AGENT_DIR = BASE_DIR / "agent"
TASKS_DIR = BASE_DIR / "tasks"
CACHE_DIR = BASE_DIR / "cache"
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".snapshots", "uploads", ".venv", "venv", "env", "dist",
             "build", ".pytest_cache", ".mypy_cache", ".tox", ".next", ".nuxt", ".output",
             "coverage", "htmlcov", ".eggs", "site-packages"}
SCAN_EXTS = {".py", ".js", ".ts"}
MAX_SKILLS_PER_REPO = 100
MAX_FILE_BYTES = 1_000_000
COMMIT_EVERY = 500

# -- Tunable Evolution & Pruning Thresholds --------------------------------
EVOLUTION_SUCCESS_THRESHOLD = 0.80
PRUNE_SUCCESS_THRESHOLD = 0.30
PRUNE_MIN_USES = 8
EVAL_WINDOW_SECONDS = 12 * 3600  # 12-hour rolling evaluation window


def _ensure_dirs():
    for d in (KNOWLEDGE_DIR, SKILLS_DIR, AGENT_DIR, TASKS_DIR, CACHE_DIR, LOGS_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)


class KnowledgeGraph:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.autocommit = True
        self._pending = 0
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        self._init_db()

    def _init_db(self):
        self.conn.executescript("""
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, name TEXT, type TEXT, source TEXT,
    content TEXT, metadata TEXT, created REAL, updated REAL,
    access_count INTEGER DEFAULT 0, success_count INTEGER DEFAULT 0, fail_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY, from_id TEXT, to_id TEXT, rel_type TEXT,
    strength REAL DEFAULT 1.0, created REAL
);
CREATE TABLE IF NOT EXISTS skill_registry (
    id TEXT PRIMARY KEY, name TEXT, description TEXT, code TEXT,
    language TEXT, source_repo TEXT, auto_generated INTEGER DEFAULT 1,
    test_status TEXT, usage_count INTEGER DEFAULT 0, success_rate REAL DEFAULT 0.0,
    created REAL, last_used REAL
);
CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
    content TEXT, task_type TEXT, model_used TEXT, success INTEGER,
    execution_time REAL, timestamp REAL, feedback TEXT
);
CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, generation INTEGER, change_type TEXT,
    description TEXT, old_value TEXT, new_value TEXT, reason TEXT, timestamp REAL
);
CREATE TABLE IF NOT EXISTS file_state (
    path TEXT PRIMARY KEY, mtime REAL, size INTEGER
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_rel_to ON relationships(to_id);
CREATE INDEX IF NOT EXISTS idx_memory_task ON agent_memory(task_type);
CREATE INDEX IF NOT EXISTS idx_memory_success ON agent_memory(success);
        """)
        self.conn.commit()

    # -- commit batching ---------------------------------------------------
    def _touch(self):
        self._pending += 1
        if self.autocommit or self._pending >= COMMIT_EVERY:
            self.commit()

    def commit(self):
        self.conn.commit()
        self._pending = 0

    # -- writes -------------------------------------------------------------
    def add_entity(self, name, etype, source, content, metadata=None):
        eid = hashlib.sha256(f"{name}:{etype}:{source}".encode()).hexdigest()[:16]
        now = time.time()
        self.conn.execute("""
            INSERT OR REPLACE INTO entities
            (id, name, type, source, content, metadata, created, updated)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created FROM entities WHERE id=?), ?), ?)
        """, (eid, name, etype, source, content, json.dumps(metadata or {}), eid, now, now))
        self._touch()
        return eid

    def add_relationship(self, from_id, to_id, rel_type, strength=1.0):
        rid = hashlib.sha256(f"{from_id}:{to_id}:{rel_type}".encode()).hexdigest()[:16]
        self.conn.execute("""
            INSERT OR REPLACE INTO relationships (id, from_id, to_id, rel_type, strength, created)
            VALUES (?, ?, ?, ?, ?, COALESCE((SELECT created FROM relationships WHERE id=?), ?))
        """, (rid, from_id, to_id, rel_type, strength, rid, time.time()))
        self._touch()
        return rid

    def register_skill(self, skill_id, name, description, code, language, source_repo):
        self.conn.execute("""
            INSERT INTO skill_registry
            (id, name, description, code, language, source_repo, auto_generated, test_status, created)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'unverified', ?)
            ON CONFLICT(id) DO UPDATE SET description=excluded.description, code=excluded.code
        """, (skill_id, name, description, code, language, source_repo, time.time()))
        self._touch()

    def record_skill_outcome(self, skill_id, success: bool):
        """Update running usage_count, success_rate, test_status, and last_used after skill execution."""
        outcome = 1.0 if success else 0.0
        now = time.time()
        self.conn.execute("""
            UPDATE skill_registry
            SET usage_count = usage_count + 1,
                success_rate = ((COALESCE(success_rate, 0.0) * usage_count) + ?) / (usage_count + 1),
                test_status = CASE WHEN ? = 1.0 THEN 'verified' ELSE test_status END,
                last_used = ?
            WHERE id = ?
        """, (outcome, outcome, now, skill_id))
        self._touch()

    def log_evolution(self, generation, change_type, description, old_val, new_val, reason):
        self.conn.execute("""
            INSERT INTO evolution_log (generation, change_type, description, old_value, new_value, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (generation, change_type, description, old_val, new_val, reason, time.time()))
        self._touch()

    def file_unchanged(self, path, mtime, size):
        row = self.conn.execute("SELECT mtime, size FROM file_state WHERE path=?", (path,)).fetchone()
        return bool(row) and row[0] == mtime and row[1] == size

    def mark_file(self, path, mtime, size):
        self.conn.execute("INSERT OR REPLACE INTO file_state (path, mtime, size) VALUES (?, ?, ?)",
                          (path, mtime, size))
        self._touch()

    # -- reads --------------------------------------------------------------
    def find_entity(self, name_pattern, etype=None):
        c = self.conn.cursor()
        if etype:
            c.execute("SELECT * FROM entities WHERE name LIKE ? AND type=?", (f"%{name_pattern}%", etype))
        else:
            c.execute("SELECT * FROM entities WHERE name LIKE ?", (f"%{name_pattern}%",))
        return c.fetchall()

    def get_related(self, entity_id, rel_type=None):
        c = self.conn.cursor()
        if rel_type:
            c.execute("""
                SELECT e.*, r.rel_type, r.strength FROM entities e
                JOIN relationships r ON (e.id = r.to_id AND r.from_id = ?)
                WHERE r.rel_type = ? ORDER BY r.strength DESC
            """, (entity_id, rel_type))
        else:
            c.execute("""
                SELECT e.*, r.rel_type, r.strength FROM entities e
                JOIN relationships r ON (e.id = r.to_id AND r.from_id = ?)
                ORDER BY r.strength DESC
            """, (entity_id,))
        return c.fetchall()

    def get_stats(self):
        c = self.conn.cursor()
        stats = {}
        for table in ["entities", "relationships", "skill_registry", "agent_memory", "evolution_log"]:
            c.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = c.fetchone()[0]
        return stats


class WorkspaceIngestor:
    def __init__(self, kg):
        _ensure_dirs()
        self.kg = kg
        self._repo_skills = 0
        self.stats = {"repos_scanned": 0, "functions_found": 0, "classes_found": 0, "apis_found": 0,
                      "files_scanned": 0, "files_skipped_unchanged": 0, "skills_forged": 0}

    @staticmethod
    def _skip_name(name):
        return name in SKIP_DIRS or name.startswith(".") or name.endswith(".egg-info")

    def scan_workspace(self, full=False):
        print("\n[SCAN] Scanning workspace...")
        if not WORKSPACE_DIR.exists():
            print(f"  ERROR: {WORKSPACE_DIR} not found (set BUDDY_ROOT if the project root is elsewhere)")
            return
        self.kg.autocommit = False
        try:
            for repo_path in sorted(WORKSPACE_DIR.iterdir()):
                if not repo_path.is_dir():
                    continue
                if self._skip_name(repo_path.name):
                    print(f"  [SKIP] {repo_path.name} (hidden/cache)")
                    continue
                try:
                    self._ingest_repo(repo_path, full)
                except KeyboardInterrupt:
                    print("\n  [STOP] Scan interrupted by user.")
                    raise
                except Exception as e:
                    print(f"  [ERROR] {repo_path.name}: {e}")
        finally:
            self.kg.autocommit = True
            self.kg.commit()
        s = self.stats
        print(f"  Done: {s['repos_scanned']} repos, {s['files_scanned']} files scanned "
              f"({s['files_skipped_unchanged']} unchanged, skipped), {s['functions_found']} functions, "
              f"{s['classes_found']} classes, {s['apis_found']} apis, {s['skills_forged']} skills")

    def _ingest_repo(self, repo_path, full=False):
        repo_name = repo_path.name
        self._repo_skills = 0
        repo_id = self.kg.add_entity(repo_name, "repo", str(repo_path), "", {"path": str(repo_path)})
        self.stats["repos_scanned"] += 1
        print(f"  [REPO] {repo_name}...", end=" ", flush=True)
        for dirpath, dirnames, filenames in os.walk(repo_path):
            dirnames[:] = [d for d in dirnames if not self._skip_name(d)]  # prune: never descend
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in SCAN_EXTS:
                    continue
                fpath = Path(dirpath) / fname
                try:
                    st = fpath.stat()
                    if st.st_size > MAX_FILE_BYTES:
                        continue
                    key = str(fpath)
                    if not full and self.kg.file_unchanged(key, st.st_mtime, st.st_size):
                        self.stats["files_skipped_unchanged"] += 1
                        continue
                    self.stats["files_scanned"] += 1
                    if self.stats["files_scanned"] % 500 == 0:
                        print(f"\r  [REPO] {repo_name}... ({self.stats['files_scanned']} files scanned)",
                              end="", flush=True)
                    content = fpath.read_text(encoding="utf-8-sig", errors="ignore")
                    if ext == ".py":
                        self._extract_python(fpath, content, repo_id, repo_name)
                    else:
                        self._extract_javascript(fpath, content, repo_id, repo_name)
                    self._extract_api_patterns(content, fpath, repo_id)
                    self.kg.mark_file(key, st.st_mtime, st.st_size)
                except Exception:
                    continue  # unreadable/odd file: skip it, never abort the repo
        print(f" -> {self._repo_skills} skills")

    # -- python ------------------------------------------------------------
    def _extract_python(self, file_path, content, repo_id, repo_name):
        rel_path = str(file_path.relative_to(WORKSPACE_DIR))
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError):
            return
        self._visit(tree, "", content.splitlines(), rel_path, file_path, repo_id, repo_name)

    def _visit(self, node, qual, lines, rel_path, file_path, repo_id, repo_name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qname = f"{qual}.{child.name}" if qual else child.name
                methods = [n.name for n in child.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                eid = self.kg.add_entity(f"{repo_name}.{qname}", "class", rel_path,
                                         ast.get_docstring(child) or "",
                                         {"methods": methods, "repo": repo_name})
                self.kg.add_relationship(repo_id, eid, "contains")
                self.stats["classes_found"] += 1
                self._visit(child, qname, lines, rel_path, file_path, repo_id, repo_name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = f"{qual}.{child.name}" if qual else child.name
                is_method = bool(qual)
                if not (child.name.startswith("__") and child.name != "__init__"):
                    a = child.args
                    args = [x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)]
                    doc = ast.get_docstring(child) or ""
                    body = "\n".join(lines[child.lineno - 1:child.end_lineno])[:20000]
                    is_async = isinstance(child, ast.AsyncFunctionDef)
                    eid = self.kg.add_entity(
                        f"{repo_name}.{qname}", "function", rel_path, body,
                        {"args": args, "docstring": doc, "repo": repo_name, "file": file_path.name,
                         "async": is_async, "is_method": is_method})
                    self.kg.add_relationship(repo_id, eid, "contains")
                    self.stats["functions_found"] += 1
                    # Forge top-level functions (directly importable without instance state)
                    if (not is_method and self._repo_skills < MAX_SKILLS_PER_REPO
                            and self._is_skill_worthy(child.name, doc, args, child)):
                        self._forge_skill(
                            eid, qname, doc, body, "python", repo_name,
                            source_file=str(file_path.resolve()), is_async=is_async
                        )
                        self._repo_skills += 1
                        self.stats["skills_forged"] += 1
                self._visit(child, qname, lines, rel_path, file_path, repo_id, repo_name)
            elif isinstance(child, ast.stmt):
                self._visit(child, qual, lines, rel_path, file_path, repo_id, repo_name)

    # -- javascript / typescript --------------------------------------------
    def _extract_javascript(self, file_path, content, repo_id, repo_name):
        rel_path = str(file_path.relative_to(WORKSPACE_DIR))
        for match in re.finditer(r"function\s+(\w+)\s*\(([^)]*)\)", content):
            func_name, args = match.groups()
            eid = self.kg.add_entity(
                f"{repo_name}.{func_name}", "function", rel_path, "",
                {"args": args.split(","), "repo": repo_name, "language": "javascript"})
            self.kg.add_relationship(repo_id, eid, "contains")
            self.stats["functions_found"] += 1
        for match in re.finditer(r"const\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>", content):
            func_name, args = match.groups()
            eid = self.kg.add_entity(
                f"{repo_name}.{func_name}", "function", rel_path, "",
                {"args": args.split(","), "repo": repo_name, "language": "javascript", "type": "arrow"})
            self.kg.add_relationship(repo_id, eid, "contains")
            self.stats["functions_found"] += 1

    def _extract_api_patterns(self, content, file_path, repo_id):
        patterns = [
            (r"@app\.(get|post|put|delete|patch)\(\s*[\"'](/[^\"']*)", "fastapi"),
            (r"@router\.(get|post|put|delete|patch)\(\s*[\"'](/[^\"']*)", "fastapi_router"),
            (r"(?<!@)\b(?:app|router)\.(get|post|put|delete|patch)\(\s*[\"'](/[^\"']*)", "express"),
        ]
        for pattern, framework in patterns:
            for match in re.finditer(pattern, content):
                method, route = match.groups()
                api_id = self.kg.add_entity(
                    f"{method.upper()} {route}", "api", str(file_path), "",
                    {"method": method, "route": route, "framework": framework})
                self.kg.add_relationship(repo_id, api_id, "exposes")
                self.stats["apis_found"] += 1

    # -- skills --------------------------------------------------------------
    def _is_skill_worthy(self, func_name, docstring, args, node):
        if not docstring or len(docstring.strip()) < 25:
            return False
        if func_name.startswith("_") or len(args) > 8:
            return False

        # Require >= 4 AST statements (filters out trivial 1-line getters/wrappers)
        statements = [n for n in ast.walk(node) if isinstance(n, ast.stmt)]
        if len(statements) < 4:
            return False

        verbs = [
            "get", "fetch", "create", "build", "parse", "analyze", "convert",
            "generate", "calculate", "validate", "format", "search", "filter",
            "sort", "transform", "process", "orchestrate", "execute", "sync"
        ]
        return any(func_name.lower().startswith(v) for v in verbs)

    def _forge_skill(self, entity_id, func_name, docstring, func_body, language, repo_name,
                     source_file="", is_async=False):
        clean_func = func_name.replace(".", "_")
        skill_name = f"{repo_name}_{clean_func}"
        try:
            _rel = Path(source_file).resolve().relative_to(WORKSPACE_DIR.resolve()).as_posix()
        except Exception:
            _rel = str(source_file)
        skill_id = hashlib.sha256(f"{skill_name}|{_rel}".encode()).hexdigest()[:12]
        if language == "python":
            desc_200 = docstring[:200].replace("\n", " ")
            desc_150 = docstring[:150].replace("\n", " ").replace('"', "'")
            meta = repr({
                "skill_id": skill_id,
                "name": skill_name,
                "description": desc_150,
                "language": language,
                "source_file": source_file,
                "target_func": func_name,
                "is_async": is_async,
                "auto_generated": True,
                "entity_id": entity_id,
                "success_rate": 0.0,
                "usage_count": 0,
            })

            wrapper = "\n".join([
                f"# Auto-forged Executable Skill: {skill_name}",
                f"# Source: {repo_name} ({source_file})",
                f"# Discovered: {datetime.now().isoformat()}",
                f"# Entity ID: {entity_id}",
                "",
                "import asyncio",
                "import importlib.util",
                "import inspect",
                "import sys",
                "from pathlib import Path",
                "",
                f"METADATA = {meta}",
                "",
                "",
                "def _load_target():",
                '    src = Path(METADATA["source_file"])',
                "    if not src.exists():",
                '        raise FileNotFoundError(f"Skill source file missing: {src}")',
                '    mod_name = f"_buddy_skill_{METADATA[\'skill_id\']}_{src.stem}"',
                "    module = sys.modules.get(mod_name)",
                "    if module is None:",
                "        spec = importlib.util.spec_from_file_location(mod_name, str(src))",
                "        if spec is None or spec.loader is None:",
                '            raise ImportError(f"Could not create module spec for {src}")',
                "        module = importlib.util.module_from_spec(spec)",
                "        sys.modules[mod_name] = module",
                "        src_dir = str(src.parent)",
                "        sys.path.insert(0, src_dir)  # temporary, only while the module loads",
                "        try:",
                "            spec.loader.exec_module(module)",
                "        except BaseException:",
                "            sys.modules.pop(mod_name, None)",
                "            raise",
                "        finally:",
                "            if src_dir in sys.path:",
                "                sys.path.remove(src_dir)",
                '    return getattr(module, METADATA["target_func"])',
                "",
                "",
                "def execute(*args, **kwargs):",
                f"    {json.dumps(desc_200)}",
                "    fn = _load_target()",
                "    result = fn(*args, **kwargs)",
                "    if inspect.isawaitable(result):",
                "        try:",
                "            loop = asyncio.get_running_loop()",
                "        except RuntimeError:",
                "            loop = None",
                "        if loop and loop.is_running():",
                "            return result  # caller is already inside an event loop",
                "        return asyncio.run(result)",
                "    return result",
                "",
            ])
        else:
            wrapper = f"// Skill wrapper for {skill_name}\n// Source: {source_file}\n// TODO: Implement bridge\n"

        skill_path = SKILLS_DIR / f"{skill_id}.py"
        skill_path.write_text(wrapper, encoding="utf-8")
        skill_eid = self.kg.add_entity(skill_name, "skill", str(skill_path), wrapper,
                                       {"auto_generated": True, "source_entity": entity_id,
                                        "source_file": source_file, "language": language})
        self.kg.add_relationship(entity_id, skill_eid, "evolved_from")
        self.kg.register_skill(skill_id, skill_name, docstring[:200], wrapper, language, repo_name)
        print(f"    [SKILL] Forged: {skill_name} ({skill_id})")


class AgentEvolver:
    def __init__(self, kg):
        _ensure_dirs()
        self.kg = kg
        self.generation = self._get_current_generation()

    def _get_current_generation(self):
        c = self.kg.conn.cursor()
        c.execute("SELECT MAX(generation) FROM evolution_log")
        result = c.fetchone()[0]
        return (result or 0) + 1

    def evolve(self):
        print(f"\n[EVOLVE] Evolution Cycle (Generation {self.generation})")
        success_patterns = self._analyze_success_patterns()
        self._evolve_system_prompts(success_patterns)
        self._evolve_skill_routing()
        self._prune_dead_skills()
        self._save_generation_config()
        self.generation += 1
        print(f"  Done. Now at Generation {self.generation}")

    def _analyze_success_patterns(self):
        c = self.kg.conn.cursor()
        c.execute("""
            SELECT task_type, model_used, AVG(success), COUNT(*)
            FROM agent_memory WHERE role = 'assistant' AND timestamp > ?
            GROUP BY task_type, model_used
        """, (time.time() - 7 * 24 * 3600,))
        patterns = defaultdict(lambda: {"total": 0, "success": 0, "models": {}})
        for task_type, model, avg_success, count in c.fetchall():
            avg_success = avg_success or 0
            patterns[task_type or "unknown"]["total"] += count
            patterns[task_type or "unknown"]["success"] += avg_success * count
            patterns[task_type or "unknown"]["models"][model or "unknown"] = {
                "success_rate": avg_success, "count": count}
        return patterns

    def _evolve_system_prompts(self, patterns):
        prompt_path = AGENT_DIR / f"system_prompt_v{self.generation}.txt"
        best_models = {}
        for task_type, data in patterns.items():
            if data["models"]:
                best = max(data["models"].items(), key=lambda x: x[1]["success_rate"])
                best_models[task_type] = best[0]
        lines = [
            f"# Buddy OS Agent - Generation {self.generation}",
            f"# Auto-evolved: {datetime.now().isoformat()}",
            f"# Based on {len(patterns)} task type performance patterns",
            "",
            "You are Buddy, an autonomous AI operating system.",
            "Your capabilities expand continuously through the Growth Engine.",
            "",
            "## LEARNED ROUTING (from performance data)",
        ]
        for task, model in best_models.items():
            lines.append(f"- {task}: use {model}")
        lines += [
            "",
            "## AVAILABLE SKILLS",
            "Check the Skills/ directory and agent/skill_routing_v*.json for auto-discovered capabilities.",
            "When uncertain, consult the Knowledge Graph (knowledge_base/buddy_graph.db).",
            "",
            "## SELF-IMPROVEMENT RULES",
            "1. After every task, log success/failure to agent_memory",
            f"2. If a skill fails repeatedly (>{PRUNE_MIN_USES} uses, <{int(PRUNE_SUCCESS_THRESHOLD*100)}% success), it is marked deprecated",
            "3. If no skill exists for a task, request workspace ingestion",
            "4. Prefer cached responses for identical prompts",
            f"5. Evolve your own prompts when success rate drops below {int(EVOLUTION_SUCCESS_THRESHOLD*100)}%",
            "",
            "## CORE DIRECTIVE",
            "Grow. Learn. Adapt. Every interaction makes you stronger.",
        ]
        prompt_path.write_text("\n".join(lines), encoding="utf-8")
        self.kg.log_evolution(
            self.generation, "prompt_update",
            f"System prompt evolved to Gen {self.generation}",
            f"Gen {self.generation-1} prompt",
            f"Gen {self.generation} prompt with {len(best_models)} routing rules",
            "Performance analysis showed model-task mismatches")
        print(f"  [PROMPT] Evolved -> {prompt_path.name}")

    def _evolve_skill_routing(self):
        c = self.kg.conn.cursor()
        c.execute("SELECT id, name, description FROM skill_registry "
                  "WHERE auto_generated = 1 AND COALESCE(test_status, '') != 'deprecated'")
        routing = {}
        for skill_id, name, desc in c.fetchall():
            text = f"{(name or '').replace('_', ' ')} {desc or ''}".lower()
            for kw in set(re.findall(r"\b\w{4,}\b", text)):
                routing.setdefault(kw, []).append({"skill": name, "id": skill_id})
        routing_path = AGENT_DIR / f"skill_routing_v{self.generation}.json"
        routing_path.write_text(json.dumps(routing, indent=2), encoding="utf-8")
        self.kg.log_evolution(
            self.generation, "routing_update", "Skill routing table updated",
            f"Gen {self.generation-1} routing",
            f"Gen {self.generation} routing with {len(routing)} keyword mappings",
            "New skills discovered in workspace")
        print(f"  [ROUTING] Updated -> {routing_path.name} ({len(routing)} keywords)")

    def _prune_dead_skills(self):
        c = self.kg.conn.cursor()
        c.execute(
            "SELECT id, name, usage_count, success_rate FROM skill_registry "
            "WHERE usage_count > ? AND success_rate < ?",
            (PRUNE_MIN_USES, PRUNE_SUCCESS_THRESHOLD),
        )
        for skill_id, name, usage, rate in c.fetchall():
            c.execute("UPDATE skill_registry SET test_status = 'deprecated' WHERE id = ?", (skill_id,))
            print(f"  [PRUNE] Dead skill: {name} ({usage} uses, {int((rate or 0)*100)}% success)")
        self.kg.commit()

    def _save_generation_config(self):
        config = {
            "generation": self.generation,
            "timestamp": time.time(),
            "stats": self.kg.get_stats(),
            "system_prompt": f"system_prompt_v{self.generation}.txt",
            "skill_routing": f"skill_routing_v{self.generation}.json",
        }
        (AGENT_DIR / f"generation_{self.generation}_config.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8")


class SelfEvaluator:
    def __init__(self, kg):
        _ensure_dirs()
        self.kg = kg

    def evaluate_last_session(self):
        c = self.kg.conn.cursor()
        c.execute("""
            SELECT task_type, success, execution_time, model_used, content
            FROM agent_memory WHERE role = 'assistant' AND timestamp > ?
            ORDER BY timestamp DESC LIMIT 100
        """, (time.time() - EVAL_WINDOW_SECONDS,))
        rows = c.fetchall()
        if not rows:
            print("  [EVAL] No recent activity to evaluate")
            return None
        total = len(rows)
        successes = sum(1 for r in rows if r[1] == 1)
        avg_time = sum((r[2] or 0) for r in rows) / total
        model_perf = defaultdict(lambda: {"success": 0, "total": 0, "time": 0})
        for task_type, success, exec_time, model, content in rows:
            m = model or "unknown"
            model_perf[m]["total"] += 1
            model_perf[m]["success"] += (success or 0)
            model_perf[m]["time"] += (exec_time or 0)
        window_hrs = round(EVAL_WINDOW_SECONDS / 3600, 1)
        report = {
            "evaluated_at": datetime.now().isoformat(),
            "period": f"last_{window_hrs}h",
            "total_interactions": total,
            "success_rate": successes / total,
            "avg_response_time": avg_time,
            "model_rankings": {
                model: {"success_rate": d["success"] / d["total"],
                        "avg_time": d["time"] / d["total"], "usage": d["total"]}
                for model, d in model_perf.items()
            },
            "recommendations": [],
        }
        if report["success_rate"] < EVOLUTION_SUCCESS_THRESHOLD:
            report["recommendations"].append(
                f"Success rate below {int(EVOLUTION_SUCCESS_THRESHOLD*100)}%. Triggering prompt evolution."
            )
        for model, d in report["model_rankings"].items():
            if d["success_rate"] < 0.5 and d["usage"] > 10:
                report["recommendations"].append(f"Model {model} underperforming. Reduce routing priority.")
        (LOGS_DIR / f"evaluation_{int(time.time())}.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8")
        print("\n[EVAL] Self-Evaluation Report")
        print(f"  Period: Last {window_hrs} hours")
        print(f"  Interactions: {total}")
        print(f"  Success Rate: {int(report['success_rate']*100)}%")
        print(f"  Avg Response Time: {round(avg_time, 2)}s")
        print(f"  Recommendations: {len(report['recommendations'])}")
        for rec in report["recommendations"]:
            print(f"    ! {rec}")
        return report


class SkillRunner:
    """Helper for worker scripts to load, execute, and record telemetry for forged skills."""
    def __init__(self, kg: KnowledgeGraph):
        self.kg = kg

    def run(self, skill_id: str, *args, **kwargs):
        skill_path = SKILLS_DIR / f"{skill_id}.py"
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill file not found: {skill_path}")
        try:
            spec = importlib.util.spec_from_file_location(f"buddy_skill_{skill_id}", str(skill_path))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            res = module.execute(*args, **kwargs)
            self.kg.record_skill_outcome(skill_id, True)
            return res
        except Exception:
            self.kg.record_skill_outcome(skill_id, False)
            raise


class GrowthEngine:
    def __init__(self):
        _ensure_dirs()
        self.kg = KnowledgeGraph(str(KNOWLEDGE_DIR / "buddy_graph.db"))
        self.ingestor = WorkspaceIngestor(self.kg)
        self.evolver = AgentEvolver(self.kg)
        self.evaluator = SelfEvaluator(self.kg)
        self.runner = SkillRunner(self.kg)
        self.running = True

    def run_cycle(self, full=False):
        print("\n" + "=" * 60)
        print(f"BUDDY GROWTH ENGINE - Cycle {datetime.now().isoformat()}")
        print(f"Project root: {BASE_DIR}")
        print("=" * 60)
        self.ingestor.scan_workspace(full=full)
        report = self.evaluator.evaluate_last_session()
        if report and report.get("success_rate", 1.0) < EVOLUTION_SUCCESS_THRESHOLD:
            print("\n  Performance below threshold. Triggering evolution...")
            self.evolver.evolve()
        else:
            print("\n  Performance acceptable. Skipping evolution.")
        stats = self.kg.get_stats()
        print("\n[STATS] Knowledge Graph")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        print("\n[GROWTH] Cycle complete.")
        return stats

    def continuous_mode(self, interval_minutes=60):
        print(f"\n[CONTINUOUS] Growth Mode (every {interval_minutes} minutes)")
        print("Press Ctrl+C to stop\n")
        try:
            while self.running:
                try:
                    self.run_cycle()
                except Exception as e:  # one bad cycle must not kill the loop
                    print(f"\n[ERROR] Cycle failed: {type(e).__name__}: {e}")
                if not self.running:
                    break
                print(f"\n[SLEEP] Sleeping for {interval_minutes} minutes...")
                time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            print("\n\n[STOP] Growth Engine stopped by user.")
        print(f"Final stats: {self.kg.get_stats()}")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Buddy Growth Engine")
    parser.add_argument("--continuous", "-c", action="store_true", help="Run continuously")
    parser.add_argument("--interval", "-i", type=int, default=60, help="Minutes between cycles")
    parser.add_argument("--stats", "-s", action="store_true", help="Show current stats only")
    parser.add_argument("--full", "-f", action="store_true", help="Rescan every file, ignoring unchanged-file cache")
    parser.add_argument("--evolve", "-e", action="store_true", help="Force an evolution cycle immediately")
    args = parser.parse_args(argv)
    engine = GrowthEngine()
    try:
        if args.stats:
            print(json.dumps(engine.kg.get_stats(), indent=2))
        elif args.evolve:
            engine.evolver.evolve()
        elif args.continuous:
            engine.continuous_mode(args.interval)
        else:
            engine.run_cycle(full=args.full)
    except KeyboardInterrupt:
        print("\n[STOP] Interrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())