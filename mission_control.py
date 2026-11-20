"""
Buddy OS Mission Control
Decompose objectives. Execute in parallel. Self-correct.
Fixed:
  • Skill API alignment (grep dicts, .exists, register_local/find_referenced)
  • Deadlock detection
  • Research context usage
  • Timeouts on task execution
  • Boolean conversion for needs_research
  • Absolute paths
"""

import os
import stat
import shutil
import asyncio
import ast
import re
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

from core.lessons_store import LessonsStore
from core.skill_policy import match_skills

BASE_DIR = Path(__file__).parent.parent  # project root
WORKSPACE_DIR = BASE_DIR / "workspace"
OUTPUT_DIR = WORKSPACE_DIR / "output"
CLONED_DIR = WORKSPACE_DIR / "cloned"

for d in [WORKSPACE_DIR, OUTPUT_DIR, CLONED_DIR]:
    d.mkdir(parents=True, exist_ok=True)


@dataclass
class Mission:
    id: str = field(default_factory=lambda: hashlib.sha256(datetime.now().isoformat().encode()).hexdigest()[:12])
    objective: str = ''
    status: str = 'pending'
    tasks: List[Dict] = field(default_factory=list)
    artifacts: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class MissionControl:
    def __init__(self, llm_client, model: str, workers: Dict, skills: Any, knowledge: Any,
                 guard: Any, lessons: Optional[Any] = None,
                 money_engine: Optional[Any] = None, skill_forge: Optional[Any] = None,
                 soul: Optional[Any] = None):
        self.client = llm_client
        self.model = model
        self.workers = workers
        self.skills = skills
        self.knowledge = knowledge
        self.guard = guard
        self.lessons = lessons or LessonsStore()
        self.money_engine = money_engine      # v8: revenue production inside missions
        self.skill_forge = skill_forge        # v8: create missing capabilities on demand
        self.soul = soul                      # v8.2: persona + memory journal
        self.missions: Dict[str, Mission] = {}
        self.semaphore = asyncio.Semaphore(5)

    def _remember(self, text: str):
        """Soul journal — best-effort; never break a mission over memories."""
        s = getattr(self, 'soul', None)
        if s:
            try:
                s.remember(text)
            except Exception:
                pass

    async def launch(self, objective: str, websocket=None) -> Mission:
        mission = Mission(objective=objective)
        self.missions[mission.id] = mission
        asyncio.create_task(self._run_mission(mission, websocket))
        return mission

    def _to_bool(self, value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")

    async def _run_mission(self, mission: Mission, websocket):
        mission.status = 'running'
        await self._log(mission, websocket, f'[Mission {mission.id}] Launched: {mission.objective}')

        try:
            tasks = await self._decompose(mission)
            mission.tasks = tasks
            await self._log(mission, websocket, f'[Mission {mission.id}] Decomposed into {len(tasks)} tasks')

            blocking = await self._preflight_check(tasks, mission, websocket)
            if blocking:
                mission.status = 'failed'
                await self._log(
                    mission, websocket,
                    f'[Mission {mission.id}] ABORTED (preflight) -- ' + '; '.join(blocking)
                )
                return

            gaps = [t.get('description', '') for t in tasks if self._to_bool(t.get('needs_research', False))]
            if gaps:
                await self._log(mission, websocket, f'[Mission {mission.id}] Researching {len(gaps)} knowledge gaps...')
                knowledge = await self.knowledge.gather(gaps[:3])
                mission.context.update(knowledge)
                await self._log(mission, websocket, f'[Mission {mission.id}] Research complete: {len(knowledge)} items')

            completed = set()
            while len(completed) < len(tasks):
                ready = [t for t in tasks if t['id'] not in completed and all(dep in completed for dep in t.get('depends_on', []))]
                if not ready:
                    if len(completed) < len(tasks):
                        mission.status = 'failed'
                        await self._log(
                            mission, websocket,
                            f'[Mission {mission.id}] ABORTED -- {len(tasks)-len(completed)} task(s) cannot be scheduled due to missing dependencies or cycles'
                        )
                        return
                    break

                batch = ready[:3]
                coros = [self._execute_task(t, mission, websocket) for t in batch]
                try:
                    results = await asyncio.gather(*coros, return_exceptions=True)
                except Exception as e:
                    mission.status = 'failed'
                    await self._log(mission, websocket, f'[Mission {mission.id}] CRITICAL FAILURE during batch execution: {e}')
                    return

                for t, r in zip(batch, results):
                    completed.add(t['id'])
                    is_failed = isinstance(r, Exception) or (isinstance(r, dict) and r.get('error'))
                    ttype = t.get('type', 'code')
                    desc = t.get('description', '')

                    if is_failed:
                        error_msg = str(r) if isinstance(r, Exception) else r.get('error', 'Unknown error')
                        lesson_id = self.lessons.record(ttype, desc, 'failure', detail=error_msg)
                        self._remember(f"mission task failed [{ttype}]: {desc[:80]}")
                        await self._log(mission, websocket, f"[Task {t['id']}] FAILED: {error_msg[:200]}")
                        await asyncio.sleep(1)

                        try:
                            retry = await asyncio.wait_for(self._execute_task(t, mission, websocket), timeout=300)
                        except asyncio.TimeoutError:
                            mission.status = 'failed'
                            await self._log(mission, websocket, f'[Mission {mission.id}] ABORTED -- retry timed out for task {t["id"]}')
                            return

                        retry_failed = isinstance(retry, Exception) or (isinstance(retry, dict) and retry.get('error'))
                        if retry_failed:
                            mission.status = 'failed'
                            retry_err = str(retry) if isinstance(retry, Exception) else retry.get('error', 'Unknown')
                            await self._log(mission, websocket, f'[Mission {mission.id}] ABORTED -- {retry_err}')
                            return
                        else:
                            r = retry
                            self.lessons.mark_fix(lesson_id, 'Succeeded on unmodified retry')

                    mission.artifacts[t['id']] = r
                    summary = r.get('summary', 'Done') if isinstance(r, dict) else 'Done'
                    self.lessons.record(ttype, desc, 'success', detail=summary)
                    self._remember(f"mission task done [{ttype}]: {desc[:80]}")
                    await self._log(mission, websocket, f"[Task {t['id']}] Complete: {summary}")

            mission.status = 'completed'
            self._remember(f"mission completed: {mission.objective[:70]}")
            await self._log(mission, websocket, f'[Mission {mission.id}] ALL TASKS COMPLETE')

        except Exception as e:
            mission.status = 'failed'
            await self._log(mission, websocket, f'[Mission {mission.id}] CRITICAL FAILURE: {e}')

    async def _preflight_check(self, tasks: List[Dict], mission: Mission, websocket) -> List[str]:
        checked: set = set()
        blockers: List[str] = []

        for task in tasks:
            if task.get('type') == 'git':
                continue

            desc = task.get('description', '')
            search_text = f"{desc} {mission.objective}"

            # Only an EXPLICIT skill reference (the task/objective actually
            # names a known skill) can block a mission here. match_skills()
            # is a fuzzy keyword scan across the whole registry — treating
            # its guesses as hard requirements meant plain-language tasks
            # with no real skill dependency ("write a poem", "say hello")
            # could get matched to some unrelated registered skill and
            # abort the entire mission just because that skill wasn't on
            # disk. A fuzzy hit alone is not grounds to block anything.
            skill = self.skills.find_referenced(search_text)
            is_explicit = skill is not None
            if not skill:
                matched = match_skills(search_text, self.skills)
                skill = matched[0] if matched else None

            if not skill or skill.name in checked:
                continue
            checked.add(skill.name)

            if skill.path.exists():
                continue

            if not is_explicit:
                # Fuzzy-only match on a missing skill: note it, don't block.
                await self._log(
                    mission, websocket,
                    f"[Preflight] Possible (unconfirmed) reference to skill '{skill.name}', "
                    f"which is missing on disk — proceeding without it since the task "
                    f"doesn't explicitly name it."
                )
                continue

            # Skill objects don't carry 'url'/'repo_url'/etc as direct attributes —
            # SkillRegistry.clone() saves the source URL inside skill.metadata['source'].
            # Check both direct attributes (in case a future Skill subclass adds them)
            # and metadata, so a genuinely cloned skill's saved URL is actually found.
            repo_url = None
            for attr in ('url', 'repo_url', 'source_url', 'clone_url', 'git_url'):
                val = getattr(skill, attr, None) or (skill.metadata or {}).get(attr)
                if val:
                    repo_url = val
                    break
            if not repo_url:
                meta = skill.metadata or {}
                source = meta.get('source')
                # metadata['source'] is only a re-clonable URL for actually-cloned
                # skills (type == 'cloned'); for register_local() skills it's a local
                # path string, which isn't something git can re-clone from.
                if source and meta.get('type') == 'cloned' and re.match(r'^https?://', str(source)):
                    repo_url = source

            if not repo_url:
                blockers.append(
                    f"Skill '{skill.name}' is registered but missing on disk "
                    f"(no source URL on record to re-clone — re-run its integration mission)"
                )
                continue

            await self._log(
                mission, websocket,
                f"[Preflight] Skill '{skill.name}' missing on disk — attempting auto re-clone from {repo_url}..."
            )
            target = Path(getattr(skill, 'path', CLONED_DIR / skill.name))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

            try:
                proc = await asyncio.create_subprocess_exec(
                    'git', '-c', 'core.longpaths=true',
                    'clone', '--depth', '1', repo_url, str(target),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            except asyncio.TimeoutError:
                blockers.append(f"Skill '{skill.name}' re-clone timed out")
                continue
            except Exception as e:
                blockers.append(f"Skill '{skill.name}' re-clone failed to start: {e}")
                continue

            if proc.returncode == 0 and target.exists():
                self.skills.register_local(target, name=skill.name)
                await self._log(mission, websocket, f"[Preflight] Re-cloned and re-registered '{skill.name}'.")
            else:
                err = stderr.decode()[:300] if stderr else f'git exited with code {proc.returncode}'
                blockers.append(f"Skill '{skill.name}' re-clone failed: {err}")

        return blockers

    def _skill_snippet(self, skill, desc: str, mission: Mission) -> List[str]:
        parts = [f"Skill: {skill.name} (path: {skill.path})"]

        for artifact in mission.artifacts.values():
            if isinstance(artifact, dict) and artifact.get('type') == 'analyze' \
                    and artifact.get('path') == str(skill.path):
                parts.append(f"Architecture summary:\n{artifact.get('summary', '')}")
                break

        keywords = [w.strip('.,:;()"\'') for w in desc.split() if len(w) > 4][:5]
        seen_files = set()
        for kw in keywords:
            for hit in skill.grep(kw, max_hits=3):
                file = hit.get('file')
                if file in seen_files:
                    continue
                seen_files.add(file)
                parts.append(f"{file}:{hit.get('line')}: {hit.get('content')}")
            if len(seen_files) >= 5:
                break

        return parts

    def _gather_skill_context(self, desc: str, mission: Mission, max_chars: int = 3000) -> str:
        search_text = f"{desc} {mission.objective}"

        explicit = self.skills.find_referenced(search_text)
        if explicit and explicit.path.exists():
            skills_to_use = [explicit]
        else:
            skills_to_use = match_skills(search_text, self.skills)

        if not skills_to_use:
            return ""

        # Ensure skills_to_use contains Skill objects, not strings
        normalized = []
        for s in skills_to_use:
            if isinstance(s, str):
                skill_obj = self.skills.get(s)
                if skill_obj:
                    normalized.append(skill_obj)
            else:
                normalized.append(s)
        skills_to_use = normalized

        if not skills_to_use:
            return ""

        per_skill_budget = max_chars // len(skills_to_use)
        all_parts: List[str] = []
        for skill in skills_to_use:
            snippet = "\n".join(self._skill_snippet(skill, desc, mission))
            all_parts.append(snippet[:per_skill_budget])

        return "\n\n".join(all_parts)[:max_chars]

    async def _decompose(self, mission: Mission) -> List[Dict]:
        pitfalls = self.lessons.as_prompt_context(limit=5)
        pitfalls_block = f"\n{pitfalls}\n" if pitfalls else ""
        soul_block = ""
        if getattr(self, 'soul', None):
            try:
                soul_block = "\n" + self.soul.prompt_block(max_chars=900) + "\n"
            except Exception:
                soul_block = ""
        prompt = f"""Decompose this objective into 3-8 atomic tasks. Return ONLY valid JSON.

{soul_block}
Objective: {mission.objective}
{pitfalls_block}
Each task must have:
- id: unique string
- type: code | image | video | web | game | presentation | research | analyze | git | money | forge
- description: what to do
- depends_on: list of task ids that must finish first (can be empty)
- needs_research: boolean, true if internet knowledge needed

Type notes:
- 'money': produce sellable assets. Description MUST use the exact format:
  "stream=<shorts|seo|products|stock|gigs>; topic=<what to produce about>; count=<N>"
  Use when the objective is about earning revenue, content for monetization, or products.
- 'forge': a capability the system lacks that should be created as a new skill.
  Description = plain-language capability description.

IMPORTANT: If the objective references a skill that is already cloned (e.g. mentions
skill "some_name" rather than a raw github.com/... URL), do NOT create a 'git' task —
the repository already exists on disk. Go straight to 'analyze' (and any other
relevant task types). Only use type 'git' when the objective contains an actual
repository URL that still needs to be cloned.

JSON format:
[{{'id': '1', 'type': 'git', 'description': 'Clone repo', 'depends_on': [], 'needs_research': false}}]
"""
        # client.chat.completions.create(...) is a BLOCKING sync call. Calling it
        # directly here freezes the entire asyncio event loop (and therefore the
        # whole websocket server, all clients, Chat Mode too) for the full
        # duration of decomposition. Run it in a thread executor instead so the
        # event loop stays responsive during every mission launch.
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self.client.chat.completions.create(
                model=self.model,
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.1,
                max_tokens=4000
            )
        )
        raw = resp.choices[0].message.content
        try:
            json_start = raw.find('[')
            json_end = raw.rfind(']') + 1
            tasks = json.loads(raw[json_start:json_end])
            for t in tasks:
                t['needs_research'] = self._to_bool(t.get('needs_research', False))
            return tasks
        except Exception:
            return [{'id': '1', 'type': 'code', 'description': mission.objective, 'depends_on': [], 'needs_research': False}]

    async def _execute_task(self, task: Dict, mission: Mission, websocket) -> Dict:
        async with self.semaphore:
            ttype = task.get('type', 'code')
            desc = task.get('description', '')

            # Add research context if available
            context_prefix = ""
            if mission.context:
                context_prefix = "Researched context:\n" + json.dumps(mission.context, indent=2) + "\n\n"

            if ttype == 'code':
                skill_context = self._gather_skill_context(desc, mission)
                full_desc = context_prefix + desc
                code = await self.workers['code'].generate(full_desc, context=skill_context)
                grounded = ' (grounded in integrated skill)' if skill_context else ''

                out_dir = OUTPUT_DIR / "code"
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"{mission.id}_{task['id']}.py"
                cleaned = re.sub(r'^```(?:python)?\s*\n?|\n?```\s*$', '', code.strip())
                out_path.write_text(cleaned, encoding='utf-8')

                return {
                    'type': 'code',
                    'content': cleaned,
                    'path': str(out_path),
                    'summary': f'Generated {len(cleaned)} chars of code{grounded} -> {out_path}'
                }

            elif ttype == 'web':
                files = await self.workers['web'].build(desc)
                for fname, fcontent in files.items():
                    path = OUTPUT_DIR / "web" / fname
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(fcontent, encoding='utf-8')
                return {'type': 'web', 'files': list(files.keys()), 'summary': f'Built web app with {len(files)} files'}

            elif ttype == 'game':
                files = await self.workers['game'].build(desc)
                for fname, fcontent in files.items():
                    path = OUTPUT_DIR / "game" / fname
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(fcontent, encoding='utf-8')
                return {'type': 'game', 'files': list(files.keys()), 'summary': f'Built game with {len(files)} files'}

            elif ttype == 'presentation':
                html = await self.workers['presentation'].build(desc)
                path = OUTPUT_DIR / "presentation.html"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(html, encoding='utf-8')
                return {'type': 'presentation', 'path': str(path), 'summary': 'Generated HTML presentation'}

            elif ttype == 'image':
                out = OUTPUT_DIR / f"image_{task['id']}.svg"
                result = await self.workers['image'].generate(desc, out)
                return {'type': 'image', 'path': result, 'summary': f'Generated image: {result}'}

            elif ttype == 'video':
                scenario = desc
                for artifact in mission.artifacts.values():
                    if isinstance(artifact, dict) and artifact.get('type') == 'analyze' \
                            and artifact.get('kind') == 'data_file' and artifact.get('raw_preview'):
                        scenario = artifact['raw_preview']
                        break
                result = await self.workers['video'].generate(scenario)
                return {'type': 'video', 'path': result, 'summary': f'Generated video: {result}'}

            elif ttype == 'research':
                result = await self.workers['research'].research(desc)
                return {'type': 'research', 'content': result, 'summary': 'Research complete'}

            elif ttype == 'git':
                url_match = re.search(r'(https?://\S+\.git)', desc)
                if not url_match:
                    url_match = re.search(r'(https?://github\.com/[^\s/]+/[^\s/]+)', desc)
                if not url_match:
                    url_match = re.search(r'(https?://\S+\.git)', mission.objective)
                if not url_match:
                    url_match = re.search(r'(https?://github\.com/[^\s/]+/[^\s/]+)', mission.objective)

                if not url_match:
                    search_text = f"{desc} {mission.objective}"
                    existing = self.skills.find_referenced(search_text)
                    if existing and existing.path.exists():
                        return {
                            'type': 'git',
                            'path': str(existing.path),
                            'skill': existing.name,
                            'summary': f'Skill "{existing.name}" already cloned — skipped redundant clone'
                        }
                    return {'type': 'git', 'error': 'No valid GitHub URL found in task or mission', 'summary': 'Clone failed'}

                repo_url = url_match.group(1)
                if not repo_url.endswith('.git'):
                    repo_url += '.git'

                repo_name = repo_url.split('/')[-1].replace('.git', '')
                target = CLONED_DIR / repo_name
                target.parent.mkdir(parents=True, exist_ok=True)

                if target.exists():
                    def on_rm_error(func, path, excinfo):
                        os.chmod(path, stat.S_IWRITE)
                        func(path)
                    shutil.rmtree(target, onerror=on_rm_error)
                    await self._log(mission, websocket, f'[Git] Cleared old clone at {target}')

                await self._log(mission, websocket, f'[Git] Cloning {repo_url} into {target}...')

                proc = await asyncio.create_subprocess_exec(
                    'git', '-c', 'core.longpaths=true',
                    'clone', '--depth', '1', repo_url, str(target),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                except asyncio.TimeoutError:
                    return {'type': 'git', 'error': 'Git clone timed out', 'summary': 'Clone failed'}

                if proc.returncode == 0:
                    await self._log(mission, websocket, '[Git] Clone successful. Integrating as skill...')
                    skill = self.skills.register_local(target, name=repo_name)
                    return {
                        'type': 'git',
                        'path': str(target),
                        'skill': skill.name,
                        'summary': f'Cloned & integrated as skill: {skill.name}'
                    }
                else:
                    err = stderr.decode()[:500] if stderr else f'Git exited with code {proc.returncode}'
                    await self._log(mission, websocket, f'[Git ERROR] {err}')
                    return {'type': 'git', 'error': err, 'summary': 'Clone failed'}

            elif ttype == 'analyze':
                repo_path = None
                for artifact in mission.artifacts.values():
                    if isinstance(artifact, dict) and artifact.get('type') == 'git' and artifact.get('path'):
                        repo_path = Path(artifact['path'])
                        break

                if not repo_path or not repo_path.exists():
                    search_text = f"{desc} {mission.objective}"
                    existing = self.skills.find_referenced(search_text)
                    if existing and existing.path.exists():
                        repo_path = existing.path

                if not repo_path or not repo_path.exists():
                    file_match = re.search(r'([A-Za-z0-9_\-]+\.(?:json|csv|txt|md|yaml|yml))', f"{desc} {mission.objective}")
                    if file_match:
                        fname = file_match.group(1)
                        hits = list(Path('.').rglob(fname))
                        if hits:
                            data_path = hits[0]
                            try:
                                raw = data_path.read_text(encoding='utf-8')
                            except Exception as e:
                                return {'type': 'analyze', 'error': f'Found {fname} but could not read it: {e}', 'summary': 'Analysis failed'}
                            preview = raw[:4000]
                            summary_prompt = f"""Summarize the structure and content of this data file in 3 bullet points.
File: {fname}
Content (may be truncated):
{preview}
"""
                            summary = await self._llm(summary_prompt, max_tokens=800)
                            return {
                                'type': 'analyze',
                                'kind': 'data_file',
                                'path': str(data_path),
                                'summary': summary[:500],
                                'raw_preview': preview
                            }

                if not repo_path or not repo_path.exists():
                    return {'type': 'analyze', 'error': 'No cloned repository or referenced data file found', 'summary': 'Analysis failed'}

                for artifact in mission.artifacts.values():
                    if isinstance(artifact, dict) and artifact.get('type') == 'analyze' \
                            and artifact.get('path') == str(repo_path):
                        cached = dict(artifact)
                        cached['summary'] = f"(cached) {cached.get('summary', '')}"
                        return cached

                py_files = list(repo_path.rglob('*.py'))
                structure = {
                    'total_py_files': len(py_files),
                    'top_modules': [f.name for f in repo_path.glob('*.py')],
                    'packages': [d.name for d in repo_path.iterdir() if d.is_dir() and not d.name.startswith('.')],
                    'key_classes': [],
                    'key_functions': []
                }

                for py_file in py_files[:20]:
                    try:
                        content = py_file.read_text(encoding='utf-8')
                        tree = ast.parse(content)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.ClassDef):
                                structure['key_classes'].append(node.name)
                            elif isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
                                structure['key_functions'].append(node.name)
                    except Exception:
                        continue

                snippet_budget = 4000
                snippets = []
                candidates = sorted(
                    [f for f in repo_path.glob('*.py')] or py_files[:10],
                    key=lambda f: f.stat().st_size,
                    reverse=True
                )
                for py_file in candidates[:5]:
                    if snippet_budget <= 0:
                        break
                    try:
                        content = py_file.read_text(encoding='utf-8')
                    except Exception:
                        continue
                    chunk = content[:1200]
                    snippets.append(f"--- {py_file.name} ---\n{chunk}")
                    snippet_budget -= len(chunk)

                code_excerpts = "\n\n".join(snippets) if snippets else "(no readable source files found)"

                summary_prompt = f"""Summarize this codebase in 3 bullet points, based on the actual code excerpts below (not just names):
Project: {repo_path.name}
Files: {structure['total_py_files']} Python files
Packages: {', '.join(structure['packages'][:10])}
Classes seen: {', '.join(structure['key_classes'][:15])}
Functions seen: {', '.join(structure['key_functions'][:15])}

Code excerpts:
{code_excerpts}
"""
                summary = await self._llm(summary_prompt, max_tokens=1000)

                return {
                    'type': 'analyze',
                    'structure': structure,
                    'summary': summary[:500],
                    'path': str(repo_path)
                }

            elif ttype == 'money':
                # v8: revenue production via the Money Engine.
                # Expected description format (from decompose prompt):
                #   "stream=<name>; topic=<...>; count=<N>"
                if not self.money_engine:
                    return {'type': 'money', 'error': 'Money Engine not attached to MissionControl',
                            'summary': 'Money task skipped'}
                stream_m = re.search(r'stream\s*[:=]\s*(\w+)', desc, re.I)
                count_m = re.search(r'count\s*[:=]\s*(\d+)', desc, re.I)
                topic_m = re.search(r'topic\s*[:=]\s*(.+?)(?:;\s*count|$)', desc, re.I | re.S)
                stream = stream_m.group(1).lower() if stream_m else 'products'
                count = int(count_m.group(1)) if count_m else 1
                topic = topic_m.group(1).strip() if topic_m else mission.objective

                results = await self.money_engine.produce(stream, topic, count=min(count, 5),
                                                          websocket=websocket)
                ok = [r for r in results if isinstance(r, dict) and r.get('status') == 'OK']
                if ok:
                    paths = [r.get('path', '') for r in ok if r.get('path')]
                    return {'type': 'money', 'stream': stream, 'results': results,
                            'summary': f'Produced {len(ok)} {stream} unit(s)'
                                       + (f' -> {paths[:3]}' if paths else '')}
                err = results[0].get('message') if results and isinstance(results[0], dict) else 'no output'
                return {'type': 'money', 'error': str(err)[:300], 'summary': f'{stream} production failed'}

            elif ttype == 'forge':
                # v8: create a brand-new skill for a capability the system lacks.
                if not self.skill_forge:
                    return {'type': 'forge', 'error': 'Skill Forge not attached to MissionControl',
                            'summary': 'Forge task skipped'}
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: self.skill_forge.forge(desc))
                if isinstance(result, dict) and result.get('status') == 'OK':
                    return {'type': 'forge', 'skill': result.get('skill'),
                            'path': result.get('path'),
                            'verified': result.get('verified', False),
                            'summary': f'Forged new skill "{result.get("skill")}" '
                                       f'({"verified" if result.get("verified") else "unverified"})'}
                err = result.get('message') if isinstance(result, dict) else str(result)
                return {'type': 'forge', 'error': str(err)[:300], 'summary': 'Skill forge failed'}

            else:
                return {'type': 'unknown', 'summary': f'Unknown task type: {ttype}'}

    async def _log(self, mission: Mission, websocket, message: str):
        mission.logs.append(message)
        print(message)
        if websocket and getattr(websocket, 'client_state', None) and websocket.client_state.name == 'CONNECTED':
            try:
                await websocket.send_text(message)
            except Exception:
                pass

    async def _llm(self, prompt: str, max_tokens: int = 4000) -> str:
        try:
            # Same blocking-call issue as _decompose: run the sync client call
            # in a thread executor so this doesn't stall the event loop during
            # every 'analyze' task's summary generation.
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self.client.chat.completions.create(
                    model=self.model,
                    messages=[{'role': 'user', 'content': prompt}],
                    temperature=0.2,
                    max_tokens=max_tokens
                )
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            return f'[LLM Error] {e}'
