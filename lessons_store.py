"""
Buddy OS — Lessons Store
=========================
Persistent memory of what worked and what failed, keyed by task type.
MissionControl consults this before decomposing/executing tasks so the
agent stops repeating the same mistakes across missions.

This is the self-improvement loop: every failed task gets recorded with
its error; every mission's decomposition step recalls recent failures
and folds them into the planning prompt as "known pitfalls to avoid."
"""

import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


class LessonsStore:
    def __init__(self, db_path: str = "workspace/.buddy_lessons.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS lessons (
                id TEXT PRIMARY KEY,
                task_type TEXT,
                description TEXT,
                outcome TEXT,
                detail TEXT,
                fix TEXT,
                timestamp TEXT,
                occurrences INTEGER DEFAULT 1
            )
        """)
        conn.commit()
        conn.close()

    def record(self, task_type: str, description: str, outcome: str,
               detail: str = '', fix: str = '') -> str:
        """
        Record a task outcome. outcome should be 'success' or 'failure'.
        Same (task_type, description, detail) combo bumps occurrences
        instead of duplicating, so recurring mistakes surface as repeat
        offenders rather than flooding the table.
        """
        lesson_id = hashlib.md5(f"{task_type}:{description}:{detail}".encode()).hexdigest()[:16]
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT occurrences FROM lessons WHERE id = ?", (lesson_id,))
        row = c.fetchone()
        occurrences = (row[0] + 1) if row else 1
        c.execute("""
            INSERT OR REPLACE INTO lessons
                (id, task_type, description, outcome, detail, fix, timestamp, occurrences)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (lesson_id, task_type, description[:500], outcome,
              detail[:1000], fix[:1000], datetime.now().isoformat(), occurrences))
        conn.commit()
        conn.close()
        return lesson_id

    def recall(self, task_type: str, limit: int = 5) -> List[Dict]:
        """Most recent failures for a given task type, newest first."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            SELECT description, detail, fix, occurrences, timestamp FROM lessons
            WHERE task_type = ? AND outcome = 'failure'
            ORDER BY occurrences DESC, timestamp DESC LIMIT ?
        """, (task_type, limit))
        rows = c.fetchall()
        conn.close()
        return [
            {"description": d, "detail": det, "fix": f, "occurrences": occ, "timestamp": ts}
            for d, det, f, occ, ts in rows
        ]

    def recall_all_types(self, limit: int = 5) -> List[Dict]:
        """Top recurring failures across every task type — used to brief the planner."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            SELECT task_type, description, detail, fix, occurrences FROM lessons
            WHERE outcome = 'failure'
            ORDER BY occurrences DESC, timestamp DESC LIMIT ?
        """, (limit,))
        rows = c.fetchall()
        conn.close()
        return [
            {"task_type": tt, "description": d, "detail": det, "fix": f, "occurrences": occ}
            for tt, d, det, f, occ in rows
        ]

    def as_prompt_context(self, limit: int = 5) -> str:
        """Render recurring failures as short prompt text so the planner avoids repeat mistakes."""
        lessons = self.recall_all_types(limit=limit)
        if not lessons:
            return ""
        lines = ["Known pitfalls from past missions (do not repeat these):"]
        for l in lessons:
            entry = f"- [{l['task_type']}] {l['description'][:120]} -> failed with: {l['detail'][:150]}"
            if l['fix']:
                entry += f" | worked instead: {l['fix'][:150]}"
            if l['occurrences'] > 1:
                entry += f" (seen {l['occurrences']}x)"
            lines.append(entry)
        return "\n".join(lines)

    def mark_fix(self, lesson_id: str, fix: str):
        """Record what actually resolved a failure, once known (e.g. after a successful retry)."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE lessons SET fix = ? WHERE id = ?", (fix, lesson_id))
        conn.commit()
        conn.close()

    def stats(self) -> Dict[str, int]:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT outcome, COUNT(*) FROM lessons GROUP BY outcome")
        rows = dict(c.fetchall())
        conn.close()
        return {"failures": rows.get("failure", 0), "successes": rows.get("success", 0)}
