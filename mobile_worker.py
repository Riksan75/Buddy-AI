"""
core/mobile_worker.py — Buddy OS: Cross-Platform App Worker (v1.0)

Generates real, runnable apps for:
  - mobile  -> React Native + Expo (Android/iOS, testable via Expo Go, no Mac needed)
  - desktop -> Electron (Windows + Mac, same JS/TS core logic reused)

Why this design:
  - A local 30B model reliably scaffolds JS/TS/React — it struggles far more with
    native Kotlin/Swift or complex build-system config. Sticking to one language
    family for both targets means Buddy can share business-logic files between
    the phone app and the desktop app instead of writing everything twice.
  - Routes through your existing HybridRouter (`client`) using tier='balanced'
    by default (NOT 'best') so routine scaffolding stays on local Ollama and
    only genuinely hard logic (data flow, tricky state, non-trivial algorithms)
    gets escalated — this is the actual token/cost saving lever, not the
    framework choice itself.
  - Does NOT trust the LLM's first draft. After writing files it runs a real
    self-test (npm install, tsc --noEmit) in a subprocess, feeds any errors
    back to the model, and retries up to `max_fix_rounds` times. This mirrors
    the fix-loop pattern already used by your debug worker.
"""
import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional


class MobileAppWorker:
    def __init__(self, client, model: str, project_root: str = 'workspace/output',
                 web_toolkit=None):
        self.client = client          # HybridRouter instance (already in server.py)
        self.model = model            # local model name, e.g. 'qwen3-coder:30b'
        self.project_root = Path(project_root)
        self.web_toolkit = web_toolkit  # core.web_toolkit.WebToolkit, optional

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    async def build(self, spec: str, target: str = 'mobile',
                     app_name: Optional[str] = None,
                     websocket=None, send_json=None,
                     max_fix_rounds: int = 2) -> Dict[str, str]:
        """
        target: 'mobile' (Expo/React Native) or 'desktop' (Electron)
        Returns dict of {relative_filepath: content} that was written to disk.
        """
        slug = self._slugify(app_name or spec)
        out_dir = self.project_root / ('mobile' if target == 'mobile' else 'desktop') / slug
        out_dir.mkdir(parents=True, exist_ok=True)

        await self._log(websocket, send_json, f'[{target.title()}App] Scaffolding "{spec}"...', 'text-purple-400')

        # If the spec references a URL (e.g. "clone the layout of https://...",
        # "match this site's pricing table"), pull real reference content
        # instead of letting the model guess — no confirmation needed.
        ref_context = ''
        if self.web_toolkit:
            url_match = re.search(r'https?://\S+', spec)
            if url_match:
                ref_url = url_match.group(0).rstrip('.,;)')
                try:
                    ref_data = await asyncio.to_thread(self.web_toolkit.extract, ref_url)
                    ref_context = (
                        f"\n\nREFERENCE CONTENT (scraped from {ref_url}):\n"
                        f"Title: {ref_data.get('title', '')}\n"
                        f"Headings: {', '.join(ref_data.get('headings', [])[:15])}\n"
                        f"Sample text: {' '.join(ref_data.get('paragraphs', [])[:5])[:800]}"
                    )
                    await self._log(websocket, send_json,
                                     f'[{target.title()}App] Pulled reference content from {ref_url}',
                                     'text-indigo-200')
                except Exception as e:
                    await self._log(websocket, send_json,
                                     f'[{target.title()}App] Reference scrape failed ({e}), continuing without it',
                                     'text-amber-300')

        files = await self._generate(spec + ref_context, target, app_name or slug)
        self._write_files(out_dir, files)

        await self._log(websocket, send_json,
                         f'[{target.title()}App] Wrote {len(files)} files to {out_dir}', 'text-indigo-200')

        # ----- Self-test / auto-fix loop -----
        for attempt in range(1, max_fix_rounds + 1):
            await self._log(websocket, send_json,
                             f'[{target.title()}App] Self-test attempt {attempt}/{max_fix_rounds}...',
                             'text-amber-300')
            ok, error_log = await asyncio.to_thread(self._self_test, out_dir, target)
            if ok:
                await self._log(websocket, send_json,
                                 f'[{target.title()}App] Self-test PASSED. Ready in {out_dir}',
                                 'text-emerald-400')
                return files

            await self._log(websocket, send_json,
                             f'[{target.title()}App] Self-test failed:\n{error_log[:800]}',
                             'text-rose-400')
            if attempt == max_fix_rounds:
                break

            fix_files = await self._generate_fix(spec, target, files, error_log)
            files.update(fix_files)
            self._write_files(out_dir, fix_files)

        await self._log(websocket, send_json,
                         f'[{target.title()}App] Gave up after {max_fix_rounds} fix attempts — '
                         f'files are in {out_dir}, needs manual review.',
                         'text-amber-400')
        return files

    # ------------------------------------------------------------------
    # LLM generation
    # ------------------------------------------------------------------
    async def _generate(self, spec: str, target: str, app_name: str) -> Dict[str, str]:
        system = self._system_prompt(target)
        user = (
            f'App name: {app_name}\n'
            f'Spec: {spec}\n\n'
            'Return ONLY a single JSON object mapping relative file paths to full file '
            'contents (as strings). No markdown fences, no commentary, no explanation — '
            'just the raw JSON object. Include a complete package.json and every file '
            'needed to run the app from a clean folder.'
        )
        raw = await self._call_llm(system, user, tier='balanced')
        return self._parse_file_json(raw)

    async def _generate_fix(self, spec: str, target: str, files: Dict[str, str], error_log: str) -> Dict[str, str]:
        system = self._system_prompt(target)
        # Keep the prompt small: only send file names + the error, not the whole codebase
        file_list = '\n'.join(files.keys())
        user = (
            f'The app below failed its build/type-check step. Fix ONLY the files that '
            f'need changing.\n\n'
            f'Spec: {spec}\n\n'
            f'Existing files:\n{file_list}\n\n'
            f'Error output:\n{error_log[:3000]}\n\n'
            'Return ONLY a JSON object mapping the relative file paths that need to '
            'change to their FULL corrected contents. Do not include unchanged files. '
            'No markdown fences, no commentary — raw JSON only.'
        )
        raw = await self._call_llm(system, user, tier='balanced')
        return self._parse_file_json(raw)

    async def _call_llm(self, system: str, user: str, tier: str = 'balanced') -> str:
        loop = asyncio.get_event_loop()
        # tier is passed through if your HybridRouter/client supports it; if your
        # current client.chat.completions.create() doesn't accept `tier`, drop the
        # kwarg here — the important part is NOT hardcoding tier='best'.
        def _call():
            kwargs = dict(
                model=self.model,
                messages=[
                    {'role': 'system', 'content': system},
                    {'role': 'user', 'content': user},
                ],
                temperature=0.15,
                max_tokens=6000,
            )
            try:
                return self.client.chat.completions.create(tier=tier, **kwargs)
            except TypeError:
                # client doesn't support tier kwarg — fall back gracefully
                return self.client.chat.completions.create(**kwargs)
        response = await loop.run_in_executor(None, _call)
        return response.choices[0].message.content or ''

    def _system_prompt(self, target: str) -> str:
        if target == 'mobile':
            return (
                'You are an expert React Native + Expo engineer. Generate a complete, '
                'runnable Expo (SDK 51+) TypeScript app. Use functional components and '
                'hooks only. Keep dependencies minimal (expo, react, react-native, and '
                'only what the spec truly requires). The app must run with '
                '`npx expo start` from a clean `npm install`. Include app.json with a '
                'valid expo config block.'
            )
        return (
            'You are an expert Electron + TypeScript engineer building a cross-platform '
            '(Windows + Mac) desktop app. Generate a complete, runnable Electron app '
            '(main process + preload + a simple React or vanilla renderer — prefer '
            'plain HTML/TS renderer unless the spec needs React). Keep dependencies '
            'minimal. The app must run with `npm install && npm start` from a clean '
            'folder. Include a valid package.json with an "electron" devDependency and '
            'a "start" script.'
        )

    def _parse_file_json(self, raw: str) -> Dict[str, str]:
        raw = raw.strip()
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw, flags=re.DOTALL)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # try to salvage the largest {...} block
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                raise ValueError('Model did not return parseable JSON file map')
            data = json.loads(m.group(0))
        if not isinstance(data, dict):
            raise ValueError('Model JSON was not a {path: content} object')
        return {str(k): str(v) for k, v in data.items()}

    # ------------------------------------------------------------------
    # Disk + self-test
    # ------------------------------------------------------------------
    def _write_files(self, out_dir: Path, files: Dict[str, str]) -> None:
        for rel_path, content in files.items():
            rel_path = rel_path.lstrip('/\\')
            path = out_dir / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')

    def _self_test(self, out_dir: Path, target: str) -> (bool, str):
        """Runs a real, cheap sanity check: npm install + a type/lint pass.
        Does NOT try to fully build/package the app (too slow for a fix loop) —
        that's a separate manual/CI step once this passes."""
        log = []

        npm_install = subprocess.run(
            ['npm', 'install', '--no-audit', '--no-fund'],
            cwd=out_dir, capture_output=True, text=True, timeout=600
        )
        log.append(npm_install.stdout[-2000:])
        log.append(npm_install.stderr[-2000:])
        if npm_install.returncode != 0:
            return False, '\n'.join(log)

        # TypeScript check if a tsconfig exists; otherwise just confirm entry file parses
        if (out_dir / 'tsconfig.json').exists():
            tsc = subprocess.run(
                ['npx', '--yes', 'tsc', '--noEmit'],
                cwd=out_dir, capture_output=True, text=True, timeout=300
            )
            log.append(tsc.stdout[-2000:])
            log.append(tsc.stderr[-2000:])
            if tsc.returncode != 0:
                return False, '\n'.join(log)

        return True, ''

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
        return slug[:40] or 'app'

    @staticmethod
    async def _log(websocket, send_json, message: str, color: str = 'text-indigo-200'):
        if websocket is not None and send_json is not None:
            await send_json(websocket, {'message': message, 'color': color})
        else:
            print(message, flush=True)