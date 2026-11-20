import importlib.util
import os
import re
import sys
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")


def _project_root() -> Path:
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


class SkillBridge:
    def __init__(self, skills_dir="Skills"):
        p = Path(skills_dir)
        # anchored to the project root, not the current directory (the sandbox changes cwd)
        self.skills_dir = str((p if p.is_absolute() else _project_root() / p).resolve())
        self._cache = {}  # skill_name -> (mtime, module)

    def _load(self, skill_name: str, skill_path: str):
        mtime = os.path.getmtime(skill_path)
        cached = self._cache.get(skill_name)
        if cached and cached[0] == mtime:
            return cached[1]
        # Fresh module each time the file changes. (importlib.reload() cannot reload a module that was
        # loaded straight from a file path, so every second call to the same skill used to fail.)
        mod_name = f"buddy_skill_{skill_name}"
        spec = importlib.util.spec_from_file_location(mod_name, skill_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(mod_name, None)  # never leave a half-initialised module behind
            raise
        # keep the old bare-name registration, but never shadow a real/installed module
        if skill_name not in sys.modules and importlib.util.find_spec(skill_name) is None:
            sys.modules[skill_name] = module
        self._cache[skill_name] = (mtime, module)
        return module

    def execute_skill(self, skill_name: str, function_name: str, *args, **kwargs):
        """Dynamically invokes a function inside any local skill module."""
        if not isinstance(skill_name, str) or not _NAME_RE.match(skill_name):
            return {"status": "error", "message": f"Invalid skill name {skill_name!r}."}
        if not isinstance(function_name, str) or not function_name.isidentifier() or function_name.startswith("_"):
            return {"status": "error", "message": f"Invalid function name {function_name!r}."}
        skill_path = os.path.join(self.skills_dir, f"{skill_name}.py")
        if not os.path.exists(skill_path):
            return {"status": "error", "message": f"Skill '{skill_name}' not found."}
        try:
            module = self._load(skill_name, skill_path)
            func = getattr(module, function_name, None)
            if not callable(func):
                return {"status": "error", "message": f"Function '{function_name}' not found in skill '{skill_name}'."}
            return {"status": "success", "result": func(*args, **kwargs)}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}
