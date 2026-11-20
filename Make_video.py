"""
Shorts Video Builder — turns a script (Hook/Body/CTA) into an actual MP4.
----------------------------------------------------------------------------
Pipeline: text -> edge-tts voiceover (with word timings) -> matching Pexels
stock clips -> assembled vertical (1080x1920) video with voice, footage,
and burned-in synced captions.

REQUIREMENTS:
  pip install requests moviepy imageio-ffmpeg edge-tts

YOU NEED ONE API KEY (free):
  Pexels: https://www.pexels.com/api/  (free, instant signup)

Voiceover uses edge-tts (Microsoft's free neural TTS) - no key needed.
Browse more voices by running in a terminal:  edge-tts --list-voices

Fill in the Pexels key below, then run:
  python make_video.py
"""

import requests
import os
import csv
import sys
import random
import asyncio
import edge_tts
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Compatibility shim: newer Pillow versions removed Image.ANTIALIAS
# (renamed to Image.LANCZOS), but this version of moviepy still expects it.
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import (
    VideoFileClip, AudioFileClip, ImageClip, concatenate_videoclips, CompositeVideoClip
)

# ============ FILL THIS IN ============
PEXELS_API_KEY = "XDslyzSoR110stfLP7ztvZiMljRsQSJFZSmQqOog3MNIqXHSkXRWVJwP"
EDGE_TTS_VOICE = "en-US-GuyNeural"  # try "en-US-JennyNeural", "en-US-AriaNeural", "en-GB-RyanNeural", etc.
CAPTIONS_ENABLED = True
CAPTION_FONT_SIZE = 68
CAPTION_WORDS_PER_GROUP = 3  # how many words shown on screen at once
# =========================================

# Try a few common Windows/Mac font paths, fall back to PIL default if none found
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    print("⚠️  No system font found, using a basic fallback font (captions will look plainer).")
    return ImageFont.load_default()

TARGET_W, TARGET_H = 1080, 1920  # vertical 9:16 for Shorts
WORK_DIR = "video_build_temp"


