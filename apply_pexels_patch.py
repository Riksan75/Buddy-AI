import io

path = "agent_shorts_app.py"
with io.open(path, "r", encoding="utf-8") as f:
    src = f.read()

edits = [
    (
        'SERPER_API_KEY = os.environ.get("SERPER_API_KEY")  # https://serper.dev',
        'SERPER_API_KEY = os.environ.get("SERPER_API_KEY")  # https://serper.dev\n'
        'PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")  # https://www.pexels.com/api/ (optional; falls back to solid color background)'
    ),
    (
        'from moviepy.editor import ColorClip, ImageClip, AudioFileClip, CompositeVideoClip',
        'from moviepy.editor import ColorClip, ImageClip, AudioFileClip, CompositeVideoClip, VideoFileClip'
    ),
    (
        'def render_video_real(assembly_output, captions_output, out_path=None, video_size=(1080, 1920)):',
        'def _select_pexels_video_file(video_files, target_h=1920):\n'
        '    candidates = [f for f in video_files if f.get("file_type") == "video/mp4" and f.get("height")]\n'
        '    if not candidates:\n'
        '        return None\n'
        '    candidates.sort(key=lambda f: abs(f["height"] - target_h))\n'
        '    return candidates[0]\n'
        '\n'
        '\n'
        'def _fetch_pexels_background(query, video_size=(1080, 1920), min_duration=6):\n'
        '    """Downloads a topic-matched stock video clip from Pexels for use as a\n'
        '    background. Returns a local file path, or None if PEXELS_API_KEY isn\'t\n'
        '    set, no query is given, or anything goes wrong (caller falls back to a\n'
        '    solid color background in that case)."""\n'
        '    if not PEXELS_API_KEY or not query:\n'
        '        return None\n'
        '    try:\n'
        '        resp = requests.get(\n'
        '            "https://api.pexels.com/videos/search",\n'
        '            headers={"Authorization": PEXELS_API_KEY},\n'
        '            params={"query": query, "orientation": "portrait", "per_page": 8},\n'
        '            timeout=20,\n'
        '        )\n'
        '        resp.raise_for_status()\n'
        '        data = resp.json()\n'
        '        videos = [v for v in data.get("videos", []) if v.get("duration", 0) >= min_duration]\n'
        '        if not videos:\n'
        '            videos = data.get("videos", [])\n'
        '        if not videos:\n'
        '            return None\n'
        '        file_info = _select_pexels_video_file(videos[0].get("video_files", []), target_h=video_size[1])\n'
        '        if not file_info:\n'
        '            return None\n'
        '        local_path = f"pexels_bg_{uuid.uuid4().hex}.mp4"\n'
        '        with requests.get(file_info["link"], stream=True, timeout=60) as r:\n'
        '            r.raise_for_status()\n'
        '            with open(local_path, "wb") as f:\n'
        '                for chunk in r.iter_content(chunk_size=1 << 16):\n'
        '                    f.write(chunk)\n'
        '        return local_path\n'
        '    except Exception:\n'
        '        return None\n'
        '\n'
        '\n'
        'def render_video_real(assembly_output, captions_output, out_path=None, video_size=(1080, 1920), topic=None):'
    ),
    (
        '    # 2. Background \u2014 plain color, vertical (Shorts) aspect ratio.\n'
        '    background = ColorClip(size=video_size, color=(15, 15, 20)).set_duration(duration)\n'
        '    clips = [background]',
        '    # 2. Background \u2014 real stock footage from Pexels if PEXELS_API_KEY is set\n'
        '    # and a topic was given; otherwise falls back to a plain solid color.\n'
        '    bg_path = _fetch_pexels_background((topic or "").strip(), video_size=video_size)\n'
        '    background = None\n'
        '    if bg_path:\n'
        '        try:\n'
        '            from moviepy.video.fx.all import loop as vfx_loop, crop as vfx_crop\n'
        '            raw_bg = VideoFileClip(bg_path)\n'
        '            scaled = raw_bg.resize(height=video_size[1])\n'
        '            if scaled.w < video_size[0]:\n'
        '                scaled = raw_bg.resize(width=video_size[0])\n'
        '            cropped = vfx_crop(scaled, width=video_size[0], height=video_size[1],\n'
        '                                x_center=scaled.w / 2, y_center=scaled.h / 2)\n'
        '            background = (vfx_loop(cropped, duration=duration) if cropped.duration < duration\n'
        '                          else cropped.set_duration(duration)).without_audio()\n'
        '        except Exception:\n'
        '            background = None\n'
        '    if background is None:\n'
        '        background = ColorClip(size=video_size, color=(15, 15, 20)).set_duration(duration)\n'
        '        bg_path = None\n'
        '    clips = [background]'
    ),
    (
        '    try:\n'
        '        os.remove(tmp_audio_path)\n'
        '    except OSError:\n'
        '        pass\n'
        '\n'
        '    return out_path',
        '    try:\n'
        '        os.remove(tmp_audio_path)\n'
        '    except OSError:\n'
        '        pass\n'
        '    if bg_path:\n'
        '        try:\n'
        '            os.remove(bg_path)\n'
        '        except OSError:\n'
        '            pass\n'
        '\n'
        '    return out_path'
    ),
    (
        '            video_path = render_video_real(assembly_output, captions_output)',
        '            video_path = render_video_real(assembly_output, captions_output, topic=inputs.get("topic"))'
    ),
]

for old, new in edits:
    count = src.count(old)
    if count != 1:
        print(f"WARNING: expected 1 match, found {count}, for anchor starting: {old[:60]!r}")
    else:
        src = src.replace(old, new, 1)

with io.open(path, "w", encoding="utf-8") as f:
    f.write(src)

print("Patch applied.")
