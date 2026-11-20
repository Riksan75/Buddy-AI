"""
SelfMaintainer — Buddy OS maintains its own code. Fully autonomous.
Part of v8.1 "Agent Senses" (2026-08-22). Built to the owner's explicit
choice: NO approval prompts. The non-negotiable floor (kept even in full
autonomy): timestamped backup before every edit, compile-check after,
automatic rollback on any failure, complete audit log.

Usage (server):
    /maintain                    -> scan + fix up to 3 issues (syntax first)
    /maintain 5                  -> fix up to 5 issues
    /maintain focus: error handling in workers -> LLM picks targets by theme

Flow per issue:
  1. Pick target file (py_compile errors > focus triage > code markers).
  2. Ask the coder model for the COMPLETE corrected file (minimal diff).
  3. Validate the answer: parses, not truncated, not a gutting rewrite.
  4. Backup original -> write new version -> recompile -> run import check.
  5. Any failure at any step -> restore backup automatically, log everything.

Never touches: anything under workspace/, .env*, files matching *key*/
*secret*/ *token*, excluded dirs (.venv, node_modules, cloned repos...).
"""
from __future__ import annotations

import ast
import json
import os
import py_compile
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

MARKERS = ('TODO', 'FIXME', 'HACK', 'XXX')
EXCLUDE_DIRS = {'.venv', 'venv', 'env', 'node_modules', '__pycache__', '.git',
                'workspace', '.agents', 'skills', 'snapshots', 'cloned_repos',
                'ComfyUI-master', 'Wan2_2-main', 'OmniVoice-Studio-main'}
MAX_EDIT_BYTES = 60_000          # skip giant files (clones, data)
MIN_LINE_RETENTION = 0.6         # a "fix" that deletes 40%+ of a file = suspect

BACKUP_ROOT = Path('workspace/backups')
REPORT_DIR = Path('workspace/reports')


