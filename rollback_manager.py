"""
Buddy OS — Rollback Manager
===========================
Git-based rollback for safe execution with per-task snapshots.
"""

import subprocess
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("buddy_os.rollback")


class RollbackManager:
    """Git-based rollback for safe execution."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self._ensure_git()

    def _ensure_git(self):
        """Initialize git repo if needed."""
        if not (self.workspace / ".git").exists():
            subprocess.run(["git", "init"], cwd=str(self.workspace), capture_output=True)
            subprocess.run(["git", "config", "user.email", "buddy@os"], cwd=str(self.workspace), capture_output=True)
            subprocess.run(["git", "config", "user.name", "Buddy OS"], cwd=str(self.workspace), capture_output=True)
            # Initial commit
            subprocess.run(["git", "add", "-A"], cwd=str(self.workspace), capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.workspace), capture_output=True)
            logger.info(f"Initialized git repo at {self.workspace}")

    def snapshot(self, message: str = "Pre-execution snapshot") -> str:
        """Create a git commit as rollback point."""
        subprocess.run(["git", "add", "-A"], cwd=str(self.workspace), capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(self.workspace), capture_output=True, text=True
        )
        if result.returncode == 0:
            # Get commit hash
            hash_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.workspace), capture_output=True, text=True
            )
            commit_hash = hash_result.stdout.strip()
            logger.info(f"Snapshot created: {commit_hash[:8]} - {message}")
            return commit_hash
        logger.warning(f"Snapshot failed: {result.stderr}")
        return ""

    def rollback(self, commit_hash: str = None) -> Tuple[bool, str]:
        """Rollback to a commit (or previous if none specified)."""
        try:
            if commit_hash:
                result = subprocess.run(
                    ["git", "reset", "--hard", commit_hash],
                    cwd=str(self.workspace), capture_output=True, text=True
                )
            else:
                result = subprocess.run(
                    ["git", "reset", "--hard", "HEAD~1"],
                    cwd=str(self.workspace), capture_output=True, text=True
                )
            if result.returncode == 0:
                msg = f"Rolled back to {commit_hash or 'previous commit'}"
                logger.info(msg)
                return True, msg
            return False, result.stderr
        except Exception as e:
            return False, str(e)

    def get_status(self) -> Dict[str, Any]:
        """Get git status."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.workspace), capture_output=True, text=True
        )
        files = result.stdout.strip().split('\n') if result.stdout.strip() else []
        return {
            "clean": len(files) == 0,
            "changed_files": files,
            "count": len(files)
        }

    def list_snapshots(self, limit: int = 10) -> list:
        """List recent snapshots."""
        result = subprocess.run(
            ["git", "log", "--oneline", f"-{limit}"],
            cwd=str(self.workspace), capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip().split('\n')
        return []


# Integration helper for AutonomousEngine
def add_rollback_to_engine(engine):
    """Add rollback capabilities to AutonomousEngine instance."""
    engine.rollback_mgr = RollbackManager(engine.workspace)
    return engine


if __name__ == "__main__":
    # Quick test
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        rm = RollbackManager(Path(tmp))
        print("RollbackManager initialized")
        print(f"Status: {rm.get_status()}")
        snap = rm.snapshot("Test snapshot")
        print(f"Snapshot: {snap}")
        print(f"Snapshots: {rm.list_snapshots()}")