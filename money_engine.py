"""
Buddy OS v8 — Money Engine
==========================
Turns Buddy's production power into daily earning workflows.

Honest framing (this matters):
  - Buddy produces REAL assets: videos, articles, digital products, stock
    packs, gig proposals. Money arrives when those are published on real
    platforms and real humans buy/watch/hire. There is no magic here —
    there is leverage: the machine does the grinding, you do the approving
    and uploading where platforms require a human account.
  - The ComplianceGuard exists because banned accounts earn $0. No spam,
    no fake claims, no undisclosed affiliate links, no fake reviews.

Architecture:
    MoneyEngine
      ├── EarningsLedger        (core/earnings_ledger.py) — books & stats
      ├── ComplianceGuard       — blocks deceptive/spammy content pre-ship
      ├── DailyPlanner          — picks today's highest-EV production plan
      └── Streams
            ├── shorts     -> ShortFormStream      (videos via ShortsWorker)
            ├── seo        -> SEOContentStream     (keyword-researched articles)
            ├── products   -> DigitalProductStream (ebooks, templates, packs)
            ├── stock      -> StockAssetStream     (SVG/image packs)
            └── gigs       -> GigProposalStream    (listings + proposal drafts)
"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.earnings_ledger import EarningsLedger
from core.video_factory import VideoFactory

OUTPUT_ROOT = Path('workspace/output/money')


# ===========================================================================
# Compliance guard — the difference between an income machine and a ban machine
# ===========================================================================
class ComplianceGuard:
    """Heuristic first pass + LLM second pass. Blocks obvious money-losers."""

    RED_FLAGS = [
        (r'\b(guaranteed|guarantee)\s+(income|money|earnings|profit)\b',
         'Guaranteed-income claims violate ad/platform policies'),
        (r'\b(earn|make)\s+\$?\d+[k]?\s+(a|per)\s+day\b(?!.{0,40}\b(results not typical|individual results)\b)',
         'Specific income claims need disclaimers'),
        (r'\b(cure|cures|miracle (cure|remedy)|cancer cure)\b',
         'Medical claims can get content removed and are harmful'),
        (r'\b(buy (real|active) followers|buy subscribers|fake engagement)\b',
         'Platform manipulation — bannable'),
        (r'\b(clickbank|hustle) hack\b', 'Spam-pattern phrasing'),
    ]

    REQUIRED_DISCLOSURE_HINTS = [
        (r'#?ad\b|affiliate link|commission', 'affiliate'),
    ]

    AFFILIATE_DISCLOSURE = (
        'Disclosure: this content contains affiliate links. '
        'We may earn a commission at no extra cost to you.'
    )

    def __init__(self, llm_router=None):
        self.router = llm_router

    def check(self, text: str, kind: str = 'article') -> Dict[str, Any]:
        violations, warnings, disclosures = [], [], []

        for pattern, reason in self.RED_FLAGS:
            if re.search(pattern, text or '', re.I):
                violations.append(reason)

        for pattern, tag in self.REQUIRED_DISCLOSURE_HINTS:
            if re.search(pattern, text or '', re.I) and 'affiliate' in text.lower():
                if self.AFFILIATE_DISCLOSURE.split(':')[1].strip()[:20] not in text:
                    disclosures.append(self.AFFILIATE_DISCLOSURE)

        # Length sanity: ultra-thin content is spam by definition
        if kind in ('article', 'description') and len(text or '') < 300:
            warnings.append('Content is very thin (<300 chars) — expand before publishing')

        return {'allowed': not violations,
                'violations': violations,
                'warnings': warnings,
                'disclosures': disclosures}

    async def review(self, text: str, kind: str = 'article') -> Dict[str, Any]:
        """Full review = heuristics + one LLM policy pass (if router available)."""
        base = self.check(text, kind)
        if not self.router or not base['allowed']:
            return base
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: self.router.chat.completions.create(
                    model=None,
                    messages=[{'role': 'user', 'content':
                        f'You are a platform-policy reviewer. Review this {kind} for: spam, '
                        f'deceptive claims, undisclosed sponsorship, impersonation, '
                        f'medical/financial misinformation. Reply ONLY JSON: '
                        f'{{"ok": true/false, "issues": ["..."], "fixes": ["..."]}}\n\n'
                        f'{text[:6000]}'}],
                    temperature=0.0, max_tokens=500, tier='cloud',
                ))
            raw = resp.choices[0].message.content or ''
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                verdict = json.loads(m.group(0))
                base['llm_ok'] = verdict.get('ok', True)
                base['warnings'] += [str(i) for i in verdict.get('issues', [])][:5]
                base['allowed'] = base['allowed'] and bool(verdict.get('ok', True))
        except Exception:
            pass  # heuristic result stands if LLM review unavailable
        return base


# ===========================================================================
# Revenue streams
# ===========================================================================
class _StreamBase:
    name = 'base'
    label = 'Base Stream'
    description = ''
    effort = 'medium'                 # low | medium | high
    time_to_first_dollar = 'weeks'

    def __init__(self, engine: 'MoneyEngine'):
        self.engine = engine
        self.router = engine.router
        self.workers = engine.workers
        self.ledger = engine.ledger
        self.guard = engine.guard

    async def _llm(self, prompt: str, max_tokens: int = 4000, tier: Optional[str] = None) -> str:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self.router.chat.completions.create(
                model=None, messages=[{'role': 'user', 'content': prompt}],
                temperature=0.4, max_tokens=max_tokens, tier=tier)
        )
        text = resp.choices[0].message.content or ''
        # qwen3-family models sometimes emit reasoning blocks that break JSON
        # parsing downstream — strip them wherever they appear.
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _out_dir(self, sub: str) -> Path:
        d = OUTPUT_ROOT / self.name / sub / datetime.now().strftime('%Y%m%d')
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def log(self, message: str, color: str = 'text-emerald-300'):
        await self.engine.log(message, color)


class ShortFormStream(_StreamBase):
    """Batch-produce vertical shorts via the existing ShortsWorker pipeline."""
    name = 'shorts'
    label = 'Short-Form Video Factory'
    description = 'TikTok / Reels / YouTube Shorts batches with script, voiceover, b-roll, captions.'
    effort = 'medium'
    time_to_first_dollar = '2-8 weeks (monetization thresholds)'

    async def produce(self, niche: str, count: int = 3, websocket=None) -> List[Dict]:
        await self.log(f'[Shorts] Queueing {count} videos on niche "{niche}"...', 'text-purple-400')
        worker = self.workers.get('shorts')
        if not worker:
            return [{'status': 'ERROR', 'message': 'ShortsWorker not available'}]
        await worker.create_batch(niche, count, websocket)
        results = []
        out_dir = Path('generated_videos')
        if out_dir.exists():
            vids = sorted(out_dir.glob('*.mp4'), key=lambda p: p.stat().st_mtime)[-count:]
            for v in vids:
                entry = self.ledger.add_produced(
                    self.name, 'video', v.stem, path=str(v),
                    meta={'niche': niche})
                results.append({'status': 'OK', 'entry_id': entry['id'],
                                'path': str(v), 'title': v.stem})
        return results


class SEOContentStream(_StreamBase):
    """Keyword-targeted articles, researched then written, saved as markdown."""
    name = 'seo'
    label = 'SEO Content Forge'
    description = 'Long-tail keyword articles for blogs / Medium / niche sites (adsense + affiliate).'
    effort = 'low'
    time_to_first_dollar = '2-6 months (SEO ramp) — fastest as freelance writing instead'

    async def produce(self, topic: str, count: int = 1, websocket=None) -> List[Dict]:
        results = []
        research_ctx = ''
        try:
            rw = self.workers.get('research')
            if rw:
                await self.log(f'[SEO] Researching "{topic}"...')
                research_ctx = await rw.research(
                    f'long-tail keywords, search intent, and current angles for: {topic}')
        except Exception:
            pass

        for i in range(count):
            angle = ''
            if count > 1:
                angle = f' (distinct angle #{i+1}, do NOT repeat earlier angles)'
            prompt = f"""Write a complete SEO-optimized blog article.

