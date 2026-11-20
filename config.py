from __future__ import annotations

import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

def find_root() -> Path:
    env = os.environ.get("BUDDY_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent.parent
    for p in (here, *list(here.parents)[:3]):
        if (p / "workspace").is_dir() or (p / "server.py").exists() or (p / "config.yaml").exists():
            return p
    return here

class BuddyConfig:
    def __init__(self):
        self.ROOT: Path = find_root()
        self.CONFIG_PATH: Path = self.ROOT / "config.yaml"
        self._raw_config: dict = self._load_yaml()

    def _load_yaml(self) -> dict:
        if self.CONFIG_PATH.exists():
            try:
                with open(self.CONFIG_PATH, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[Config] Warning: Failed to parse config.yaml: {e}")
        return {}

    @property
    def database_path(self) -> Path:
        db_rel = self._raw_config.get("paths", {}).get("database", "knowledge_base/buddy_graph.db")
        path = (self.ROOT / db_rel).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def workspace_path(self) -> Path:
        ws_rel = self._raw_config.get("paths", {}).get("workspace", "workspace")
        path = (self.ROOT / ws_rel).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def ollama_host(self) -> str:
        return self._raw_config.get("models", {}).get("ollama_host", os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))

    def get(self, section: str, key: str, default=None):
        return self._raw_config.get(section, {}).get(key, default)

# Singleton instance across buddy_osv5
config = BuddyConfig()