def generate_voiceover_with_timings(text: str, output_path: str):
    """Use edge-tts to generate voiceover audio AND capture word-level timing.
    Returns (success: bool, word_timings: list of {"text", "start", "end"})."""
    word_timings = []

    async def _generate():
        communicate = edge_tts.Communicate(text, EDGE_TTS_VOICE)
        with open(output_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    start = chunk["offset"] / 10_000_000  # 100-ns units -> seconds
                    duration = chunk["duration"] / 10_000_000
                    word_timings.append({
                        "text": chunk["text"],
                        "start": start,
                        "end": start + duration,
                    })

    try:
        asyncio.run(_generate())
        ok = os.path.exists(output_path) and os.path.getsize(output_path) > 0
        return ok, word_timings
    except Exception as e:
        print(f"❌ edge-tts generation failed: {e}")
        return False, []


def group_words_into_captions(word_timings: list, words_per_group: int) -> list:
    """Group word timings into caption chunks of N words each, with combined start/end."""
    groups = []
    for i in range(0, len(word_timings), words_per_group):
        chunk = word_timings[i:i + words_per_group]
        if not chunk:
            continue
        groups.append({
            "text": " ".join(w["text"] for w in chunk),
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
        })
    return groups


def render_caption_image(text: str, video_width: int) -> np.ndarray:
    """Render a caption as an RGBA image: white text, black outline, on transparent background."""
    height = 220
    img = Image.new("RGBA", (video_width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(CAPTION_FONT_SIZE)

    # Wrap text if it's too wide for the frame
    words = text.split()
    lines, current = [], ""
    for w in words:
        trial = (current + " " + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] > video_width - 80 and current:
            lines.append(current)
            current = w
        else:
            current = trial
    if current:
        lines.append(current)

    line_height = CAPTION_FONT_SIZE + 14
    total_h = line_height * len(lines)
    y = (height - total_h) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (video_width - line_w) // 2
        draw.text((x, y), line, font=font, fill="white",
                   stroke_width=5, stroke_fill="black")
        y += line_height

    return np.array(img)


def build_caption_clips(word_timings: list, video_width: int, video_height: int) -> list:
    """Turn word timings into a list of positioned, timed ImageClips for burning in captions."""
    if not word_timings:
        return []
    groups = group_words_into_captions(word_timings, CAPTION_WORDS_PER_GROUP)
    clips = []
    for group in groups:
        duration = max(group["end"] - group["start"], 0.2)
        arr = render_caption_image(group["text"], video_width)
        rgb = arr[:, :, :3]
        alpha = arr[:, :, 3] / 255.0
        img_clip = ImageClip(rgb).set_duration(duration).set_start(group["start"])
        mask_clip = ImageClip(alpha, ismask=True).set_duration(duration)
        img_clip = img_clip.set_mask(mask_clip)
        img_clip = img_clip.set_position(("center", int(video_height * 0.70)))
        clips.append(img_clip)
    return clips


def search_pexels_clips(query: str, count: int = 5) -> list[str]:
    """Search Pexels for vertical video clips matching the query. Returns download URLs."""
    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": PEXELS_API_KEY}
    params = {"query": query, "per_page": count, "orientation": "portrait"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"❌ Pexels error {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        urls = []
        for video in data.get("videos", []):
            files = sorted(video["video_files"], key=lambda f: f.get("width", 0))
            # pick a mid-quality file to keep downloads fast
            pick = files[len(files) // 2] if files else None
            if pick:
                urls.append(pick["link"])
        return urls
    except Exception as e:
        print(f"❌ Pexels request failed: {e}")
        return []


def download_file(url: str, path: str) -> bool:
    try:
        resp = requests.get(url, stream=True, timeout=60)
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return False


def build_video(topic: str, narration: str, output_path: str):
    os.makedirs(WORK_DIR, exist_ok=True)
    audio_path = os.path.join(WORK_DIR, "voice.mp3")

    print("🎙️  Generating voiceover with edge-tts...")
    ok, word_timings = generate_voiceover_with_timings(narration, audio_path)
    if not ok:
        print("Skipping this video — voiceover failed.")
        return

    audio_clip = AudioFileClip(audio_path)
    target_duration = audio_clip.duration
    print(f"   Voiceover length: {target_duration:.1f}s")
    print(f"   Captured {len(word_timings)} word timings for captions")
    if not word_timings:
        print("   ⚠️  No word timing data from edge-tts — using estimated even spacing instead.")
        words = narration.split()
        if words:
            per_word = target_duration / len(words)
            t = 0.0
            for w in words:
                word_timings.append({"text": w, "start": t, "end": t + per_word})
                t += per_word

    print(f"🎥 Searching Pexels for footage matching: '{topic}'...")
    clip_urls = search_pexels_clips(topic, count=5)
    if not clip_urls:
        print("   No results for exact topic, trying a broader search...")
        clip_urls = search_pexels_clips(topic.split()[0], count=5)
    if not clip_urls:
        print("❌ No stock footage found. Skipping this video.")
        return

    downloaded_clips = []
    for i, url in enumerate(clip_urls):
        path = os.path.join(WORK_DIR, f"clip_{i}.mp4")
        print(f"   Downloading clip {i+1}/{len(clip_urls)}...")
        if download_file(url, path):
            downloaded_clips.append(path)

    if not downloaded_clips:
        print("❌ Failed to download any clips. Skipping this video.")
        return

    print("🎬 Assembling video...")
    video_clips = []
    running_time = 0.0
    random.shuffle(downloaded_clips)
    i = 0
    while running_time < target_duration:
        path = downloaded_clips[i % len(downloaded_clips)]
        clip = VideoFileClip(path)
        clip = clip.resize(height=TARGET_H) if clip.h < clip.w else clip.resize(width=TARGET_W)
        clip = clip.crop(
            x_center=clip.w / 2, y_center=clip.h / 2, width=TARGET_W, height=TARGET_H
        )
        video_clips.append(clip)
        running_time += clip.duration
        i += 1

    final_video = concatenate_videoclips(video_clips).subclip(0, target_duration)
    final_video = final_video.set_audio(audio_clip)

    if CAPTIONS_ENABLED and word_timings:
        print("💬 Adding synced captions...")
        caption_clips = build_caption_clips(word_timings, TARGET_W, TARGET_H)
        print(f"   Built {len(caption_clips)} caption clips")
        final_video = CompositeVideoClip([final_video] + caption_clips)

    print(f"💾 Rendering final video to: {output_path}")
    final_video.write_videofile(output_path, fps=30, codec="libx264", audio_codec="aac")

    for clip in video_clips:
        clip.close()
    audio_clip.close()


def main():
    if PEXELS_API_KEY == "YOUR_PEXELS_API_KEY":
        print("⚠️  Please fill in PEXELS_API_KEY at the top of this file first.")
        sys.exit(1)

    csv_path = input("Path to your scripts CSV (from the interactive generator): ").strip().strip('"')
    if not os.path.exists(csv_path):
        print("File not found.")
        return

    os.makedirs("output_videos", exist_ok=True)

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))

    print(f"\nFound {len(reader)} scripts. Which one do you want to turn into a video?")
    for i, row in enumerate(reader, 1):
        print(f"  {i}. {row['topic']}")
    choice = input("Enter number (or 'all' for every script): ").strip().lower()

    targets = reader if choice == "all" else [reader[int(choice) - 1]]

    for row in targets:
        narration = f"{row['hook']} {row['body']} {row['cta']}"
        safe_name = "".join(c for c in row["topic"] if c.isalnum() or c in " _-")[:50].strip()
        output_path = os.path.join("output_videos", f"{safe_name}.mp4")
        print(f"\n=== Building: {row['topic']} ===")
        build_video(row["topic"], narration, output_path)

    print("\n🎉 All done! Check the 'output_videos' folder.")


if __name__ == "__main__":
    main()