TOPIC: {topic}{angle}
RESEARCH CONTEXT:
{research_ctx[:2500]}

Requirements:
- Compelling title (with primary keyword naturally placed)
- Meta description (155 chars max)
- 1200-1800 words, H2/H3 structure, short paragraphs
- FAQ section (3 questions)
- Natural placement for affiliate opportunities marked with [AFFILIATE: product type]
- Honest tone: no guaranteed-income claims, no medical/cure claims

Return JSON only: {{"title": "...", "meta_description": "...", "keywords": ["..."],
"article_markdown": "..."}}"""
            raw = await self._llm(prompt, max_tokens=6000, tier='cloud')
            try:
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                data = json.loads(m.group(0))
            except Exception:
                data = {'title': f'{topic} — part {i+1}', 'meta_description': '',
                        'keywords': [], 'article_markdown': raw}

            body = data.get('article_markdown', '')
            review = await self.guard.review(body, 'article')
            if not review['allowed']:
                await self.log(f"[Compliance] BLOCKED article: {review['violations']}", 'text-rose-400')
                continue
            for disc in review['disclosures']:
                body += f'\n\n> {disc}'

            fname = re.sub(r'[^a-z0-9]+', '_', data['title'].lower())[:60].strip('_') or f'article_{i+1}'
            path = self._out_dir('articles') / f'{fname}.md'
            front = (f"---\ntitle: \"{data['title']}\"\n"
                     f"description: \"{data.get('meta_description', '')}\"\n"
                     f"keywords: {json.dumps(data.get('keywords', []))}\n"
                     f"date: {datetime.now().isoformat(timespec='seconds')}\n---\n\n")
            path.write_text(front + body, encoding='utf-8')

            entry = self.ledger.add_produced(self.name, 'article', data['title'],
                                             path=str(path), meta={'keywords': data.get('keywords', [])})
            results.append({'status': 'OK', 'entry_id': entry['id'], 'path': str(path),
                            'title': data['title'], 'compliance_warnings': review['warnings']})
            await self.log(f'[SEO] Article ready: {data["title"]}', 'text-emerald-300')
        return results


class DigitalProductStream(_StreamBase):
    """Ebooks, guides, template packs, prompt libraries — upload-ready folders."""
    name = 'products'
    label = 'Digital Product Studio'
    description = 'Ebooks / guides / Notion-style templates / prompt packs for Gumroad, Etsy, Ko-fi.'
    effort = 'medium'
    time_to_first_dollar = 'days-weeks (marketplace approval + traffic)'

    PRODUCT_TYPES = ['ebook', 'guide', 'template_pack', 'prompt_pack', 'checklist_kit']

    async def produce(self, spec: str, count: int = 1, websocket=None) -> List[Dict]:
        results = []
        for i in range(count):
            suffix = f' (variation #{i+1}, distinct from previous)' if count > 1 else ''
            prompt = f"""Create a complete, sellable digital product.

