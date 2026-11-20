"""
Buddy OS — Deterministic Output Verification
============================================
Soul vow #1: "Verify before I claim done."

Every rendered video passes through ffprobe BEFORE anything reports success.
No assumptions, no vibes — resolution, duration, and audio are measured facts.

    verify_video(path,
                 expect_w=1080, expect_h=1920,   # exact dims (optional)
                 min_duration=0.5,               # seconds
                 exact_duration=30.0, tol=2.0)   # window match (optional)
        -> {'ok': bool, 'checks': {...}, 'detail': 'what failed, plainly'}
"""

import json
import subprocess
from pathlib import Path
from typing import Dict, Optional


def probe(path) -> Optional[Dict]:
    """Raw ffprobe data, or None if the file can't be probed at all."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < 10_000:
        return None
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-print_format', 'json',
             '-show_format', '-show_streams', str(p)],
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except Exception:
        return None


def verify_video(path, expect_w: Optional[int] = None,
                 expect_h: Optional[int] = None,
                 min_duration: float = 0.5,
                 need_audio: bool = True,
                 exact_duration: Optional[float] = None,
                 tol: float = 2.5) -> Dict:
    checks: Dict[str, bool] = {}
    fails: list = []

    data = probe(path)
    checks['probeable'] = data is not None
    if not data:
        return {'ok': False, 'checks': checks,
                'detail': 'file missing/corrupt/too small for ffprobe'}

    vstreams = [s for s in data.get('streams', [])
                if s.get('codec_type') == 'video']
    astreams = [s for s in data.get('streams', [])
                if s.get('codec_type') == 'audio']

    checks['has_video'] = bool(vstreams)
    if not vstreams:
        fails.append('no video stream')

    if vstreams:
        vs = vstreams[0]
        w, h = vs.get('width'), vs.get('height')
        if expect_w is not None:
            checks[f'width={expect_w}'] = (w == expect_w)
            if w != expect_w:
                fails.append(f'width {w} != {expect_w}')
        if expect_h is not None:
            checks[f'height={expect_h}'] = (h == expect_h)
            if h != expect_h:
                fails.append(f'height {h} != {expect_h}')

        try:
            dur = float(data.get('format', {}).get('duration')
                        or vs.get('duration') or 0)
        except (TypeError, ValueError):
            dur = 0.0
        checks[f'duration>={min_duration}s'] = dur >= min_duration
        if dur < min_duration:
            fails.append(f'duration {dur:.2f}s < {min_duration}s')
        if exact_duration is not None:
            near = abs(dur - exact_duration) <= tol
            checks[f'duration≈{exact_duration}s±{tol}'] = near
            if not near:
                fails.append(f'duration {dur:.2f}s vs expected {exact_duration}s')

    if need_audio:
        checks['has_audio'] = bool(astreams)
        if not astreams:
            fails.append('no audio stream')

    return {'ok': not fails, 'checks': checks,
            'detail': '; '.join(fails) if fails else 'all checks passed'}
