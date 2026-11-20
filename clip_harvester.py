"""
Buddy OS v8.2 — Clip Harvester
==============================
One long video link in -> N platform-ready vertical shorts out.

    INPUT:  a yt-dlp-supported URL (YouTube, etc.) OR a local video path
    OUTPUT: workspace/output/money/clips/<slug>/
              clip_01_<hook>.mp4 ...   1080x1920 vertical, blurred-fill
                                       background (nothing cropped away),
                                       burned word-synced captions
              harvest.json             windows, hooks, source metadata

Pipeline — built ONLY from parts already installed and proven on this
machine (no repo cloning, no new heavy deps):
    1. fetch      yt-dlp  (same pattern as /learn's downloader)
    2. transcribe faster-whisper 'small' int8, word timestamps (/learn engine)
    3. decide     hybrid-router LLM scores the transcript and picks the
                  strongest moments (escalates to cloud if keys exist)
    4. reframe    ffmpeg: blurred-background vertical fill (content kept)
    5. caption    moviepy + PIL overlays (same approach as ShortsWorker)

HONEST-USE NOTE (attached to every batch): only harvest videos you own or
have a license/rights to re-use. Re-uploading other people's content as
your own invites copyright strikes and platform bans — this tool is for
repurposing YOUR OWN long-form library (or licensed/CC material).
"""

import asyncio
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.shorts_worker import _load_font
from core.output_verifier import verify_video

WORK_ROOT = Path('workspace/clips_temp')
OUTPUT_ROOT = Path('workspace/output/money/clips')

VERT_W, VERT_H = 1080, 1920
MIN_CLIP, MAX_CLIP = 15.0, 60.0

RIGHTS_NOTE = ('Harvested clips: upload only videos you own or have rights '
               'to re-use. Repurposing others\' content without permission '
               'risks copyright strikes and channel bans.')