SPEC: {spec}{suffix}

Deliver as JSON:
{{"product_name": "...", "type": "ebook|guide|template_pack|prompt_pack|checklist_kit",
"target_buyer": "...", "price_suggestion_usd": 9-49,
"sales_copy": "150-word marketplace listing copy, honest, no income guarantees",
"contents": [
   {{"section": "Section title", "body": "full deliverable content for this section"}}
]
}}
Rules: 6-12 sections, each section body must be COMPLETE and genuinely useful
(no placeholders like "TODO" or "insert here"). Original work only."""

            raw = await self._llm(prompt, max_tokens=8000, tier='cloud')
            try:
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                data = json.loads(m.group(0))
            except Exception:
                await self.log(f'[Products] Failed to parse product JSON (attempt {i+1})', 'text-rose-400')
                continue

            review = await self.guard.review(data.get('sales_copy', ''), 'description')
            if not review['allowed']:
                await self.log(f"[Compliance] BLOCKED listing copy: {review['violations']}", 'text-rose-400')
                continue

            slug = re.sub(r'[^a-z0-9]+', '_', data['product_name'].lower())[:50].strip('_') or f'product_{i+1}'
            pdir = self._out_dir(slug)
            (pdir / 'LISTING.md').write_text(
                f"# {data['product_name']}\n\n**Price:** ${data.get('price_suggestion_usd', 19)}\n\n"
                f"{data.get('sales_copy', '')}\n", encoding='utf-8')
            body_md = '\n\n'.join(f"## {s['section']}\n\n{s['body']}"
                                  for s in data.get('contents', []))
            (pdir / 'PRODUCT.md').write_text(
                f"# {data['product_name']}\n\n{body_md}\n", encoding='utf-8')

            entry = self.ledger.add_produced(self.name, data.get('type', 'digital_product'),
                                             data['product_name'], path=str(pdir),
                                             meta={'price_suggestion_usd': data.get('price_suggestion_usd')})
            results.append({'status': 'OK', 'entry_id': entry['id'], 'path': str(pdir),
                            'title': data['product_name'],
                            'price_suggestion_usd': data.get('price_suggestion_usd')})
            await self.log(f'[Products] Built "{data["product_name"]}" -> {pdir}', 'text-emerald-300')
        return results


class StockAssetStream(_StreamBase):
    """Vector/SVG asset packs via the ImageWorker."""
    name = 'stock'
    label = 'Stock Asset Factory'
    description = 'Icon packs, backgrounds, illustration sets for marketplaces.'
    effort = 'low'
    time_to_first_dollar = 'weeks-months (volume game)'

    async def produce(self, theme: str, count: int = 10, websocket=None) -> List[Dict]:
        iw = self.workers.get('image')
        if not iw:
            return [{'status': 'ERROR', 'message': 'ImageWorker not available'}]

        concepts_raw = await self._llm(
            f'List {min(count, 20)} distinct, commercially-useful visual asset concepts for the theme '
            f'"{theme}". One line each, format: "slug | image generation prompt". No intro text.',
            max_tokens=800)
        concepts = []
        for line in concepts_raw.splitlines():
            if '|' in line:
                slug, prompt_txt = line.split('|', 1)
                slug = re.sub(r'[^a-z0-9_]+', '_', slug.strip().lower()).strip('_')
                if slug:
                    concepts.append((slug, prompt_txt.strip()))
        concepts = concepts[:count]

        out_dir = self._out_dir(re.sub(r'[^a-z0-9_]+', '_', theme.lower())[:30])
        results = []
        for slug, prompt_txt in concepts:
            try:
                path = out_dir / f'{slug}.svg'
                await iw.generate(prompt_txt, str(path))
                entry = self.ledger.add_produced(self.name, 'asset', slug, path=str(path),
                                                 meta={'theme': theme})
                results.append({'status': 'OK', 'entry_id': entry['id'], 'path': str(path)})
            except Exception as e:
                results.append({'status': 'ERROR', 'message': f'{slug}: {e}'})
        await self.log(f'[Stock] Produced {len([r for r in results if r["status"] == "OK"])}/{len(concepts)} assets in {out_dir}')
        return results


class GigProposalStream(_StreamBase):
    """Marketplace gig listings + tailored proposal templates."""
    name = 'gigs'
    label = 'Gig Pilot'
    description = 'Fiverr/Upwork gig listings and reusable proposal drafts based on your skills.'
    effort = 'low'
    time_to_first_dollar = 'fastest route: days (service work pays first)'

    async def produce(self, skills_desc: str, count: int = 3, websocket=None) -> List[Dict]:
        results = []
        for i in range(count):
            prompt = f"""Design gig offer #{i+1} of {count} for a freelancer with these skills: {skills_desc}

