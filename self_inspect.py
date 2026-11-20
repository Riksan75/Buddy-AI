"""
self_inspect.py - what is actually missing or broken in the Buddy project, found by code, not guessed by an LLM.

    python self_inspect.py                 writes self_inspection.md and prints the findings
    python self_inspect.py --stdout        print the full report only (use this as a tool inside Buddy)
    python self_inspect.py --deep          also run agent_dispatcher.py's health check (imports every agent)
    python self_inspect.py --json          machine-readable output

Checks (read-only, nothing is installed, moved or changed):
  1. files that do not parse (syntax errors)
  2. imports that are neither in the standard library, nor installed for THIS python, nor in the project
  3. local services: Ollama (+ which models), ComfyUI, Voicebox, Blender-MCP, the Buddy API
  4. expected files/folders (soul.md, .env keys by NAME only, media_config.json, ...)
  5. memory database size, media hub health

Run it with the same python you start Buddy with, so "installed" means installed for Buddy.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

SKIP = {".git", "__pycache__", "node_modules", "venv", ".venv", "env", "site-packages", "lib", "dist", "build",
        "workspace", "services", "services_data", "skills", "_legacy", "knowledge_base", "media_output",
        ".snapshots", ".mypy_cache", ".pytest_cache", "logs", "cache", "data", "vendor", "third_party",
        "external", "repos", "clones", "custom_nodes", "comfyui", "voicebox", "openmontage", "outputs"}

PIP_NAMES = {"cv2": "opencv-python", "PIL": "pillow", "yaml": "pyyaml", "bs4": "beautifulsoup4",
             "sklearn": "scikit-learn", "dotenv": "python-dotenv", "mem0": "mem0ai", "git": "gitpython",
             "sentence_transformers": "sentence-transformers", "duckduckgo_search": "duckduckgo-search",
             "googleapiclient": "google-api-python-client", "jwt": "pyjwt", "docx": "python-docx",
             "pptx": "python-pptx", "fitz": "pymupdf", "skimage": "scikit-image", "serial": "pyserial",
             "firecrawl": "firecrawl-py", "youtube_transcript_api": "youtube-transcript-api", "gtts": "gTTS",
             "win32api": "pywin32", "win32gui": "pywin32", "multipart": "python-multipart"}
STDLIB = set(getattr(sys, "stdlib_module_names", ()))


# ------------------------------------------------------------------ project scan
def find_root() -> Path:
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


def scan_tree(root: Path):
    files, dirs = [], set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirs.update(dirnames)  # every folder name counts as "in the project" (e.g. `from skills import x`), even skipped ones
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP and not d.startswith(".")]
        files += [Path(dirpath) / f for f in filenames if f.endswith(".py") and f != Path(__file__).name]
    return sorted(files), dirs


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
    return bool(names & {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"})


def collect_imports(tree: ast.AST):
    """Yield (top_level_name, kind) with kind in hard / optional (try-except ImportError) / lazy (inside a function)."""
    found = []

    def walk(stmts, ctx):
        for st in stmts:
            if isinstance(st, ast.Import):
                for a in st.names:
                    found.append((a.name.split(".")[0], ctx))
            elif isinstance(st, ast.ImportFrom):
                if st.level == 0 and st.module:
                    found.append((st.module.split(".")[0], ctx))
            elif isinstance(st, ast.Try):
                opt = any(_catches_import_error(h) for h in st.handlers)
                walk(st.body, "optional" if opt and ctx == "hard" else ctx)
                for h in st.handlers:  # `except ImportError: import fallback` is optional by definition
                    walk(h.body, "optional" if ctx == "hard" and _catches_import_error(h) else ctx)
                walk(st.orelse, ctx)
                walk(st.finalbody, ctx)
            elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(st.body, "lazy" if ctx == "hard" else ctx)
            elif isinstance(st, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                walk(st.body, ctx)
                walk(st.orelse, ctx)
            elif isinstance(st, (ast.With, ast.AsyncWith, ast.ClassDef)):
                walk(st.body, ctx)

    walk(getattr(tree, "body", []), "hard")
    return found


def installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def analyse_code(root: Path) -> dict:
    files, dirs = scan_tree(root)
    local_names = {p.stem for p in files} | dirs
    syntax, missing = [], defaultdict(lambda: {"hard": set(), "optional": set(), "lazy": set()})
    verdict: dict[str, bool] = {}
    for p in files:
        rel = p.relative_to(root).as_posix()
        try:
            # utf-8-sig: a leading BOM is legal for Python, so it must not count as a syntax error
            tree = ast.parse(p.read_bytes().decode("utf-8-sig", errors="replace"))
        except (SyntaxError, ValueError) as e:
            syntax.append({"file": rel, "error": f"line {getattr(e, 'lineno', '?')}: {getattr(e, 'msg', e)}"})
            continue
        for name, kind in collect_imports(tree):
            if name in STDLIB or name in local_names:
                continue
            if name not in verdict:
                verdict[name] = installed(name)
            if not verdict[name]:
                missing[name][kind].add(rel)
    rows = []
    for name, d in missing.items():
        rows.append({"module": name, "pip": PIP_NAMES.get(name, name),
                     "hard": sorted(d["hard"]), "optional": sorted(d["optional"]), "lazy": sorted(d["lazy"])})
    rows.sort(key=lambda r: (-len(r["hard"]), -(len(r["lazy"]) + len(r["optional"])), r["module"]))
    return {"files": len(files), "syntax_errors": syntax, "unresolved_imports": rows}


# ------------------------------------------------------------------ environment
def port_open(host: str, port: int, timeout=1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_services() -> list[dict]:
    out = []
    ollama = {"name": "Ollama", "where": "127.0.0.1:11434", "up": port_open("127.0.0.1", 11434), "detail": ""}
    if ollama["up"]:
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
                models = [m["name"] for m in json.loads(r.read()).get("models", [])]
            ollama["detail"] = "models: " + (", ".join(models) if models else "NONE pulled")
        except Exception as e:  # noqa: BLE001
            ollama["detail"] = f"up, but /api/tags failed: {e}"
    out.append(ollama)
    for name, port in (("ComfyUI", 8188), ("Voicebox", 17493), ("Blender-MCP add-on", 9876), ("Buddy API", 8000)):
        out.append({"name": name, "where": f"127.0.0.1:{port}", "up": port_open("127.0.0.1", port), "detail": ""})
    return out


def check_files(root: Path) -> list[dict]:
    items = []
    for name, why, level in (("soul.md", "Buddy's identity file", "required"), ("CAPABILITIES.md", "operating rules", "required"),
                             ("media_config.json", "ComfyUI workflow / Voicebox profile (copy media_config.example.json)", "recommended"),
                             ("dashboard.html", "web dashboard", "required"),
                             ("requirements.txt", "dependency list for a clean install", "recommended"),
                             ("workforce.json", "self-created agents registry (created automatically on first use)", "auto")):
        items.append({"item": name, "exists": (root / name).exists(), "why": why, "level": level})
    for d in ("workspace", "knowledge_base", "core", "agent"):
        items.append({"item": d + "/", "exists": (root / d).is_dir(), "why": "project folder", "level": "required"})
    env = root / ".env"
    keys = []
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                keys.append((k.strip(), bool(v.strip().strip('"').strip("'"))))
    return {"items": items, "env_keys": keys}


def check_db(root: Path) -> str:
    db = root / "knowledge_base" / "buddy_graph.db"
    if not db.exists():
        return "knowledge_base/buddy_graph.db does not exist yet (the growth engine has never run)"
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        parts = []
        for t in ("entities", "skill_registry", "agent_memory", "evolution_log"):
            try:
                parts.append(f"{t}={con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
            except sqlite3.Error:
                parts.append(f"{t}=missing")
        con.close()
        return f"{db.stat().st_size // 1024} KB, " + ", ".join(parts)
    except sqlite3.Error as e:
        return f"cannot read database: {e}"


def run_script(root: Path, name: str, args: list[str], timeout: int) -> str:
    for cand in (root / name, root / "core" / name):
        if cand.exists():
            try:
                env = {**os.environ, "BUDDY_ROOT": str(root)}
                p = subprocess.run([sys.executable, str(cand), *args], cwd=root, capture_output=True, text=True,
                                   timeout=timeout, env=env)
                return ((p.stdout or "") + (p.stderr or "")).strip()[-3000:] or "(no output)"
            except subprocess.TimeoutExpired:
                return f"timed out after {timeout}s"
    return f"{name} not found in the project root or core/"


# ------------------------------------------------------------------ report
def build(root: Path, deep: bool) -> dict:
    t = time.time()
    code = analyse_code(root)
    files = check_files(root)
    r = {"root": str(root), "python": sys.executable, "version": sys.version.split()[0], "code": code,
         "services": check_services(), "files": files, "database": check_db(root),
         "media_hub": run_script(root, "media_hub.py", ["health"], 30)}
    if deep:
        r["dispatcher"] = run_script(root, "agent_dispatcher.py", [], 180)
    r["seconds"] = round(time.time() - t, 1)
    return r


def findings(r: dict) -> list[str]:
    f = []
    for e in r["code"]["syntax_errors"]:
        f.append(f"BROKEN FILE (will not import): {e['file']} - {e['error']}")
    hard = [m for m in r["code"]["unresolved_imports"] if m["hard"]]
    if hard:
        f.append("MISSING AT STARTUP (imported at the top of a file, not installed and not in the project): "
                 + ", ".join(f"{m['module']} (needed by {len(m['hard'])} file{'s' if len(m['hard']) > 1 else ''})" for m in hard[:15]))
    pips = sorted({m["pip"] for m in r["code"]["unresolved_imports"] if m["hard"] or m["lazy"]})
    if pips:
        f.append("If those are pip packages: pip install " + " ".join(pips[:25]) + "   (names guessed from import names)")
    ol0 = next((s for s in r["services"] if s["name"] == "Ollama"), None)
    if ol0 and not ol0["up"]:
        f.append("SERVICE DOWN: Ollama (127.0.0.1:11434) - Buddy cannot think without it")
    idle = [s["name"] for s in r["services"] if not s["up"] and s["name"] != "Ollama"]
    if idle:
        f.append("NOT RUNNING right now (fine unless you need them; start them first): " + ", ".join(idle))
    ol = next((s for s in r["services"] if s["name"] == "Ollama"), None)
    if ol and ol["up"] and "NONE" in ol["detail"]:
        f.append("Ollama is running but has no models (ollama pull qwen3-coder:30b)")
    for it in r["files"]["items"]:
        if not it["exists"] and it["level"] != "auto":
            f.append(f"{'MISSING' if it['level'] == 'required' else 'RECOMMENDED'}: {it['item']} - {it['why']}")
    blank = [k for k, has in r["files"]["env_keys"] if not has]
    if blank:
        f.append("Empty keys in .env: " + ", ".join(blank))
    if "does not exist" in r["database"]:
        f.append("Growth engine has never run: " + r["database"])
    return f or ["Nothing obviously missing."]


def render(r: dict) -> str:
    c = r["code"]
    L = [f"# Buddy self-inspection ({r['seconds']}s)", f"Root: `{r['root']}`  ", f"Python: `{r['python']}` {r['version']}  ",
         f"Own Python files checked: **{c['files']}**\n", "## Findings (most important first)"]
    L += [f"{i}. {x}" for i, x in enumerate(findings(r), 1)]
    L += ["\n## Services"] + [f"- {'UP  ' if s['up'] else 'DOWN'} {s['name']} {s['where']} {s['detail']}".rstrip() for s in r["services"]]
    L += ["\n## Key files"] + [f"- {'ok     ' if i['exists'] else 'MISSING'} {i['item']} ({i['why']})" for i in r["files"]["items"]]
    if r["files"]["env_keys"]:
        L += ["\n## .env keys (names only, values never shown)"] + [f"- {k}: {'set' if has else 'EMPTY'}" for k, has in r["files"]["env_keys"]]
    L += ["\n## Memory database", f"- {r['database']}", "\n## Media hub", "```", r["media_hub"], "```"]
    if "dispatcher" in r:
        L += ["\n## Agent dispatcher health", "```", r["dispatcher"], "```"]
    L.append("\n## Unresolved imports (detail)")
    for m in c["unresolved_imports"][:60]:
        where = m["hard"] or m["lazy"] or m["optional"]
        kind = "hard" if m["hard"] else "lazy" if m["lazy"] else "optional"
        L.append(f"- `{m['module']}` [{kind}] {', '.join(where[:4])}{' ...' if len(where) > 4 else ''}")
    if c["syntax_errors"]:
        L.append("\n## Syntax errors")
        L += [f"- `{e['file']}`: {e['error']}" for e in c["syntax_errors"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Facts about what is missing or broken in the Buddy project")
    ap.add_argument("--root")
    ap.add_argument("--deep", action="store_true", help="also run the agent dispatcher health check")
    ap.add_argument("--stdout", action="store_true", help="print the full report, write no file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default="self_inspection.md")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else find_root()
    r = build(root, a.deep)
    if a.json:
        print(json.dumps(r, indent=2, default=list))
        return 0
    text = render(r)
    if a.stdout:
        print(text)
        return 0
    (root / a.out).write_text(text, encoding="utf-8")
    print("\n".join(["FINDINGS:"] + [f"  {i}. {x}" for i, x in enumerate(findings(r), 1)]))
    print(f"\nFull report: {root / a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