def _run(cmd: List[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', s.lower())[:40].strip('_') or 'clip'


class ClipHarvester:
    """See module docstring. router: HybridRouter-compatible client."""

    def __init__(self, router, log=None):
        self.router = router
        self._log = log          # async callable(message, color) or None
        WORK_ROOT.mkdir(parents=True, exist_ok=True)
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    async def log(self, message: str, color: str = 'text-emerald-300'):
        print(message, flush=True)
        if self._log:
            try:
                await self._log(message, color)
            except Exception:
                pass

    async def _llm(self, prompt: str, max_tokens: int = 2500,
                   tier: Optional[str] = None) -> str:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: self.router.chat.completions.create(
                model=None, messages=[{'role': 'user', 'content': prompt}],
                temperature=0.3, max_tokens=max_tokens, tier=tier)
        )
        text = resp.choices[0].message.content or ''
        # strip qwen3-style reasoning blocks that break JSON parsing
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # ------------------------------------------------------------------
    # 1. FETCH
    # ------------------------------------------------------------------
    def _download(self, url: str, workdir: Path) -> tuple:
        """URL -> (source_path, meta). Local paths pass straight through."""
        if Path(url).exists():
            p = Path(url)
            return p, {'title': p.stem, 'uploader': '', 'duration': 0,
                       'webpage_url': str(p.resolve())}
        try:
            import yt_dlp
            ydl_opts = {
                'format': 'best[height<=720][ext=mp4]/best[height<=720]/best',
                'outtmpl': str(workdir / 'source.%(ext)s'),
                'merge_output_format': 'mp4',
                'noplaylist': True,
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            src = workdir / f"source.{info.get('ext', 'mp4')}"
            if not src.exists():     # merged file may carry another ext
                cands = [p for p in workdir.glob('source.*')
                         if p.suffix != '.json']
                src = cands[0] if cands else src
            meta = {'title': info.get('title', 'untitled'),
                    'uploader': info.get('uploader', ''),
                    'duration': info.get('duration', 0),
                    'webpage_url': info.get('webpage_url', url)}
            return src, meta
        except ImportError:
            if not shutil.which('yt-dlp'):
                raise RuntimeError('yt-dlp is not installed. '
                                   'Run: python -m pip install -U yt-dlp')
            out = workdir / 'source.mp4'
            proc = _run(['yt-dlp', '-f', 'best[height<=720]/best',
                         '-o', str(out), '--no-playlist', url])
            if not out.exists():
                raise RuntimeError(f'yt-dlp failed: '
                                   f'{(proc.stderr or proc.stdout)[-300:]}')
            return out, {'title': 'untitled', 'uploader': '',
                         'duration': 0, 'webpage_url': url}

    # ------------------------------------------------------------------
    # 2. TRANSCRIBE (faster-whisper, same engine as /learn)
    # ------------------------------------------------------------------
    def _transcribe(self, src: Path, workdir: Path) -> List[Dict[str, Any]]:
        wav = workdir / 'audio16k.wav'
        proc = _run(['ffmpeg', '-y', '-i', str(src), '-vn', '-ar', '16000',
                     '-ac', '1', str(wav)], timeout=1200)
        if not wav.exists():
            raise RuntimeError(f'audio extraction failed: '
                               f'{(proc.stderr or "")[-300:]}')
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError('faster-whisper not installed. Run: '
                               'python -m pip install -U faster-whisper')
        model = WhisperModel('small', device='auto', compute_type='int8')
        segments, _info = model.transcribe(str(wav), vad_filter=True,
                                           word_timestamps=True)
        out: List[Dict[str, Any]] = []
        for seg in segments:
            words = [{'start': round(w.start, 2), 'end': round(w.end, 2),
                      'text': w.word.strip()}
                     for w in (seg.words or []) if w.word and w.word.strip()]
            out.append({'start': round(seg.start, 2), 'end': round(seg.end, 2),
                        'text': seg.text.strip(), 'words': words})
        if not out:
            raise RuntimeError('No speech found in the video.')
        return out

    # ------------------------------------------------------------------
    # 3. DECIDE — LLM picks the strongest moments
    # ------------------------------------------------------------------
    async def _pick_windows(self, segments: List[Dict], count: int,
                            total_dur: float) -> List[Dict[str, float]]:
        lines, char_budget = [], 15_000
        for s in segments:
            line = f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}"
            if char_budget - len(line) < 0:
                break
            char_budget -= len(line) + 1
            lines.append(line)
        raw = await self._llm(f"""You are a short-form video editor. Below is a
timestamped transcript of a longer video. Pick the TOP {count} moments that work
as standalone vertical shorts (each between {int(MIN_CLIP)} and {int(MAX_CLIP)}
seconds).

Prefer: strong hooks in the first sentence, surprising facts, emotional peaks,
complete ideas with a payoff, moments that make sense WITHOUT the rest of the
video. Timestamps must come from the transcript.

Return ONLY a JSON array:
[{{"start": 123.4, "end": 155.0, "title": "short punchy title", "hook": "why this moment works"}}]

TRANSCRIPT:
{chr(10).join(lines)}""", max_tokens=2000, tier='cloud')

        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            raise ValueError('LLM moment selection did not return JSON')
        picks = json.loads(m.group(0))

        # ---- validate / clamp / de-overlap ----
        seg_starts = [s['start'] for s in segments]
        windows, taken = [], []
        for p in sorted(picks, key=lambda x: x.get('start', 0)):
            try:
                st, en = float(p['start']), float(p['end'])
            except (KeyError, TypeError, ValueError):
                continue
            # snap to a nearby segment start so cuts land between sentences
            near = [t for t in seg_starts if abs(t - st) <= 3.0]
            if near:
                st = min(near, key=lambda t: abs(t - st))
            en = min(max(en, st + MIN_CLIP * 0.6), min(st + MAX_CLIP,
                                                       max(total_dur, st + 1)))
            st = max(st, 0.0)
            if en - st < 8.0:
                continue
            if any(st < t_end and en > t_start for t_start, t_end in taken):
                continue                      # overlapping an earlier pick
            taken.append((st, en))
            windows.append({'start': round(st, 2), 'end': round(en, 2),
                            'title': str(p.get('title', ''))[:100],
                            'hook': str(p.get('hook', ''))[:200]})
            if len(windows) >= count:
                break
        if not windows:
            raise ValueError('No valid clip windows survived validation — '
                             'try --count 3 on a talkier video.')
        return windows

    # ------------------------------------------------------------------
    # 4. REFRAME — blurred-background vertical fill (keeps all content)
    # ------------------------------------------------------------------
    def _render_vertical(self, src: Path, win: Dict, tmp_out: Path) -> bool:
        dur = win['end'] - win['start']
        vf = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=20,eq=brightness=-0.15[bgb];"
            f"[fg]scale={VERT_W}:-2[fgs];"
            "[bgb][fgs]overlay=(W-w)/2:(H-h)/2:shortest=1,"
            "format=yuv420p[v]"
        )
        proc = _run(['ffmpeg', '-y', '-ss', str(win['start']), '-t', f'{dur:.2f}',
                     '-i', str(src), '-filter_complex', vf, '-map', '[v]',
                     '-map', '0:a?', '-c:v', 'libx264', '-preset', 'veryfast',
                     '-crf', '20', '-c:a', 'aac', str(tmp_out)], timeout=900)
        return tmp_out.exists() and tmp_out.stat().st_size > 10_000

    # ------------------------------------------------------------------
    # 5. CAPTIONS — word-synced overlays, ShortsWorker's proven PIL approach
    # ------------------------------------------------------------------
    @staticmethod
    def _word_chunks(words: List[Dict], win: Dict) -> List[Dict]:
        """Group words into caption chunks (<=3 words / <=2.5s, break on pauses)."""
        inside = [w for w in words if w['end'] > win['start']
                  and w['start'] < win['end']]
        chunks, cur = [], []
        for w in inside:
            cur.append(w)
            span_end = cur[-1]['end']
            gap_next = w['end']
            if (len(cur) >= 3 or span_end - cur[0]['start'] >= 2.5):
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)
        out = []
        for ch in chunks:
            out.append({'start': max(ch[0]['start'], win['start']),
                        'end': min(ch[-1]['end'], win['end']),
                        'text': ' '.join(w['text'] for w in ch)})
        return [c for c in out if c['end'] > c['start']]

    @staticmethod
    def _caption_image(text: str, font):
        from PIL import Image, ImageDraw
        probe = Image.new('RGBA', (10, 10))
        d = ImageDraw.Draw(probe)
        bbox = d.textbbox((0, 0), text, font=font, stroke_width=4)
        pad = 18
        iw, ih = bbox[2] - bbox[0] + pad * 2, bbox[3] - bbox[1] + pad * 2
        img = Image.new('RGBA', (max(iw, 10), max(ih, 10)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.text((pad - bbox[0], pad - bbox[1]), text, font=font,
               fill=(255, 255, 255, 255), stroke_width=4,
               stroke_fill=(0, 0, 0, 255))
        return img

    def _overlay_captions(self, tmp_out: Path, win: Dict,
                          all_words: List[Dict], final_out: Path) -> bool:
        try:
            import numpy as np
            from moviepy import (VideoFileClip, ImageClip, CompositeVideoClip)
        except ImportError:
            # captions are enhancement, not requirement — ship the clip
            shutil.copyfile(tmp_out, final_out)
            return False
        chunks = self._word_chunks(all_words, win)
        if not chunks:
            shutil.copyfile(tmp_out, final_out)
            return False
        font = _load_font(int(VERT_W * 0.055))       # ~59px, matches shorts style
        video = VideoFileClip(str(tmp_out))
        offset = win['start']
        overlays = []
        for i, c in enumerate(chunks):
            t0, t1 = c['start'] - offset, c['end'] - offset
            if t1 - t0 <= 0.05:
                continue
            img = self._caption_image(c['text'].upper(), font)
            if img.width > VERT_W * 0.92:            # shrink oversized lines
                scale = (VERT_W * 0.92) / img.width
                img = img.resize((int(img.width * scale), int(img.height * scale)))
            clip = ImageClip(np.array(img)).with_duration(t1 - t0) \
                                          .with_start(max(t0, 0)) \
                                          .with_position(('center', 0.74 + 0.03 * (i % 2)))
            overlays.append(clip)
        final = CompositeVideoClip([video] + overlays)
        final.write_videofile(str(final_out), codec='libx264',
                              audio_codec='aac', preset='veryfast',
                              logger=None)
        final.close()
        video.close()
        return final_out.exists() and final_out.stat().st_size > 10_000

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------
    async def run(self, url: str, count: int = 3,
                  ledger=None) -> List[Dict[str, Any]]:
        tag = datetime.now().strftime('%H%M%S%f')[:10]
        workdir = WORK_ROOT / f'job_{tag}'
        workdir.mkdir(parents=True, exist_ok=True)

        await self.log('[ClipHarvester] Fetching source video...', 'text-amber-300')
        src, meta = self._download(url.strip(), workdir)
        await self.log(f'[ClipHarvester] Source: "{meta["title"]}"', 'text-indigo-200')

        await self.log('[ClipHarvester] Transcribing (faster-whisper small/int8)...',
                       'text-amber-300')
        segments = self._transcribe(src, workdir)
        total_dur = segments[-1]['end'] if segments else meta.get('duration') or 0
        all_words = [w for s in segments for w in s['words']]
        spoken = sum(len(s['words']) for s in segments)
        await self.log(f'[ClipHarvester] Transcript ready: {spoken} words, '
                       f'{total_dur/60:.1f} min of speech', 'text-indigo-200')

        count = max(1, min(int(count or 3), 6))
        await self.log(f'[ClipHarvester] Asking the brain for the top {count} '
                       'moments...', 'text-amber-300')
        windows = await self._pick_windows(segments, count, total_dur)
        for i, w in enumerate(windows, 1):
            await self.log(f'   moment {i}: [{w["start"]:.0f}s-{w["end"]:.0f}s] '
                           f'{w["title"]}', 'text-cyan-300')

        slug = _slug(meta['title'])
        out_dir = OUTPUT_ROOT / f'{slug}_{tag}'
        out_dir.mkdir(parents=True, exist_ok=True)

        results: List[Dict[str, Any]] = []
        for i, win in enumerate(windows, 1):
            hook_slug = _slug(win['title'])[:24]
            final = out_dir / f'clip_{i:02d}_{hook_slug}.mp4'
            tmp = workdir / f'tmp_{i:02d}.mp4'
            await self.log(f'[ClipHarvester] Rendering short {i}/{len(windows)} '
                           '(vertical reframe)...', 'text-amber-300')
            if not self._render_vertical(src, win, tmp):
                results.append({'status': 'ERROR',
                                'message': f'render failed for window {i}'})
                continue
            self._overlay_captions(tmp, win, all_words, final)
            tmp.unlink(missing_ok=True)
            if not final.exists():
                results.append({'status': 'ERROR',
                                'message': f'caption pass failed for window {i}'})
                continue
            # Soul vow #1: ffprobe proof before calling it done.
            qa = verify_video(final, expect_w=VERT_W, expect_h=VERT_H,
                              need_audio=True,
                              exact_duration=round(win['end'] - win['start'], 1))
            if not qa['ok']:
                results.append({'status': 'ERROR',
                                'message': f'clip {i} failed verification: '
                                           f'{qa["detail"]}'})
                await self.log(f'[ClipHarvester] ⛔ Short {i} failed QA: '
                               f'{qa["detail"]} — not counting it as produced',
                               'text-rose-400')
                continue
            entry_id = ''
            if ledger is not None:
                try:
                    rec = ledger.add_produced(
                        'clips', 'short_clip', win['title'] or meta['title'],
                        path=str(final),
                        meta={'url': meta['webpage_url'],
                              'start': win['start'], 'end': win['end'],
                              'duration': round(win['end'] - win['start'], 1)})
                    entry_id = rec['id']
                except Exception:
                    pass
            results.append({'status': 'OK', 'entry_id': entry_id,
                            'path': str(final), 'title': win['title'],
                            'start': win['start'],
                            'duration': round(win['end'] - win['start'], 1)})
            await self.log(f'[ClipHarvester] ✅ Short {i} ready: {final.name}',
                           'text-emerald-300')

        (out_dir / 'harvest.json').write_text(json.dumps({
            'url': meta['webpage_url'], 'title': meta['title'],
            'created': datetime.now().isoformat(timespec='seconds'),
            'rights_note': RIGHTS_NOTE,
            'windows': windows, 'results': results,
        }, indent=2, ensure_ascii=False), encoding='utf-8')

        ok_n = sum(1 for r in results if r['status'] == 'OK')
        await self.log(f'[ClipHarvester] Done: {ok_n}/{len(windows)} shorts in '
                       f'{out_dir}\n⚠️ {RIGHTS_NOTE}', 'text-emerald-400')
        shutil.rmtree(workdir, ignore_errors=True)     # RAM/disk hygiene
        return results


if __name__ == '__main__':
    import sys
    _router = None
    print('Standalone use needs a router; run via Buddy instead:')
    print('  /clip <video-url> [--count 3]')
    sys.exit(0)