Different service angle each time. Return JSON only:
{{"gig_title": "...", "platform_fit": "fiverr|upwork", "starting_price_usd": 15-150,
"gig_description": "full listing copy, benefit-led, honest, no fake portfolio claims",
"packages": [{{"name":"Basic","price_usd":X,"deliverables":"...","delivery_days":N}},
             {{"name":"Standard",...}}, {{"name":"Premium",...}}],
"proposal_template": "reusable proposal text with [PLACEHOLDERS] to personalize per client"}}"""
            await self.log(f'[Gigs] drafting offer {i + 1}/{count} — local brain is '
                           'writing a full gig package; this can take several minutes…',
                           'text-indigo-200')
            data = None
            for attempt in (1, 2):
                raw = await self._llm(
                    prompt if attempt == 1 else
                    prompt + '\n\nIMPORTANT: reply with ONE valid JSON object and NOTHING else.',
                    max_tokens=3000, tier='cloud')
                try:
                    m = re.search(r'\{.*\}', raw, re.DOTALL)
                    data = json.loads(m.group(0)) if m else None
                except Exception:
                    data = None
                if isinstance(data, dict) and data.get('gig_title'):
                    break
                data = None
                if attempt == 1:
                    await self.log('[Gigs] reply was not clean JSON — retrying once with stricter instruction…',
                                   'text-amber-300')
            if not data:
                await self.log(f'[Gigs] offer {i + 1}: could not get valid JSON from the model',
                               'text-rose-400')
                results.append({'status': 'ERROR', 'entry_id': None, 'path': '',
                                'title': f'offer {i + 1}'})
                continue

            title = data.get('gig_title') or f'Gig offer {i + 1}'
            review = await self.guard.review(data.get('gig_description', ''), 'description')
            if not review['allowed']:
                await self.log(f"[Compliance] BLOCKED gig copy: {review['violations']}", 'text-rose-400')
                results.append({'status': 'BLOCKED', 'entry_id': None, 'path': '', 'title': title})
                continue

            slug = re.sub(r'[^a-z0-9]+', '_', title.lower())[:50].strip('_') or f'gig_{i+1}'
            path = self._out_dir('gigs') / f'{slug}.md'
            pkgs = '\n'.join(f"- **{p.get('name')}** — ${p.get('price_usd')} · {p.get('deliverables')} "
                             f"({p.get('delivery_days')} days)" for p in data.get('packages', []))
            path.write_text(
                f"# {title}\n**Platform fit:** {data.get('platform_fit')} · "
                f"**Start at:** ${data.get('starting_price_usd')}\n\n"
                f"## Listing copy\n{data.get('gig_description')}\n\n"
                f"## Packages\n{pkgs or '(none)'}\n\n"
                f"## Proposal template\n{data.get('proposal_template')}\n", encoding='utf-8')
            entry = self.ledger.add_produced(self.name, 'proposal', title, path=str(path),
                                             meta={'starting_price_usd': data.get('starting_price_usd')})
            results.append({'status': 'OK', 'entry_id': entry['id'], 'path': str(path),
                            'title': title})
            await self.log(f'[Gigs] Gig package ready: {title}', 'text-emerald-300')
        return results


class VideoCampaignStream(_StreamBase):
    """Full multi-platform video campaigns via the VideoFactory."""
    name = 'video'
    label = 'Multi-Platform Video Factory'
    description = ('One idea -> vertical master (YT Shorts/TikTok/IG Reels/FB Reels) '
                   '+ 5-min 16:9 master (YouTube/FB feed) + thumbnail + per-platform '
                   'upload packages. Visuals from ComfyUI AI or Pexels stock.')
    effort = 'high'
    time_to_first_dollar = '2-8 weeks (monetization thresholds); days if you sell video as a service'

    async def produce(self, topic: str, count: int = 1, websocket=None) -> List[Dict]:
        results = []
        campaigns = max(1, min(count, 3))
        for i in range(campaigns):
            campaign_topic = topic if campaigns == 1 else f'{topic} — distinct angle #{i+1}'
            out = await self.engine.video_factory.create_campaign(campaign_topic,
                                                                  websocket=websocket)
            ok = out.get('status') == 'OK'
            results.append({'status': 'OK' if ok else 'ERROR',
                            'entry_id': None,
                            'path': out.get('campaign_dir', ''),
                            'title': campaign_topic,
                            'renders': out.get('renders', {})})
        return results


class ClipHarvestStream(_StreamBase):
    """Long video link -> N vertical shorts (transcribe -> LLM moments ->
    reframe + captions). Only for videos the user owns or has rights to."""
    name = 'clips'
    label = 'Clip Harvest Factory'
    description = ('Repurposes long-form video (your own or licensed) into '
                   'vertical shorts with word-synced captions and blurred-fill '
                   '9:16 framing.')
    effort = 'medium'
    time_to_first_dollar = '2-6 weeks (platform monetization thresholds)'

    async def produce(self, topic: str, count: int = 3, websocket=None) -> List[Dict]:
        from core.clip_harvester import ClipHarvester
        harvester = ClipHarvester(self.router, log=self.engine.log)
        return await harvester.run(topic, count=count, ledger=self.ledger)


STREAMS = {
    s.name: s for s in (ShortFormStream, VideoCampaignStream, SEOContentStream,
                        DigitalProductStream, StockAssetStream, GigProposalStream,
                        ClipHarvestStream)
}


# ===========================================================================
# Daily planner — decides what Buddy produces today
# ===========================================================================
class DailyPlanner:
    def __init__(self, engine: 'MoneyEngine'):
        self.engine = engine

    async def build_plan(self, target_usd: float, user_context: str = '') -> Dict[str, Any]:
        stats = self.engine.ledger.what_works()
        report = self.engine.ledger.report()
        pitfalls = ''
        lessons = getattr(self.engine, 'lessons', None)
        if lessons is not None:
            try:
                pitfalls = lessons.as_prompt_context(limit=5)
            except Exception:
                pitfalls = ''

        prompt = f"""You are the revenue planner for an autonomous AI production system.
