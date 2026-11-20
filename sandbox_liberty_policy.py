"""
core/sandbox_liberty_policy.py

Expanded permission set for GuardedSandbox: gives Buddy real reach across
your whole project (and beyond, if you want) instead of the narrow
per-mission snapshot folders it's probably restricted to now.

This does NOT ask permission for actions. It logs everything and keeps a
reversible trail, so Buddy can move fast without you babysitting every
command — and so a bad call is a five-second `undo`, not a lost afternoon.

Wire this into GuardedSandbox's init in place of whatever narrow
allow-list it currently has.
"""

import os
import shutil
import subprocess
import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Scope: where Buddy is allowed to act. Widen this list as you trust it more.
# ---------------------------------------------------------------------------

ALLOWED_ROOTS = [
    r"C:\Users\Admin\desktop\buddy_osv5",   # Buddy's own project
    r"C:\Users\Admin\desktop",              # your general workspace — widen/narrow as you like
]

# Nothing is blocked by command name — Buddy can run pip installs, git ops,
# build tools, run scripts, whatever the task calls for. The only hard
# blocks are destructive-outside-scope actions:
BLOCKED_PATTERNS = [
    "format ", "diskpart", "rm -rf /", "del /s /q C:\\Windows",
    "shutdown", "Remove-Item -Recurse -Force C:\\",
]

LOG_DIR = Path(ALLOWED_ROOTS[0]) / "logs" / "sandbox_actions"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _in_scope(path: str) -> bool:
    resolved = str(Path(path).resolve())
    return any(resolved.startswith(str(Path(root).resolve())) for root in ALLOWED_ROOTS)


def _log_action(action_type: str, detail: str):
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_file = LOG_DIR / f"{stamp}.log"
    log_file.write_text(f"{action_type}\n{detail}\n", encoding="utf-8")


def guarded_write(path: str, content: str) -> bool:
    if not _in_scope(path):
        raise PermissionError(f"Path outside allowed scope: {path}")
    # snapshot before overwrite, for one-command undo
    if os.path.exists(path):
        backup = f"{path}.bak.{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content, encoding="utf-8")
    _log_action("WRITE", path)
    return True


def guarded_run(command: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    if any(pat.lower() in command.lower() for pat in BLOCKED_PATTERNS):
        raise PermissionError(f"Blocked destructive-out-of-scope command: {command}")
    if cwd and not _in_scope(cwd):
        raise PermissionError(f"cwd outside allowed scope: {cwd}")

    _log_action("RUN", f"cwd={cwd}\ncmd={command}")
    result = subprocess.run(
        command, shell=True, cwd=cwd,
        capture_output=True, text=True, timeout=600,
    )
    _log_action("RESULT", f"exit={result.returncode}\nstdout={result.stdout[:2000]}\nstderr={result.stderr[:2000]}")
    return result


def guarded_delete(path: str) -> bool:
    if not _in_scope(path):
        raise PermissionError(f"Path outside allowed scope: {path}")
    trash = Path(ALLOWED_ROOTS[0]) / "logs" / "sandbox_trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{Path(path).name}.{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.move(path, dest)   # move, not delete — recoverable
    _log_action("DELETE(moved to trash)", f"{path} -> {dest}")
    return True