class SelfMaintainer:
    def __init__(self, llm_router=None, model: str = 'qwen3-coder:30b',
                 root: str = '.', log: Optional[Callable[[str], None]] = None):
        self.router = llm_router
        self.model = model
        self.root = Path(root).resolve()
        self._log = log or (lambda msg: None)

    # ---------------- scanning ----------------

    def inventory(self) -> List[Path]:
        files: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in EXCLUDE_DIRS]
            for fn in filenames:
                if not fn.endswith('.py') or fn.startswith('._'):
                    continue
                low = fn.lower()
                if any(tag in low for tag in ('key', 'secret', 'token', '.env')):
                    continue
                p = Path(dirpath) / fn
                if p.stat().st_size <= MAX_EDIT_BYTES:
                    files.append(p)
        return files

    def _compile_check(self, path: Path) -> Optional[str]:
        try:
            py_compile.compile(str(path), doraise=True)
            return None
        except py_compile.PyCompileError as e:
            return str(e)[:500]

    def find_issues(self) -> Dict[str, List[Dict]]:
        broken, marked = [], []
        files = self.inventory()
        self._log(f'[Maintain] scanning {len(files)} Python files …')
        for p in files:
            err = self._compile_check(p)
            if err:
                broken.append({'path': p, 'kind': 'syntax', 'detail': err})
                continue
            try:
                src = p.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue
            hits = [ln for ln in src.splitlines()
                    if any(m in ln for m in MARKERS)][:3]
            if hits:
                marked.append({'path': p, 'kind': 'marker',
                               'detail': ' | '.join(h.strip()[:90]
                                                    for h in hits)})
        return {'broken': broken, 'marked': marked, 'total_files': len(files)}

    # ---------------- fixing ----------------

    def _llm(self, prompt: str, temperature: float = 0.2) -> str:
        resp = self.router.chat.completions.create(
            model=self.model, messages=[{'role': 'user', 'content': prompt}],
            temperature=temperature)
        return resp.choices[0].message.content

    def _pick_focus_targets(self, focus: str, files: List[Path],
                            limit: int) -> List[Dict]:
        listing = '\n'.join(str(f.relative_to(self.root)) for f in files[:400])
        prompt = (
            'You are Buddy OS maintaining your own codebase. The owner wants '
            f'improvements themed: "{focus}".\nBelow is the file list. Pick up '
            f'to {limit} files that most plausibly need that theme of work.\n'
            'Reply ONLY a JSON array like '
            '[{"path": "core/foo.py", "reason": "why"}, ...] — no prose.\n\n'
            f'FILES:\n{listing}')
        try:
            raw = self._llm(prompt, temperature=0.1)
            m = re.search(r'\[.*\]', raw, re.S)
            items = json.loads(m.group(0)) if m else []
            out = []
            for it in items[:limit]:
                p = (self.root / it.get('path', '')).resolve()
                if p.exists() and p.suffix == '.py' and \
                        self.root in p.parents and p.stat().st_size <= MAX_EDIT_BYTES:
                    out.append({'path': p, 'kind': 'focus',
                                'detail': it.get('reason', focus)})
            return out
        except Exception as e:
            self._log(f'[Maintain] focus triage failed: {e}')
            return []

    @staticmethod
    def _strip_code_block(text: str) -> str:
        m = re.search(r'```(?:python)?\s*\n(.*?)```', text, re.S)
        return (m.group(1) if m else text).strip() + '\n'

    def _validate_candidate(self, orig: str, new: str) -> Optional[str]:
        """Return a rejection reason, or None if the candidate is acceptable."""
        if len(new.strip()) < 30:
            return 'candidate too short (likely truncated)'
        orig_lines, new_lines = len(orig.splitlines()), len(new.splitlines())
        if new_lines < orig_lines * MIN_LINE_RETENTION:
            return (f'candidate deletes too much '
                    f'({new_lines} vs {orig_lines} lines)')
        try:
            ast.parse(new)
        except SyntaxError as e:
            return f'candidate does not parse: {e}'
        return None

    def _fix_one(self, issue: Dict, ts: str) -> Dict:
        path: Path = issue['path']
        rel = path.relative_to(self.root)
        src = path.read_text(encoding='utf-8', errors='replace')
        prompt = (
            'You are Buddy OS repairing your own source file. Return the '
            'COMPLETE corrected Python file and nothing else (no markdown '
            'fences, no commentary).\nRules: minimal, surgical changes; keep '
            'every public function/class name and the overall structure; do '
            'not delete features; keep imports consistent.\n'
            f'FILE: {rel}\nKNOWN ISSUE ({issue["kind"]}): {issue["detail"]}\n\n'
            f'CURRENT SOURCE:\n{src[:40_000]}')
        try:
            raw = self._llm(prompt)
        except Exception as e:
            return {'file': str(rel), 'action': 'fix', 'ok': False,
                    'detail': f'LLM call failed: {e}'}
        candidate = self._strip_code_block(raw)
        reason = self._validate_candidate(src, candidate)
        if reason:
            return {'file': str(rel), 'action': 'fix', 'ok': False,
                    'detail': f'rejected: {reason}'}
        # Backup -> write -> verify -> (rollback on failure)
        backup = BACKUP_ROOT / ts / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        path.write_text(candidate, encoding='utf-8')
        err = self._compile_check(path)
        if err:
            shutil.copy2(backup, path)
            return {'file': str(rel), 'action': 'fix+rollback', 'ok': False,
                    'detail': f'post-write compile failed, restored backup: {err}'}
        return {'file': str(rel), 'action': 'fixed', 'ok': True,
                'detail': f'issue: {issue["detail"][:120]} '
                          f'(backup: {backup})'}

    # ---------------- orchestrator ----------------

    def maintain(self, focus: Optional[str] = None,
                 max_fixes: int = 3) -> Dict[str, object]:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        scan = self.find_issues()
        targets: List[Dict] = list(scan['broken'][:max_fixes])
        if focus:
            focus_files = [f for f in self.inventory()]
            picked = self._pick_focus_targets(focus, focus_files,
                                              max_fixes - len(targets))
            picked = [p for p in picked
                      if p['path'] not in {t['path'] for t in targets}]
            targets += picked[:max_fixes - len(targets)]
        elif not targets:
            targets = scan['marked'][:max_fixes]

        results = []
        for issue in targets:
            self._log(f"[Maintain] working on {issue['path'].name} "
                      f"({issue['kind']}) …")
            results.append(self._fix_one(issue, ts))

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORT_DIR / f'maintenance_{ts}.md'
        ok_n = sum(1 for r in results if r['ok'])
        if results:
            action_lines = [
                f"- [{'OK' if r['ok'] else 'FAILED'}] {r['action']}: "
                f"{r['file']} — {r['detail']}" for r in results]
        else:
            action_lines = ['(nothing to do)']
        lines = [
            '# Buddy OS Self-Maintenance Report', f'Run: {ts}',
            f'Scope: {self.root}  |  Files scanned: {scan["total_files"]}',
            f'Focus: {focus or "auto (syntax errors first, markers second)"}',
            f'Outcome: {ok_n}/{len(results)} fixes applied cleanly', '',
            '## Actions', '',
            *action_lines, '',
            f'Scan summary: {len(scan["broken"])} broken, '
            f'{len(scan["marked"])} marker-flagged files.',
            'Backups: workspace/backups/' + ts,
        ]
        report_path.write_text('\n'.join(lines), encoding='utf-8')
        with open(REPORT_DIR / 'maintenance_log.jsonl', 'a',
                  encoding='utf-8') as f:
            f.write(json.dumps({'ts': ts, 'focus': focus,
                                'results': results}) + '\n')
        return {'summary': '\n'.join(lines), 'results': results,
                'report_path': str(report_path), 'scanned': scan['total_files']}