The system can produce: shorts (vertical videos), clips (repurposing a LONG video
the user owns or has rights to into several vertical shorts — needs a link),
seo (articles), products (digital
products like ebooks/templates/prompt packs), stock (asset packs), gigs (freelance
gig listings + proposals).

TODAY'S TARGET: ${target_usd}
USER CONTEXT (skills, niches, available accounts): {user_context or 'unspecified'}
PRODUCTION HISTORY (what has worked): {json.dumps(stats) if stats else 'nothing yet — first day'}
TODAY SO FAR: {json.dumps(report['today'])}
KNOWN PITFALLS (from past failed runs — do not repeat these approaches):
{pitfalls or 'none recorded yet'}

BUDDY'S VALUES (stay consistent): honest timelines over hype; discipline over genius;
ship what can actually be finished today.

Be honest about physics: content compounds slowly; services/gigs pay fastest;
digital products are mid-term. Produce a realistic TODAY plan weighted toward
output that can actually be shipped in hours.

Return ONLY JSON:
{{"strategy_summary": "2 sentences",
"focus_stream": "shorts|seo|products|stock|gigs",
"tasks": [
  {{"stream": "...", "topic_or_spec": "...", "count": N, "why": "one line"}}
],
"human_actions_needed": ["things only the human can do today: upload X, apply to Y"]}}
Max 5 tasks."""
        raw = await self.engine.llm_raw(prompt, max_tokens=1200, tier='cloud')
        try:
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            return json.loads(m.group(0))
        except Exception:
            return {
                'strategy_summary': 'Fallback plan: ship sellable output.',
                'focus_stream': 'products',
                'tasks': [{'stream': 'products', 'topic_or_spec':
                           'practical productivity template pack', 'count': 1, 'why': 'evergreen demand'}],
                'human_actions_needed': ['create marketplace accounts if missing'],
            }


# ===========================================================================
# Facade
# ===========================================================================
class MoneyEngine:
    def __init__(self, llm_router, workers: Dict, ledger: Optional[EarningsLedger] = None,
                 log=None, lessons=None, soul=None):
        """
        llm_router: HybridRouter (or any .chat.completions.create client)
        workers:    the server's worker dict (code/web/research/shorts/image/...)
        log:        async callable(message, color) for dashboard streaming
        lessons:    shared LessonsStore (same instance MissionControl uses);
                    every production outcome is recorded here and the daily
                    planner reads known pitfalls back before planning.
        soul:       shared Soul (persona + memory journal); production wins
                    become memories so Buddy stays consistent across sessions.
        """
        self.router = llm_router
        self.workers = workers
        self.ledger = ledger or EarningsLedger()
        if lessons is None:
            from core.lessons_store import LessonsStore
            lessons = LessonsStore()
        self.lessons = lessons
        self.soul = soul
        self.guard = ComplianceGuard(llm_router)
        self.planner = DailyPlanner(self)
        self._log = log
        self.video_factory = VideoFactory(llm_router, workers, ledger=self.ledger,
                                          guard=self.guard, log=self._log,
                                          visual_source=os.getenv('VISUAL_SOURCE', 'auto'))
        self.streams = {name: cls(self) for name, cls in STREAMS.items()}
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    async def log(self, message: str, color: str = 'text-emerald-300'):
        print(message, flush=True)
        if self._log:
            try:
                await self._log(message, color)
            except Exception:
                pass

    async def llm_raw(self, prompt: str, max_tokens: int = 2000,
                      tier: Optional[str] = None) -> str:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self.router.chat.completions.create(
                model=None, messages=[{'role': 'user', 'content': prompt}],
                temperature=0.3, max_tokens=max_tokens, tier=tier)
        )
        return resp.choices[0].message.content or ''

    # ------------------------------------------------------------------
    async def brief(self) -> Dict[str, Any]:
        """Dashboard payload: today vs target, what works, suggestions."""
        report = self.ledger.report()
        works = self.ledger.what_works()
        best = sorted(works.items(), key=lambda kv: -kv[1]['revenue_usd'])[:3] if works else []
        report['best_streams'] = [{'stream': k, **v} for k, v in best]
        report['streams_catalog'] = [
            {'name': s.name, 'label': s.label, 'description': s.description,
             'effort': s.effort, 'time_to_first_dollar': s.time_to_first_dollar}
            for s in self.streams.values()]
        return report

    # ------------------------------------------------------------------
    async def produce(self, stream_name: str, topic: str, count: int = 1,
                      websocket=None) -> List[Dict]:
        stream = self.streams.get(stream_name)
        if not stream:
            return [{'status': 'ERROR',
                     'message': f'Unknown stream "{stream_name}". Available: {list(self.streams)}'}]
        await self.log(f'[MoneyEngine] Producing via {stream.label}: "{topic}" x{count}',
                       'text-amber-300')
        try:
            results = await stream.produce(topic, count=count, websocket=websocket)
        except Exception as e:
            self._record_lesson(stream_name, topic, 'failure', str(e))
            await self.log(f'[MoneyEngine] {stream_name} failed: {e}', 'text-rose-400')
            return [{'status': 'ERROR', 'message': str(e)}]

        # Lessons Loop: record outcomes so future plans avoid repeat mistakes
        ok = [r for r in results if isinstance(r, dict) and r.get('status') == 'OK']
        bad = [r for r in results if isinstance(r, dict) and r.get('status') != 'OK']
        if ok:
            self._record_lesson(stream_name, topic, 'success',
                                detail=f'{len(ok)} unit(s) produced')
            if getattr(self, 'soul', None):
                try:
                    self.soul.remember(
                        f'produced {len(ok)} via {stream_name}: {topic[:60]}',
                        kind='production')
                except Exception:
                    pass
        for r in bad:
            self._record_lesson(stream_name, topic, 'failure',
                                str(r.get('message') or 'unknown error'))
        return results

    def _record_lesson(self, stream_name: str, topic: str, outcome: str,
                       detail: str = ''):
        """Best-effort lesson write; never break production over bookkeeping."""
        try:
            self.lessons.record(f'money:{stream_name}', topic, outcome, detail=detail)
        except Exception:
            pass

    # ------------------------------------------------------------------
    async def run_daily_plan(self, target_usd: Optional[float] = None,
                             user_context: str = '', websocket=None) -> Dict[str, Any]:
        target = float(target_usd or self.ledger.daily_target())
        await self.log(f'[MoneyEngine] Building daily plan — target ${target}/day', 'text-amber-300')
        plan = await self.planner.build_plan(target, user_context)
        await self.log(f"[Plan] {plan.get('strategy_summary', '')}", 'text-indigo-200')

        results = {'plan': plan, 'executed': []}
        for task in plan.get('tasks', [])[:5]:
            stream = task.get('stream', '')
            topic = task.get('topic_or_spec', '')
            count = int(task.get('count', 1) or 1)
            if stream not in self.streams or not topic:
                continue
            out = await self.produce(stream, topic, count=min(count, 5), websocket=websocket)
            results['executed'].append({'stream': stream, 'topic': topic, 'results': out})

        report = self.ledger.report()
        results['today_after'] = report['today']
        await self.log(f"[MoneyEngine] Day's production complete — "
                       f"{report['today']['produced']} units produced today. "
                       f"Human actions next: {plan.get('human_actions_needed', [])}",
                       'text-emerald-300')
        return results

    # ------------------------------------------------------------------
    def add_revenue(self, amount_usd: float, source: str, note: str = '',
                    entry_id: Optional[str] = None):
        rec = self.ledger.add_revenue(amount_usd, source, note, entry_id)
        return rec


if __name__ == '__main__':
    # Smoke test without LLM/workers: ledger + catalog
    eng = MoneyEngine(llm_router=None, workers={})
    print(json.dumps(asyncio.run(eng.brief()), indent=2))
