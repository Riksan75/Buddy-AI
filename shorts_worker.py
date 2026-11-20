"""
core/shorts_worker.py — Viral Shorts Factory for Buddy OS v8.3
Uses edge-tts (free, multi-language), Pexels + Pixabay (free keys),
optional faster-whisper caption sync, BGM bed with auto-ducking, MoviePy.
Fully compatible with Buddy OS WebSocket logging.
"""
import os
import re
import csv
import json
import random
import asyncio
import requests
import numpy as np
from pathlib import Path
from typing import List, Dict, Any, Optional

# Video editing
from moviepy import (
    VideoFileClip, AudioFileClip, ImageClip,
    concatenate_videoclips, CompositeVideoClip
)
try:
    # Needed for BGM mixing; both ship with moviepy 2.x
    from moviepy import AudioArrayClip, CompositeAudioClip, concatenate_audioclips
except ImportError:  # very old installs — BGM silently disabled
    AudioArrayClip = CompositeAudioClip = concatenate_audioclips = None
from PIL import Image, ImageDraw, ImageFont

# Free TTS
import edge_tts

# Ollama client (we pass the one from Buddy OS)
from openai import OpenAI


def _load_env_file(path: Path) -> None:
    """Tiny .env loader so API keys can live in buddy_osv5/.env (no dotenv dep).

    Only fills keys that are NOT already set in the real environment."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


_load_env_file(Path(".env"))


# ----------------- CONFIGURATION -----------------
# Keys come from environment / .env first — move them out of source code!
PEXELS_API_KEY = os.getenv(
    "PEXELS_API_KEY",
    "XDslyzSoR110stfLP7ztvZiMljRsQSJFZSmQqOog3MNIqXHSkXRWVJwP",  # legacy fallback
)
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")  # free key: https://pixabay.com/api/docs/

if PEXELS_API_KEY == "YOUR_PEXELS_API_KEY_HERE":
    print("⚠️  WARNING: Pexels API key not set. Please paste your key.")

EDGE_TTS_VOICE = "en-US-GuyNeural"          # Try "en-US-JennyNeural"
CAPTIONS_ENABLED = True
CAPTION_FONT_SIZE = 68
CAPTION_WORDS_PER_GROUP = 3                 # Words per caption chunk
TARGET_W, TARGET_H = 1080, 1920             # Vertical Shorts
WORK_DIR = Path("workspace/shorts_temp")
OUTPUT_DIR = Path("workspace/output/shorts")

# ---- v8.3: multi-language voices (all free edge-tts neural voices) ----
DEFAULT_LANGUAGE = "en"
LANG_VOICES = {
    "en": "en-US-GuyNeural",    "hi": "hi-IN-MadhurNeural",
    "ar": "ar-SA-HamedNeural",  "es": "es-ES-AlvaroNeural",
    "pt": "pt-BR-AntonioNeural","fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural", "id": "id-ID-ArdiNeural",
    "ja": "ja-JP-KeitaNeural",  "ko": "ko-KR-InJoonNeural",
    "ru": "ru-RU-DmitryNeural", "tr": "tr-TR-AhmetNeural",
}
LANG_NAMES = {
    "en": "English", "hi": "Hindi", "ar": "Arabic", "es": "Spanish",
    "pt": "Portuguese", "fr": "French", "de": "German", "id": "Indonesian",
    "ja": "Japanese", "ko": "Korean", "ru": "Russian", "tr": "Turkish",
}
# Per-language style rules so scripts sound like real speech, not textbooks.
# Hindi must be NORMAL spoken Hindustani — never pure/shuddh Sanskritized Hindi.
LANG_STYLE_NOTES = {
    "hi": ("Write ALL text in NORMAL spoken Hindi — everyday Hindustani, the way "
           "people actually talk in Indian YouTube reels. Freely mix in common "
           "English words (video, money, time, phone, plan). Use simple daily "
           "words of Urdu/everyday origin, NEVER Sanskritized ones — say "
           "'duniya' not 'vishv', 'waqt' not 'samay', 'khush' not 'prasannt'. "
           "Short punchy sentences, street-style tone."),
}
_DEFAULT_LANG_NOTE = ("Write ALL text in {name} — casual, conversational spoken "
                      "style, like someone talking to a friend (not formal or "
                      "textbook-like).")

# ---- v8.3: background music bed ----
BGM_ENABLED = True
BGM_DIR = Path("workspace/assets/bgm")   # drop royalty-free .mp3/.wav loops here
BGM_VOLUME = 0.16                        # base music loudness vs voice (1.0 = equal)
BGM_DUCK_DEPTH = 0.85                    # how far music dips under speech (0..1)

# ---- v8.3: whisper-accurate caption timing ----
WHISPER_SYNC = True                      # needs: pip install faster-whisper
WHISPER_MODEL_SIZE = "base"              # tiny/base/small — CPU int8 is fine

_STOPWORDS = frozenset((
    "a an the and or but if then than that this these those of in on at to "
    "from by for with about into over after before is are was were be been "
    "being it its it's as his her their our your my i we you they he she "
    "them us what which who whom how why when where all any both each few "
    "more most other some such no nor not only own same so too very can "
    "will just don should now did does do has have had here there"
).split())
# -------------------------------------------------

# Font fallback
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("⚠️  No system font found, using fallback.")
    return ImageFont.load_default()


class ShortsWorker:
    """Specialized worker for generating TikTok/YT Shorts using local Ollama + Free TTS + Pexels"""

    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model
        self.pexels_key = PEXELS_API_KEY
        self.pixabay_key = PIXABAY_API_KEY
        self._whisper_model = None      # lazy-loaded on first use
        os.makedirs(WORK_DIR, exist_ok=True)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        if BGM_ENABLED:
            os.makedirs(BGM_DIR, exist_ok=True)

    # ---------- 1. OLLAMA BRAIN ----------
    def _call_ollama(self, prompt: str, temp: float = 0.8) -> str:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temp,
                max_tokens=2000
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"Ollama error: {e}")
            return ""

    def generate_topics(self, niche: str, count: int, existing: list = None) -> list:
        existing = existing or []
        avoid = f"\nDo NOT repeat these topics:\n" + "\n".join(existing) if existing else ""
        prompt = f"""Generate exactly {count} viral video topics for the niche: "{niche}".
{avoid}
Each topic must be a punchy curiosity-gap title.
Output ONLY the list, one topic per line, no numbers."""
        raw = self._call_ollama(prompt, temp=0.9)
        lines = [re.sub(r"^\s*[\d\.\-\)]+\s*", "", l).strip() for l in raw.split("\n")]
        return [l for l in lines if l][:count]

    def generate_script(self, topic: str, language: str = DEFAULT_LANGUAGE) -> dict:
        lang_name = LANG_NAMES.get(language, "English")
        if language == "en":
            lang_rule = ""
        else:
            note = LANG_STYLE_NOTES.get(
                language, _DEFAULT_LANG_NOTE.format(name=lang_name))
            lang_rule = f"\n{note}\n"
        prompt = f"""Write a 30-second YouTube Shorts script for: "{topic}".
{lang_rule}
Format:
HOOK: <1-2 sentences, scroll-stopper>
BODY: <4-8 sentences, fast-paced>
CTA: <1 sentence, natural call to action>
ONLY respond with that exact format, no extra text."""
        raw = self._call_ollama(prompt, temp=0.75)

        def extract(tag):
            match = re.search(rf"{tag}:\s*(.*?)(?=\n[A-Z]+:|\Z)", raw, re.DOTALL)
            return match.group(1).strip() if match else ""

        return {
            "topic": topic,
            "hook": extract("HOOK"),
            "body": extract("BODY"),
            "cta": extract("CTA")
        }

    # ---------- 2. FREE TTS + CAPTIONS ----------
    async def _generate_voiceover(self, text: str, output_path: str,
                                  voice: Optional[str] = None) -> tuple:
        word_timings = []
        communicate = edge_tts.Communicate(text, voice or EDGE_TTS_VOICE)
        with open(output_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    start = chunk["offset"] / 10_000_000
                    duration = chunk["duration"] / 10_000_000
                    word_timings.append({"text": chunk["text"], "start": start, "end": start + duration})
        return word_timings

    def _render_caption_image(self, text: str, width: int) -> Image.Image:
        img = Image.new("RGBA", (width, 220), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        font = _load_font(CAPTION_FONT_SIZE)
        words = text.split()
        lines, current = [], ""
        for w in words:
            trial = (current + " " + w).strip()
            if draw.textbbox((0, 0), trial, font=font)[2] > width - 80 and current:
                lines.append(current)
                current = w
            else:
                current = trial
        if current:
            lines.append(current)
        y = (220 - (CAPTION_FONT_SIZE + 14) * len(lines)) // 2
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            x = (width - (bbox[2] - bbox[0])) // 2
            draw.text((x, y), line, font=font, fill="white", stroke_width=5, stroke_fill="black")
            y += CAPTION_FONT_SIZE + 14
        return img

    def _build_caption_clips(self, word_timings: list, width: int, height: int) -> list:
        if not word_timings:
            return []
        groups = []
        for i in range(0, len(word_timings), CAPTION_WORDS_PER_GROUP):
            chunk = word_timings[i:i+CAPTION_WORDS_PER_GROUP]
            groups.append({
                "text": " ".join(w["text"] for w in chunk),
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"]
            })
        clips = []
        for g in groups:
            dur = max(g["end"] - g["start"], 0.2)
            img = self._render_caption_image(g["text"], width)
            arr = np.array(img)
            clip = ImageClip(arr).with_duration(dur).with_start(g["start"])
            clip = clip.with_position(("center", int(height * 0.70)))
            clips.append(clip)
        return clips

    # ---------- 3. PEXELS FOOTAGE ----------
    def _search_pexels(self, query: str, count: int = 5) -> list:
        if not self.pexels_key or self.pexels_key == "YOUR_PEXELS_API_KEY_HERE":
            print("⚠️  Pexels API key missing. Cannot fetch clips.")
            return []
        url = "https://api.pexels.com/videos/search"
        headers = {"Authorization": self.pexels_key}
        params = {"query": query, "per_page": count, "orientation": "portrait"}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                print(f"Pexels error: {resp.status_code}")
                return []
            data = resp.json()
            urls = []
            for video in data.get("videos", []):
                files = sorted(video["video_files"], key=lambda f: f.get("width", 0))
                if files:
                    # Pick a mid‑quality file to keep downloads fast
                    urls.append(files[len(files)//2]["link"])
            return urls[:count]
        except Exception as e:
            print(f"Pexels request failed: {e}")
            return []

    # ---------- 3b. PIXABAY + SMART FOOTAGE POOL ----------
    def _search_pixabay(self, query: str, count: int = 5) -> list:
        """Pixabay video search — free second footage source. Returns mp4 URLs."""
        if not self.pixabay_key:
            return []
        try:
            resp = requests.get(
                "https://pixabay.com/api/videos/",
                params={"key": self.pixabay_key, "q": query,
                        "per_page": max(count, 3), "video_type": "film"},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"Pixabay error: {resp.status_code}")
                return []
            urls = []
            for hit in resp.json().get("hits", []):
                variants = hit.get("videos", {})
                pick = variants.get("large") or variants.get("medium")
                if pick and pick.get("url"):
                    urls.append(pick["url"])
            return urls[:count]
        except Exception as e:
            print(f"Pixabay request failed: {e}")
            return []

    def _scene_queries(self, script: dict, topic: str, max_queries: int = 4) -> list:
        """Derive varied search phrases from different script beats so each
        section of the video gets different footage instead of one repeated look."""
        queries = [topic]
        text = f"{script.get('hook', '')} {script.get('body', '')} {script.get('cta', '')}"
        sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.split()) >= 4]
        seen = set()
        for s in sentences:
            words = [w.strip('.,!?";\'').lower() for w in s.split()]
            kws = [w for w in words if w.isalpha() and w not in _STOPWORDS][:3]
            if not kws:
                continue
            q = " ".join(kws)
            if q.lower() in seen or q.lower() == topic.lower():
                continue
            seen.add(q.lower())
            queries.append(q)
            if len(queries) >= max_queries:
                break
        return queries

    def _fetch_clip_pool(self, script: dict, topic: str, log) -> list:
        """Query several scene-specific phrases across Pexels (+ Pixabay) and merge."""
        pool, seen = [], set()
        for q in self._scene_queries(script, topic):
            log(f"   🔎 Stock search: '{q}'")
            for u in self._search_pexels(q, 3):
                if u not in seen:
                    seen.add(u)
                    pool.append(u)
            if len(pool) >= 9:
                break
        if self.pixabay_key:
            log("   🔎 Stock search: pixabay.com")
            for u in self._search_pixabay(topic, 5):
                if u not in seen:
                    seen.add(u)
                    pool.append(u)
        else:
            log("   ℹ️ PIXABAY_API_KEY not set (.env) — using Pexels only.")
        random.shuffle(pool)
        return pool

    # ---------- 3c. BACKGROUND MUSIC BED (auto-ducked) ----------
    def _pick_bgm(self) -> Optional[str]:
        if not BGM_ENABLED or not BGM_DIR.exists():
            return None
        tracks = [p for p in BGM_DIR.iterdir()
                  if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".ogg")]
        return str(random.choice(tracks)) if tracks else None

    def _build_bgm_track(self, duration: float, voice_clip, log):
        """Return a ducked background-music clip matched to `duration`, or None.

        Ducking: smooth the voice loudness envelope and dip the music by up to
        BGM_DUCK_DEPTH whenever narration is present."""
        if AudioArrayClip is None or concatenate_audioclips is None:
            log("   ⚠️ This moviepy build lacks audio-mixing primitives — skipping BGM.")
            return None
        bgm_path = self._pick_bgm()
        if not bgm_path:
            log(f"   ℹ️ BGM enabled but no tracks in {BGM_DIR} — silent bed. "
                "Drop royalty-free .mp3 loops there to enable music.")
            return None
        sr = 44100
        try:
            bgm = AudioFileClip(bgm_path)
            reps = int(np.ceil(duration / max(bgm.duration, 0.1)))
            bed = concatenate_audioclips([bgm] * max(reps, 1)).subclipped(0, duration)
            samples = np.asarray(bed.to_soundarray(fps=sr))
            bgm.close()

            varr = np.asarray(voice_clip.to_soundarray(fps=sr))
            mono = varr.mean(axis=1) if varr.ndim > 1 else varr
            env = np.abs(mono)
            win = max(int(sr * 0.08), 1)
            env = np.convolve(env, np.ones(win) / win, mode="same")
            peak = float(env.max()) or 1.0
            presence = np.clip((env / peak) * 4.0, 0.0, 1.0)

            n = min(len(samples), len(mono))
            gain = BGM_VOLUME * (1.0 - BGM_DUCK_DEPTH * presence[:n])
            ducked = samples[:n] * (gain[:, None] if samples.ndim > 1 else gain)
            if ducked.ndim == 1:
                ducked = np.stack([ducked, ducked], axis=1)
            log(f"   🎵 BGM bed: {os.path.basename(bgm_path)} "
                f"({duration:.1f}s, volume {BGM_VOLUME}, duck {BGM_DUCK_DEPTH})")
            return AudioArrayClip(ducked.astype("float32"), fps=sr).with_duration(duration)
        except Exception as e:
            log(f"   ⚠️ BGM mixing skipped: {str(e)[:80]}")
            return None

    # ---------- 3d. WHISPER-ACCURATE CAPTION TIMING ----------
    def _whisper_word_timings(self, audio_path: str) -> list:
        """Frame-accurate word timings via faster-whisper. [] if unavailable."""
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return []
        try:
            if self._whisper_model is None:
                self._whisper_model = WhisperModel(
                    WHISPER_MODEL_SIZE, device="cpu", compute_type="int8"
                )
            segments, _info = self._whisper_model.transcribe(
                str(audio_path), word_timestamps=True
            )
            words = []
            for seg in segments:
                for w in (seg.words or []):
                    txt = (w.word or "").strip()
                    if txt:
                        words.append({"text": txt,
                                      "start": float(w.start),
                                      "end": float(w.end)})
            return words
        except Exception as e:
            print(f"⚠️ Whisper sync failed, keeping TTS timings: {e}")
            return []

    # ---------- 4. MASTER RENDERER (MoviePy 2.x) ----------
    def _build_single_video(self, topic: str, script: dict, output_path: str,
                            log_callback=None, language: str = DEFAULT_LANGUAGE,
                            voice: Optional[str] = None):
        """Runs the full render pipeline – FIXED FOR MOVIEPY 2.x with debug logs"""
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        # ---- Voiceover ----
        resolved_voice = voice or LANG_VOICES.get(language, EDGE_TTS_VOICE)
        log(f"🎙️ Generating voice for: {topic} [{resolved_voice}]")
        audio_path = WORK_DIR / "voice.mp3"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        word_timings = loop.run_until_complete(self._generate_voiceover(
            f"{script['hook']} {script['body']} {script['cta']}", str(audio_path),
            voice=resolved_voice
        ))
        loop.close()

        if not audio_path.exists():
            log("❌ Voiceover failed.")
            return False

        audio = AudioFileClip(str(audio_path))
        duration = audio.duration

        # Caption timing, best source first: faster-whisper > edge-tts > even spread
        if WHISPER_SYNC:
            refined = self._whisper_word_timings(str(audio_path))
            if refined:
                log(f"🎯 Whisper-synced captions ({len(refined)} words).")
                word_timings = refined

        # Fallback timings if whisper AND edge-tts both came up empty
        if not word_timings:
            log("⚠️ Using fallback word timings.")
            words = f"{script['hook']} {script['body']} {script['cta']}".split()
            if words:
                per_word = duration / len(words)
                t = 0.0
                for w in words:
                    word_timings.append({"text": w, "start": t, "end": t + per_word})
                    t += per_word
            else:
                # If for some reason there are no words, create a dummy caption
                word_timings.append({"text": " ", "start": 0, "end": duration})

        # ---- Fetch clips (multi-query, Pexels + Pixabay) ----
        log(f"🎥 Fetching stock footage for: {topic}")
        clip_urls = self._fetch_clip_pool(script, topic, log)
        if not clip_urls:
            # fallback to first word — try BOTH stock APIs, the Pexels key is
            # a legacy hardcoded one and may be dead/exhausted
            first_word = topic.split()[0] if topic.split() else "motivation"
            clip_urls = self._search_pexels(first_word, 3) or \
                self._search_pixabay(first_word, 3)

        # ---- Download clips ----
        downloaded = []
        for i, url in enumerate(clip_urls):
            clip_path = WORK_DIR / f"clip_{i}.mp4"
            try:
                r = requests.get(url, stream=True, timeout=30)
                if r.status_code == 200:
                    with open(clip_path, "wb") as f:
                        for chunk in r.iter_content(8192):
                            f.write(chunk)
                    downloaded.append(str(clip_path))
                else:
                    log(f"   ⚠️ Download failed: status {r.status_code}")
            except Exception as e:
                log(f"   ⚠️ Download error: {str(e)[:60]}")
                continue

        # ---- AI / chain fallback (optional) when the stock search above failed ----
        # ai_source is attached by VideoFactory (auto/comfyui modes). Since the
        # chain became stock-first it can hand back remote specs, so accept
        # local files AND urls that still need downloading.
        if not downloaded and getattr(self, 'ai_source', None) is not None \
                and getattr(self.ai_source, 'available', lambda: False)():
            log(f"🤖 No stock footage found — asking the visual chain for: {topic}")
            try:
                specs = self.ai_source.get_clips(topic, count=3, landscape=False)
                for j, s in enumerate(specs or []):
                    kind = s.get('type')
                    if kind == 'file':
                        downloaded.append(s['path'])
                    elif kind in ('url', 'download') and s.get('url'):
                        dest = WORK_DIR / f"chain_{j}.mp4"
                        try:
                            r = requests.get(s['url'], stream=True, timeout=60)
                            if r.status_code == 200:
                                with open(dest, "wb") as f:
                                    for chunk in r.iter_content(8192):
                                        f.write(chunk)
                                downloaded.append(str(dest))
                        except Exception as e:
                            log(f"   ⚠️ Chain download failed: {str(e)[:60]}")
                if downloaded:
                    log(f"   ✨ {len(downloaded)} clip(s) from the visual chain")
            except Exception as e:
                log(f"   ⚠️ AI source failed: {str(e)[:80]}")

        if not downloaded:
            log("❌ No clips could be found or generated.")
            return False

        # ---- Assemble with debug logs ----
        log(f"🎬 Assembling {topic}... (target duration: {duration:.2f}s)")

        video_clips = []
        running_time = 0.0
        random.shuffle(downloaded)
        idx = 0
        clip_count = 0
        max_clips = 30  # safety limit

        while running_time < duration and clip_count < max_clips:
            path = downloaded[idx % len(downloaded)]
            clip_count += 1
            clip_name = os.path.basename(path)
            log(f"   🔄 Loading clip #{clip_count}: {clip_name}...")

            try:
                # 1. Load
                clip = VideoFileClip(path)
                log(f"      ✅ Loaded. Duration: {clip.duration:.2f}s")

                # 2. Resize (MoviePy 2.x: .resized)
                if clip.h < clip.w:
                    clip = clip.resized(height=TARGET_H)
                else:
                    clip = clip.resized(width=TARGET_W)
                log(f"      ✅ Resized to {clip.w}x{clip.h}")

                # 3. Crop (MoviePy 2.x: .cropped)
                clip = clip.cropped(
                    x_center=clip.w / 2,
                    y_center=clip.h / 2,
                    width=TARGET_W,
                    height=TARGET_H
                )
                log(f"      ✅ Cropped to {clip.w}x{clip.h}")

                # 4. Append
                video_clips.append(clip)
                running_time += clip.duration
                log(f"      📊 Running total: {running_time:.2f}s / {duration:.2f}s")
                idx += 1

            except Exception as e:
                log(f"      ❌ Failed to load/skip clip: {str(e)[:80]}")
                idx += 1  # skip this clip
                continue

        if clip_count >= max_clips:
            log(f"⚠️ Reached clip limit ({max_clips}). Final video may be shorter than audio.")

        if not video_clips:
            log("❌ No usable video clips found.")
            return False

        # ---- Final composition ----
        final = concatenate_videoclips(video_clips).subclipped(0, duration)

        # ---- Audio mix: narration + auto-ducked BGM bed ----
        bgm_clip = self._build_bgm_track(duration, audio, log)
        if bgm_clip is not None:
            final = final.with_audio(CompositeAudioClip([audio, bgm_clip]))
        else:
            final = final.with_audio(audio)

        if CAPTIONS_ENABLED and word_timings:
            log("💬 Adding captions...")
            caption_clips = self._build_caption_clips(word_timings, TARGET_W, TARGET_H)
            final = CompositeVideoClip([final] + caption_clips)

        # ---- Render ----
        log(f"💾 Rendering final video to: {output_path}")
        try:
            final.write_videofile(
                output_path,
                fps=24,
                codec="libx264",
                audio_codec="aac",
                logger=None
            )
            log("✅ Render completed.")
            return True
        except Exception as e:
            log(f"❌ Render failed: {str(e)}")
            return False
        finally:
            # Clean up clips to free memory
            for c in video_clips:
                c.close()
            if 'final' in locals():
                final.close()
            if 'bgm_clip' in locals() and bgm_clip is not None:
                bgm_clip.close()
            audio.close()

    # ---------- 5. BATCH LAUNCHER ----------
    async def create_batch(self, niche: str, count: int, websocket=None,
                           language: str = DEFAULT_LANGUAGE,
                           voice: Optional[str] = None):
        """Main entry point: generate scripts, render videos, send logs.

        language/voice are optional — defaults keep every existing caller
        (money engine, /shorts command) working unchanged."""
        async def log_to_ws(msg, color="text-indigo-200"):
            print(f"[ShortsWorker] {msg}")
            if websocket:
                try:
                    await websocket.send_text(json.dumps({"message": msg, "color": color}))
                except:
                    pass

        resolved_voice = voice or LANG_VOICES.get(language, EDGE_TTS_VOICE)
        lang_note = "" if language == "en" else f" · {LANG_NAMES.get(language, language)} ({resolved_voice})"
        await log_to_ws(f"🔥 ShortsWorker engaged. Niche: {niche}, Count: {count}{lang_note}", "text-purple-400")

        # Step 1: Generate topics
        await log_to_ws(f"🧠 Brainstorming {count} topics...", "text-amber-300")
        topics = self.generate_topics(niche, count)
        if not topics:
            await log_to_ws("❌ No topics generated.", "text-rose-400")
            return

        # Step 2: Generate scripts
        scripts = []
        for i, t in enumerate(topics):
            await log_to_ws(f"✍️ Writing script {i+1}/{len(topics)}: {t}", "text-indigo-300")
            script = self.generate_script(t, language=language)
            if script.get('hook'):
                scripts.append(script)

        if not scripts:
            await log_to_ws("❌ No scripts generated.", "text-rose-400")
            return

        # Step 3: Save CSV (optional)
        csv_path = OUTPUT_DIR / f"batch_{niche.replace(' ', '_')}.csv"
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=["topic", "hook", "body", "cta"])
            writer.writeheader()
            writer.writerows(scripts)
        await log_to_ws(f"📄 Scripts saved to: {csv_path}", "text-emerald-300")

        # Step 4: Render each video
        for idx, script in enumerate(scripts):
            safe_name = re.sub(r'[^a-zA-Z0-9 ]', '', script['topic'])[:40].strip().replace(' ', '_')
            output_video_path = OUTPUT_DIR / f"short_{idx+1}_{safe_name}.mp4"

            await log_to_ws(f"🎬 Building video {idx+1}/{len(scripts)}: {script['topic']}", "text-cyan-300")

            # Run the heavy render in a thread to not block the event loop
            loop = asyncio.get_event_loop()
            success = await loop.run_in_executor(
                None,
                self._build_single_video,
                script['topic'],
                script,
                str(output_video_path),
                None,       # log_callback = None → prints; WS logs sent above
                language,
                voice,
            )

            if success:
                await log_to_ws(f"✅ Video ready: {output_video_path.name}", "text-emerald-400")
            else:
                await log_to_ws(f"❌ Failed to render: {script['topic']}", "text-rose-400")

        await log_to_ws(f"🏁 Batch complete! Check {OUTPUT_DIR}", "text-emerald-400")
