"""
core/prompt_builder.py — Builds Eva's system prompt from soul.md + a compact
"known capabilities" summary of every registered skill.

Why this exists:
  skill_policy.py already grounds a task by keyword-matching and injecting
  the FULL matched skill's content into that one call. This is different —
  it's a standing, always-on summary (name + one-line description per
  skill, NOT full content) so Eva has baseline self-awareness of what she
  can do even before keyword-matching decides which skill is relevant.
  Think "I know I have a Blender skill" vs "here is the entire Blender
  skill's code, because this task matched it."

Confirmed against the real skills_registry.py (Aug 2026 upload): the index
lives at <skills_dir>/.registry_index.json, shaped:
    {
      "_schema_version": "1.0", "_saved_at": <ts>,
      "skills": {
        "fastapi_repo_8e11fa": {"path": "...", "metadata": {"source": "...", "type": "cloned", "cloned_at": <ts>}}
      }
    }
There is NO description field anywhere in metadata — it only ever holds
source/type/cloned_at/registered_at, or {"auto_detected": true}. So the
per-skill description here is pulled from the skill's own README via the
same fallback logic Skill.read() uses (first non-empty line, truncated),
not from the index.
"""

import json
import logging
from pathlib import Path
from functools import lru_cache

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # core/ -> project root
SOUL_PATH = PROJECT_ROOT / "soul.md"
SKILLS_DIR = PROJECT_ROOT / "workspace" / "skills"
INDEX_PATH = SKILLS_DIR / ".registry_index.json"

MAX_DESC_CHARS = 80  # keep each skill line short so the summary stays cheap

README_CANDIDATES = (
    "README.md", "readme.md", "README.rst", "README.txt",
    "README", "README.MD", "Readme.md", "README.markdown",
)


def _first_readme_line(skill_path: Path) -> str:
    """Best-effort: pull the first non-empty, non-heading-junk line from
    the skill's README as a one-line description. Mirrors Skill.read()'s
    README fallback search rather than depending on it, so this stays a
    read-only summarizer with no import coupling to skills_registry.py."""
    for candidate in README_CANDIDATES:
        p = skill_path / candidate
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip().lstrip("#").strip()
                    if line:
                        return line
            except (OSError, PermissionError, UnicodeDecodeError):
                continue
    return ""


def _summarize_skill(skill_id: str, entry: dict) -> str:
    # Base name: strip the "_repo_<hash>" / "_<n>" suffix _unique_name() adds
    import re
    name = re.sub(r'_repo_[0-9a-f]{4,}$', '', skill_id, flags=re.IGNORECASE)
    name = re.sub(r'_\d+$', '', name)

    path = Path(entry.get("path", SKILLS_DIR / skill_id))
    desc = _first_readme_line(path)
    if len(desc) > MAX_DESC_CHARS:
        desc = desc[:MAX_DESC_CHARS].rsplit(" ", 1)[0] + "…"
    return f"- {name}: {desc}" if desc else f"- {name}"


def load_soul(soul_path: Path = SOUL_PATH) -> str:
    if not soul_path.exists():
        logger.warning(f"soul.md not found at {soul_path} — proceeding without identity block")
        return ""
    return soul_path.read_text(encoding="utf-8").strip()


def build_capability_summary(index_path: Path = INDEX_PATH) -> str:
    if not index_path.exists():
        logger.warning(f"Skill index not found at {index_path} — capability summary empty")
        return ""
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to read skill index: {e}")
        return ""

    skills = data.get("skills", {})
    if not skills:
        return ""

    lines = [
        _summarize_skill(skill_id, entry)
        for skill_id, entry in sorted(skills.items())
        if not skill_id.startswith("_")
    ]
    return (
        "## Known capabilities (standing awareness, not per-task lookup)\n"
        "You have these skills registered and available right now — you don't need to "
        "\"discover\" them, they're already yours. skill_policy.py will pull the full "
        "content of whichever one actually matches a task; this list is just so you know "
        "your own range before that happens.\n\n"
        + "\n".join(lines)
    )


@lru_cache(maxsize=1)
def build_system_prompt(refresh: bool = False) -> str:
    """
    Cached by default since soul.md + the skill index change rarely relative
    to how often this gets called. Pass refresh=True (or call
    build_system_prompt.cache_clear() ) after editing soul.md or re-running
    skill discovery, so Eva doesn't run on a stale identity/capability list.
    """
    parts = [p for p in (load_soul(), build_capability_summary()) if p]
    return "\n\n".join(parts)


if __name__ == "__main__":
    print(build_system_prompt())
