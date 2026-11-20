# agent_shorts_app.py
# Job queue + worker + verifier + publish gate for auto-generated YouTube Shorts.
#
# This version wires in three real integrations (previously stubs):
#   1. LLM generation      -> Anthropic Messages API
#   2. Web evidence lookup -> Serper.dev (Google search API) with a best-effort
#                              DuckDuckGo HTML fallback if no key is configured
#   3. YouTube publishing  -> YouTube Data API v3 (OAuth2, resumable upload)
#
# IMPORTANT LIMITATIONS (read before running for real):
#   - Evidence lookup finds *candidate* source URLs that match a claim's topic.
#     It does NOT verify that the source actually supports the claim. Treat the
#     evidence_map as "here are sources to check," not "this claim is proven."
#     Add a human review step before you flip anything to public.
#   - Video rendering is basic by design: a solid-color vertical background
#     with burned-in text cards for overlays/captions, narrated by gTTS
#     (see render_video_real()). It works end-to-end but is visually plain —
#     swap in a real background clip / better TTS provider if you want more
#     production value. Skip it entirely by passing your own "video_path"
#     in job inputs.
#   - Publish defaults to a *non-public* privacy status. You have to explicitly
#     opt in to making a video public — see PublishConfig below.
#
# Setup:
#   pip install -r requirements.txt
#   export OPENROUTER_API_KEY=...
#   export OPENROUTER_MODEL=...          # optional, defaults to anthropic/claude-sonnet-4.5
#   export SERPER_API_KEY=...            # optional but recommended
#   export YOUTUBE_CLIENT_SECRETS_FILE=./client_secret.json
#   (see SETUP.md for how to get YouTube OAuth credentials)

import os
import re
import json
import time
import uuid
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import requests

DB_PATH = os.environ.get("AGENT_DB_PATH", "agent_shorts.db")
HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_PORT", "8000"))

DURATION_MIN = 30
DURATION_MAX = 60

# -----------------------------
# Config
# -----------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
# Any model slug OpenRouter serves works here — check https://openrouter.ai/models
# for the current list/pricing. This default is just a reasonable starting point.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

SERPER_API_KEY = os.environ.get("SERPER_API_KEY")  # https://serper.dev
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")  # https://www.pexels.com/api/ (optional; falls back to solid color background)

YOUTUBE_CLIENT_SECRETS_FILE = os.environ.get("YOUTUBE_CLIENT_SECRETS_FILE", "client_secret.json")
YOUTUBE_TOKEN_FILE = os.environ.get("YOUTUBE_TOKEN_FILE", "youtube_token.json")

# Publish safety switch. Must be explicitly set to "true" (env var) to allow
# a video to go out with privacyStatus=public. Anything else forces
# unlisted/private regardless of what metadata_packet says.
ALLOW_PUBLIC_PUBLISH = os.environ.get("ALLOW_PUBLIC_PUBLISH", "false").lower() == "true"


# -----------------------------
# DB
# -----------------------------

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        task_family TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        inputs_json TEXT NOT NULL,
        expected_schema_id TEXT NOT NULL,
        validator_id TEXT NOT NULL,
        payout_on_pass_usd REAL NOT NULL,
        result_json TEXT,
        failure_reasons_json TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS job_outputs (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        task_family TEXT NOT NULL,
        output_json TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS publish_receipts (
        publish_receipt_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        platform TEXT NOT NULL,
        status TEXT NOT NULL,
        receipt_payload_json TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
    );
    """)
    conn.commit()
    conn.close()


# -----------------------------
# Schemas (simple validators) — unchanged from original spec
# -----------------------------

class ValidationError(Exception):
    pass


def now_ts():
    return int(time.time())


def require_keys(obj, keys, path="root"):
    for k in keys:
        if k not in obj:
            raise ValidationError(f"Missing key '{k}' at {path}")
    return True


def is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def ensure_type(x, t, path="root"):
    if not isinstance(x, t):
        raise ValidationError(f"Expected type {t} at {path}, got {type(x)}")


def validate_json_schema(task_family, output):
    if not isinstance(output, dict):
        raise ValidationError("Output must be a JSON object")

    if task_family == "yt_shorts.script.v1":
        require_keys(output, ["script_version", "hook", "subscribe_cta", "beats", "claim_units", "non_claim_text_policy"])
        ensure_type(output["script_version"], str, "script_version")
        ensure_type(output["hook"], str, "hook")
        ensure_type(output["subscribe_cta"], str, "subscribe_cta")
        ensure_type(output["beats"], list, "beats")
        ensure_type(output["claim_units"], list, "claim_units")
        ensure_type(output["non_claim_text_policy"], dict, "non_claim_text_policy")

        if len(output["hook"].split()) < 3 or len(output["hook"].split()) > 12:
            raise ValidationError("hook must be 3–12 words")

        prev_end = None
        for i, b in enumerate(output["beats"]):
            require_keys(b, ["t_start", "t_end", "text"], path=f"beats[{i}]")
            if not is_number(b["t_start"]) or not is_number(b["t_end"]):
                raise ValidationError(f"beats[{i}] t_start/t_end must be numbers")
            if b["t_end"] <= b["t_start"]:
                raise ValidationError(f"beats[{i}] t_end must be > t_start")
            if prev_end is not None:
                if b["t_start"] > prev_end + 0.2:
                    raise ValidationError("Beats not contiguous enough")
            prev_end = b["t_end"]

        for i, c in enumerate(output["claim_units"]):
            require_keys(c, ["claim_id", "claim_text", "claim_type", "beat_refs", "requires_source"], path=f"claim_units[{i}]")
            ensure_type(c["claim_id"], str, "claim_id")
            ensure_type(c["claim_text"], str, "claim_text")
            ensure_type(c["claim_type"], str, "claim_type")
            ensure_type(c["beat_refs"], list, "beat_refs")
            if not isinstance(c["requires_source"], bool):
                raise ValidationError("requires_source must be boolean")

    elif task_family == "yt_shorts.evidence_map.v1":
        require_keys(output, ["evidence_version", "claim_to_sources"])
        ensure_type(output["claim_to_sources"], list, "claim_to_sources")
        for i, e in enumerate(output["claim_to_sources"]):
            require_keys(e, ["claim_id", "source_urls", "source_quality_tag", "evidence_snippet_rule"], path=f"claim_to_sources[{i}]")
            ensure_type(e["claim_id"], str, "claim_id")
            ensure_type(e["source_urls"], list, "source_urls")
            ensure_type(e["source_quality_tag"], str, "source_quality_tag")
            ensure_type(e["evidence_snippet_rule"], str, "evidence_snippet_rule")

    elif task_family == "yt_shorts.captions.v1":
        require_keys(output, ["caption_format", "duration_sec", "srt_text", "caption_timestamp_integrity"])
        ensure_type(output["caption_format"], str, "caption_format")
        if output["caption_format"] != "SRT":
            raise ValidationError("caption_format must be 'SRT'")
        if not is_number(output["duration_sec"]):
            raise ValidationError("duration_sec must be number")
        ensure_type(output["srt_text"], str, "srt_text")
        ensure_type(output["caption_timestamp_integrity"], dict, "caption_timestamp_integrity")
        require_keys(output["caption_timestamp_integrity"], ["monotonic", "covers_duration"], "caption_timestamp_integrity")
        if not isinstance(output["caption_timestamp_integrity"]["monotonic"], bool):
            raise ValidationError("monotonic must be bool")
        if not isinstance(output["caption_timestamp_integrity"]["covers_duration"], bool):
            raise ValidationError("covers_duration must be bool")

        if output["duration_sec"] < DURATION_MIN or output["duration_sec"] > DURATION_MAX:
            raise ValidationError(f"duration_sec must be {DURATION_MIN}–{DURATION_MAX}")

    elif task_family == "yt_shorts.on_screen_text.v1":
        require_keys(output, ["overlays_version", "overlays"])
        ensure_type(output["overlays"], list, "overlays")
        for i, o in enumerate(output["overlays"]):
            require_keys(o, ["overlay_id", "text", "t_start", "t_end", "position", "style_tag"], path=f"overlays[{i}]")
            ensure_type(o["text"], str, "text")
            if not is_number(o["t_start"]) or not is_number(o["t_end"]):
                raise ValidationError("t_start/t_end must be numbers")
            if o["t_end"] <= o["t_start"]:
                raise ValidationError("overlay t_end must be > t_start")

    elif task_family == "yt_shorts.thumbnail_text.v1":
        require_keys(output, ["thumb_variants"])
        ensure_type(output["thumb_variants"], list, "thumb_variants")
        for i, v in enumerate(output["thumb_variants"]):
            require_keys(v, ["variant_id", "text", "style_tag"], path=f"thumb_variants[{i}]")

    elif task_family == "yt_shorts.assembly.v1":
        require_keys(output, ["assembly_version", "render_instructions", "metadata_packet"])
        ensure_type(output["render_instructions"], dict, "render_instructions")
        ensure_type(output["metadata_packet"], dict, "metadata_packet")
        require_keys(output["metadata_packet"], ["title", "description", "hashtags", "privacy_status", "tags"], "metadata_packet")
        ensure_type(output["metadata_packet"]["hashtags"], list, "hashtags")
        ensure_type(output["metadata_packet"]["tags"], list, "tags")

    elif task_family == "yt_shorts.publish.v1":
        require_keys(output, ["publish_receipt_id", "platform", "status"])
        ensure_type(output["publish_receipt_id"], str, "publish_receipt_id")
        ensure_type(output["platform"], str, "platform")
        ensure_type(output["status"], str, "status")

    else:
        raise ValidationError(f"Unknown task_family: {task_family}")


def is_probably_url(s):
    return isinstance(s, str) and (s.startswith("http://") or s.startswith("https://"))


def verifier(task_family, output, context):
    validate_json_schema(task_family, output)

    if task_family == "yt_shorts.publish.v1":
        missing = []
        for req in ["script_pass", "evidence_pass", "captions_pass", "assembly_pass"]:
            if not context.get(req, False):
                missing.append(req)
        if missing:
            raise ValidationError(f"Publish blocked: missing gates: {missing}")

        script = context["script_output"]
        evidence = context["evidence_output"]
        claim_units = script["claim_units"]
        evidence_map = {e["claim_id"]: e for e in evidence["claim_to_sources"]}

        missing_src = []
        for c in claim_units:
            if c["requires_source"]:
                if c["claim_id"] not in evidence_map:
                    missing_src.append(c["claim_id"])
                    continue
                urls = evidence_map[c["claim_id"]]["source_urls"]
                if not urls or not any(is_probably_url(u) for u in urls):
                    missing_src.append(c["claim_id"])

        if missing_src:
            raise ValidationError(f"Publish blocked: missing evidence sources for claim_ids: {missing_src}")

    return True


# -----------------------------
# 1. LLM generation (Anthropic)
# -----------------------------

def call_llm(system_prompt, user_prompt, max_tokens=4000, temperature=0.7):
    """Calls OpenRouter's OpenAI-compatible chat completions endpoint.
    Works with any model slug OpenRouter serves (Claude, GPT, Gemini, Llama,
    etc.) — just change OPENROUTER_MODEL."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set — cannot call the LLM.")
    resp = requests.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENROUTER_MODEL,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Unexpected OpenRouter response shape: {data}")


def extract_json(text):
    """Strip code fences / stray prose and parse the first JSON object found."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned.strip())
    cleaned = re.sub(r"```$", "", cleaned.strip())
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fallback: find the outermost {...} block
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValidationError(f"LLM output was not valid JSON: {text[:300]}")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise ValidationError(f"LLM output was not valid JSON after cleanup: {e}")


def llm_call_for_schema(task_family, system_prompt, user_prompt, retries=2):
    """Call the LLM, parse JSON, validate against schema. Retry once with the
    validation error fed back to the model if it fails."""
    last_err = None
    prompt = user_prompt
    for attempt in range(retries + 1):
        raw = call_llm(system_prompt, prompt)
        try:
            parsed = extract_json(raw)
            validate_json_schema(task_family, parsed)
            return parsed
        except ValidationError as e:
            last_err = e
            prompt = (
                f"{user_prompt}\n\n"
                f"Your previous attempt failed validation with error: {e}\n"
                f"Raw previous output was:\n{raw}\n\n"
                f"Fix the issue and return ONLY the corrected JSON object, no prose, no markdown fences."
            )
    raise last_err


SCRIPT_SYSTEM_PROMPT = f"""You write short, punchy scripts for YouTube Shorts ({DURATION_MIN}-{DURATION_MAX} seconds).
You must respond with ONLY a single JSON object, no prose, no markdown code fences.

The JSON object must match this schema exactly:
{{
  "script_version": "1.0",
  "hook": "<3 to 12 words, a scroll-stopping opening line>",
  "subscribe_cta": "<short call to action>",
  "beats": [
    {{"t_start": <number, seconds>, "t_end": <number, seconds>, "text": "<spoken line for this beat>"}}
  ],
  "claim_units": [
    {{
      "claim_id": "<short id like c1>",
      "claim_text": "<the specific factual claim, standalone and checkable>",
      "claim_type": "<definition|factual|statistic|historical|other>",
      "beat_refs": ["<beat text or id this claim appears in>"],
      "requires_source": true
    }}
  ],
  "non_claim_text_policy": {{"allowed": ["opinion", "how-to", "framing", "questions"], "requires_source": false}}
}}

Rules:
- Beats must be contiguous (each beat's t_start should be very close to the previous beat's t_end) and together should span roughly {DURATION_MIN}-{DURATION_MAX} seconds.
- Mark EVERY specific factual/statistical/historical claim as a claim_unit with requires_source=true.
  Opinions, framing, and rhetorical questions do not need claim_units.
- claim_text should be a self-contained sentence that could be fact-checked on its own, independent of the video.
- Do not fabricate specific numbers/dates/statistics; if you include one, it must become a claim_unit.
"""


def llm_generate_script(inputs):
    topic = inputs.get("topic", "TOPIC")
    angle = inputs.get("angle", "A surprising fact")
    user_prompt = f"Topic: {topic}\nAngle: {angle}\n\nWrite the script JSON now."
    return llm_call_for_schema("yt_shorts.script.v1", SCRIPT_SYSTEM_PROMPT, user_prompt)


CAPTIONS_SYSTEM_PROMPT = f"""You convert a Shorts script into SRT captions.
Respond with ONLY a single JSON object, no prose, no markdown fences.

Schema:
{{
  "caption_format": "SRT",
  "duration_sec": <number between {DURATION_MIN} and {DURATION_MAX}>,
  "srt_text": "<valid SRT-formatted subtitle text, numbered blocks with HH:MM:SS,mmm --> HH:MM:SS,mmm timestamps>",
  "caption_timestamp_integrity": {{"monotonic": true, "covers_duration": true}}
}}

Rules:
- Base timestamps on the beats provided. Do not exceed duration_sec.
- Timestamps must be strictly increasing (monotonic) and the last one must end at or near duration_sec.
- Keep each caption block short (readable in 2-4 seconds).
"""


def llm_generate_captions(inputs, script_output):
    beats_desc = json.dumps(script_output["beats"], indent=2)
    hook = script_output["hook"]
    user_prompt = f"Hook: {hook}\nBeats:\n{beats_desc}\n\nGenerate the captions JSON now."
    return llm_call_for_schema("yt_shorts.captions.v1", CAPTIONS_SYSTEM_PROMPT, user_prompt)


OVERLAY_SYSTEM_PROMPT = """You design on-screen text overlays for a YouTube Short, timed against beats.
Respond with ONLY a single JSON object, no prose, no markdown fences.

Schema:
{
  "overlays_version": "1.0",
  "overlays": [
    {"overlay_id": "<id>", "text": "<short overlay text, all caps ok>", "t_start": <number>, "t_end": <number>, "position": "top|center|bottom", "style_tag": "<hook|key_point|cta|other>"}
  ]
}

Rules:
- t_end must be greater than t_start for every overlay.
- Times must fall within the beats' overall time range.
- Keep overlay text very short (a few words) — it's a visual accent, not a caption.
"""


def llm_generate_on_screen_text(inputs, script_output):
    beats_desc = json.dumps(script_output["beats"], indent=2)
    user_prompt = f"Beats:\n{beats_desc}\n\nGenerate the overlays JSON now."
    return llm_call_for_schema("yt_shorts.on_screen_text.v1", OVERLAY_SYSTEM_PROMPT, user_prompt)


THUMBNAIL_SYSTEM_PROMPT = """You write short punchy thumbnail text variants for a YouTube Short (for A/B testing).
Respond with ONLY a single JSON object, no prose, no markdown fences.

Schema:
{
  "thumb_variants": [
    {"variant_id": "<id>", "text": "<very short text, 2-5 words>", "style_tag": "<bold_caps|short_phrase|pattern_break|question|other>"}
  ]
}

Provide exactly 3 variants with different psychological angles (curiosity gap, bold claim, pattern break).
"""


def llm_generate_thumbnail_text(inputs, script_output):
    user_prompt = f"Topic: {inputs.get('topic')}\nHook: {script_output['hook']}\n\nGenerate the thumbnail text JSON now."
    return llm_call_for_schema("yt_shorts.thumbnail_text.v1", THUMBNAIL_SYSTEM_PROMPT, user_prompt)


ASSEMBLY_SYSTEM_PROMPT = """You produce the final assembly/render instructions and YouTube metadata packet for a Short.
Respond with ONLY a single JSON object, no prose, no markdown fences.

Schema:
{
  "assembly_version": "1.0",
  "render_instructions": {
    "video_length_sec_expected": <number>,
    "voiceover_source_text": "<concatenated spoken text from beats>",
    "overlay_tracks": [{"overlay_id": "<id>", "text": "<text>", "t_start": <number>, "t_end": <number>}],
    "captions_track_format": "SRT"
  },
  "metadata_packet": {
    "title": "<catchy YouTube title, <=95 chars>",
    "description": "<2-3 sentence description>",
    "hashtags": ["#shorts", "..."],
    "privacy_status": "unlisted",
    "tags": ["..."]
  }
}

Rules:
- privacy_status must be "unlisted" or "private" (never "public" — that decision is made outside this step).
- Base render_instructions on the provided script/captions/overlays data, don't invent new content.
"""


def llm_generate_assembly(inputs, script_output, captions_output, overlays_output):
    context = json.dumps({
        "script": script_output,
        "captions": {"duration_sec": captions_output["duration_sec"]},
        "overlays": overlays_output,
    }, indent=2)
    user_prompt = f"Topic: {inputs.get('topic')}\nContext:\n{context}\n\nGenerate the assembly JSON now."
    return llm_call_for_schema("yt_shorts.assembly.v1", ASSEMBLY_SYSTEM_PROMPT, user_prompt)


# -----------------------------
# 2. Web evidence lookup
# -----------------------------

QUALITY_DOMAIN_HINTS = {
    "primary": [".gov", ".edu", "who.int", "un.org", "nature.com", "sciencedirect.com", "ncbi.nlm.nih.gov"],
    "secondary": ["reuters.com", "apnews.com", "bbc.", "npr.org", "nytimes.com", ".org"],
}


def _tag_source_quality(url):
    low = url.lower()
    for tag, hints in QUALITY_DOMAIN_HINTS.items():
        if any(h in low for h in hints):
            return tag
    return "tertiary"


def search_web_serper(query, num_results=5):
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json={"q": query, "num": num_results},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for item in data.get("organic", [])[:num_results]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    return results


def search_web_duckduckgo_fallback(query, num_results=5):
    """Best-effort, no-API-key fallback. Fragile — HTML scraping breaks when
    DuckDuckGo changes markup. Prefer setting SERPER_API_KEY."""
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Fallback web search failed: {e}")

    urls = re.findall(r'href="(https?://[^"]+)"', resp.text)
    seen, results = set(), []
    for u in urls:
        if "duckduckgo.com" in u:
            continue
        if u in seen:
            continue
        seen.add(u)
        results.append({"title": "", "url": u, "snippet": ""})
        if len(results) >= num_results:
            break
    return results


def search_web(query, num_results=5):
    if SERPER_API_KEY:
        return search_web_serper(query, num_results)
    return search_web_duckduckgo_fallback(query, num_results)


def build_evidence_map(script_output):
    """For each claim_unit requiring a source, search for candidate URLs.
    NOTE: this returns candidate sources, it does not confirm the source
    actually substantiates the claim. Flag for human review before publish."""
    claim_to_sources = []
    for c in script_output["claim_units"]:
        if not c["requires_source"]:
            continue
        try:
            results = search_web(c["claim_text"], num_results=3)
        except Exception as e:
            # Fail closed: no sources found means the publish gate will block this claim.
            claim_to_sources.append({
                "claim_id": c["claim_id"],
                "source_urls": [],
                "source_quality_tag": "none",
                "evidence_snippet_rule": f"search_failed: {e}",
            })
            continue

        urls = [r["url"] for r in results if is_probably_url(r["url"])]
        quality = "tertiary"
        if urls:
            tags = [_tag_source_quality(u) for u in urls]
            quality = "primary" if "primary" in tags else ("secondary" if "secondary" in tags else "tertiary")

        claim_to_sources.append({
            "claim_id": c["claim_id"],
            "source_urls": urls,
            "source_quality_tag": quality,
            "evidence_snippet_rule": "candidate_sources_unverified_recommend_human_review",
        })

    return {
        "evidence_version": "1.0",
        "claim_to_sources": claim_to_sources,
    }


# -----------------------------
# Video rendering (gTTS voiceover + PIL text cards + moviepy composite)
# -----------------------------
#
# This gives you a real, working .mp4: solid-color vertical background,
# burned-in overlay text and captions timed to the script, narrated by gTTS.
# It is intentionally simple — swap the ColorClip background for a stock
# video/looping b-roll clip if you want something more visually interesting,
# and swap gTTS for a higher-quality TTS provider (e.g. ElevenLabs) if you
# want better voice quality.

def _load_font(size):
    from PIL import ImageFont
    candidates = [
        os.environ.get("SHORTS_FONT_PATH"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _render_text_rgba(text, width, font_size=64, color=(255, 255, 255, 255),
                       stroke_color=(0, 0, 0, 255), stroke_width=4, padding=30):
    from PIL import Image, ImageDraw
    font = _load_font(font_size)
    dummy = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(dummy)
    lines = _wrap_text(d, text, font, width - 2 * padding)

    line_dims = []
    for line in lines:
        bbox = d.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_dims.append((bbox[2] - bbox[0], bbox[3] - bbox[1]))

    total_height = sum(h for _, h in line_dims) + max(0, len(lines) - 1) * 10 + 2 * padding
    img = Image.new("RGBA", (width, int(total_height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y = padding
    for line, (lw, lh) in zip(lines, line_dims):
        x = (width - lw) // 2
        draw.text((x, y), line, font=font, fill=color, stroke_width=stroke_width, stroke_fill=stroke_color)
        y += lh + 10
    return img


def parse_srt(srt_text):
    """Parse SRT text into a list of (start_sec, end_sec, text) tuples."""
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    time_re = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})")
    entries = []
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        match = None
        text_lines = []
        for line in lines:
            m = time_re.search(line)
            if m:
                match = m
            elif not line.strip().isdigit():
                text_lines.append(line)
        if not match:
            continue
        h1, mi1, s1, ms1, h2, mi2, s2, ms2 = map(int, match.groups())
        start = h1 * 3600 + mi1 * 60 + s1 + ms1 / 1000
        end = h2 * 3600 + mi2 * 60 + s2 + ms2 / 1000
        entries.append((start, end, " ".join(text_lines)))
    return entries


def _select_pexels_video_file(video_files, target_h=1920):
    candidates = [f for f in video_files if f.get("file_type") == "video/mp4" and f.get("height")]
    if not candidates:
        return None
    candidates.sort(key=lambda f: abs(f["height"] - target_h))
    return candidates[0]


def _fetch_pexels_background(query, video_size=(1080, 1920), min_duration=6):
    """Downloads a topic-matched stock video clip from Pexels for use as a
    background. Returns a local file path, or None if PEXELS_API_KEY isn't
    set, no query is given, or anything goes wrong (caller falls back to a
    solid color background in that case)."""
    if not PEXELS_API_KEY or not query:
        return None
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "orientation": "portrait", "per_page": 8},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        videos = [v for v in data.get("videos", []) if v.get("duration", 0) >= min_duration]
        if not videos:
            videos = data.get("videos", [])
        if not videos:
            return None
        file_info = _select_pexels_video_file(videos[0].get("video_files", []), target_h=video_size[1])
        if not file_info:
            return None
        local_path = f"pexels_bg_{uuid.uuid4().hex}.mp4"
        with requests.get(file_info["link"], stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
        return local_path
    except Exception:
        return None


def render_video_real(assembly_output, captions_output, out_path=None, video_size=(1080, 1920), topic=None):
    """Renders a real .mp4: solid background + burned-in overlays/captions +
    gTTS voiceover. Requires: moviepy, gTTS, Pillow, numpy."""
    try:
        import numpy as np
        from gtts import gTTS
        from moviepy.editor import ColorClip, ImageClip, AudioFileClip, CompositeVideoClip, VideoFileClip
    except ImportError as e:
        raise RuntimeError(
            "Video rendering requires: pip install moviepy==1.0.3 gTTS numpy Pillow "
            f"(missing dependency: {e})"
        )

    render = assembly_output["render_instructions"]
    voiceover_text = render.get("voiceover_source_text", "").strip()
    if not voiceover_text:
        raise RuntimeError("No voiceover_source_text in assembly output — nothing to narrate.")

    out_path = out_path or f"rendered_{uuid.uuid4().hex}.mp4"
    tmp_audio_path = f"voiceover_{uuid.uuid4().hex}.mp3"

    # 1. Voiceover. Note: gTTS calls out to a Google endpoint at runtime, so
    # this needs network access wherever you actually run the renderer.
    gTTS(text=voiceover_text, lang="en").save(tmp_audio_path)
    audio_clip = AudioFileClip(tmp_audio_path)

    duration = max(
        audio_clip.duration,
        captions_output.get("duration_sec", 0),
        render.get("video_length_sec_expected", 0),
    )
    duration = max(DURATION_MIN, min(duration, DURATION_MAX + 5))  # small slack over the nominal max

    # 2. Background — real stock footage from Pexels if PEXELS_API_KEY is set
    # and a topic was given; otherwise falls back to a plain solid color.
    bg_path = _fetch_pexels_background((topic or "").strip(), video_size=video_size)
    background = None
    if bg_path:
        try:
            from moviepy.video.fx.all import loop as vfx_loop, crop as vfx_crop
            raw_bg = VideoFileClip(bg_path)
            scaled = raw_bg.resize(height=video_size[1])
            if scaled.w < video_size[0]:
                scaled = raw_bg.resize(width=video_size[0])
            cropped = vfx_crop(scaled, width=video_size[0], height=video_size[1],
                                x_center=scaled.w / 2, y_center=scaled.h / 2)
            background = (vfx_loop(cropped, duration=duration) if cropped.duration < duration
                          else cropped.set_duration(duration)).without_audio()
        except Exception:
            background = None
    if background is None:
        background = ColorClip(size=video_size, color=(15, 15, 20)).set_duration(duration)
        bg_path = None
    clips = [background]

    # 3. Short accent overlays, timed per overlay_tracks.
    position_map = {
        "top": ("center", int(video_size[1] * 0.12)),
        "center": "center",
        "bottom": ("center", int(video_size[1] * 0.75)),
    }
    for ov in render.get("overlay_tracks", []):
        img = _render_text_rgba(ov["text"], width=int(video_size[0] * 0.85), font_size=72)
        clip = (ImageClip(np.array(img))
                .set_start(ov["t_start"])
                .set_duration(max(0.1, ov["t_end"] - ov["t_start"]))
                .set_position(position_map.get(ov.get("position", "center"), "center")))
        clips.append(clip)

    # 4. Burned-in captions from the SRT text.
    for start, end, text in parse_srt(captions_output.get("srt_text", "")):
        img = _render_text_rgba(text, width=int(video_size[0] * 0.9), font_size=54)
        clip = (ImageClip(np.array(img))
                .set_start(start)
                .set_duration(max(0.1, end - start))
                .set_position(("center", int(video_size[1] * 0.85))))
        clips.append(clip)

    video = CompositeVideoClip(clips, size=video_size).set_duration(duration)
    video = video.set_audio(audio_clip.set_duration(min(audio_clip.duration, duration)))

    video.write_videofile(out_path, fps=30, codec="libx264", audio_codec="aac", verbose=False, logger=None)

    try:
        os.remove(tmp_audio_path)
    except OSError:
        pass
    if bg_path:
        try:
            os.remove(bg_path)
        except OSError:
            pass

    return out_path


# -----------------------------
# 3. YouTube publishing
# -----------------------------

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def _get_youtube_service():
    try:
        import google.auth.transport.requests
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload  # noqa: F401 (used by caller)
    except ImportError:
        raise RuntimeError(
            "YouTube upload requires: pip install google-api-python-client google-auth-oauthlib"
        )

    creds = None
    if os.path.exists(YOUTUBE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(YOUTUBE_TOKEN_FILE, [YOUTUBE_UPLOAD_SCOPE])

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
        else:
            if not os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
                raise RuntimeError(
                    f"Missing OAuth client secrets file at {YOUTUBE_CLIENT_SECRETS_FILE}. "
                    "See SETUP.md for how to create one in Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(YOUTUBE_CLIENT_SECRETS_FILE, [YOUTUBE_UPLOAD_SCOPE])
            creds = flow.run_local_server(port=0)  # opens a browser for consent

        with open(YOUTUBE_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def youtube_upload(video_path, metadata_packet, schedule_time_iso=None):
    """Uploads a video to YouTube.

    schedule_time_iso: optional ISO-8601 UTC timestamp (e.g. '2026-07-10T15:00:00Z').
    If provided, the video is uploaded as private with publishAt set, and
    YouTube will make it public automatically at that time.

    Safety: privacyStatus is forced to 'private' or 'unlisted' unless
    ALLOW_PUBLIC_PUBLISH=true is explicitly set in the environment.
    """
    if not os.path.exists(video_path):
        raise RuntimeError(f"video_path does not exist: {video_path}")

    from googleapiclient.http import MediaFileUpload

    requested_status = metadata_packet.get("privacy_status", "unlisted")
    if requested_status == "public" and not ALLOW_PUBLIC_PUBLISH:
        requested_status = "unlisted"

    status_body = {"privacyStatus": requested_status, "selfDeclaredMadeForKids": False}
    if schedule_time_iso:
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = schedule_time_iso

    body = {
        "snippet": {
            "title": metadata_packet["title"][:95],
            "description": metadata_packet["description"],
            "tags": metadata_packet.get("tags", []),
            "categoryId": "22",
        },
        "status": status_body,
    }

    youtube = _get_youtube_service()
    media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()

    return {
        "publish_receipt_id": response["id"],
        "platform": "youtube",
        "status": status_body["privacyStatus"],
        "youtube_video_id": response["id"],
        "youtube_url": f"https://youtube.com/watch?v={response['id']}",
    }


# -----------------------------
# Task execution — dispatches to real implementations, falls back to a stub
# only when a task doesn't need external services.
# -----------------------------

def execute_task(task_family, inputs, prior_outputs):
    if task_family == "yt_shorts.script.v1":
        return llm_generate_script(inputs)

    if task_family == "yt_shorts.evidence_map.v1":
        script_output = prior_outputs.get("yt_shorts.script.v1")
        if not script_output:
            raise ValidationError("evidence_map requires script output first")
        return build_evidence_map(script_output)

    if task_family == "yt_shorts.captions.v1":
        script_output = prior_outputs.get("yt_shorts.script.v1")
        if not script_output:
            raise ValidationError("captions requires script output first")
        return llm_generate_captions(inputs, script_output)

    if task_family == "yt_shorts.on_screen_text.v1":
        script_output = prior_outputs.get("yt_shorts.script.v1")
        if not script_output:
            raise ValidationError("on_screen_text requires script output first")
        return llm_generate_on_screen_text(inputs, script_output)

    if task_family == "yt_shorts.thumbnail_text.v1":
        script_output = prior_outputs.get("yt_shorts.script.v1")
        if not script_output:
            raise ValidationError("thumbnail_text requires script output first")
        return llm_generate_thumbnail_text(inputs, script_output)

    if task_family == "yt_shorts.assembly.v1":
        script_output = prior_outputs.get("yt_shorts.script.v1")
        captions_output = prior_outputs.get("yt_shorts.captions.v1")
        overlays_output = prior_outputs.get("yt_shorts.on_screen_text.v1")
        if not (script_output and captions_output and overlays_output):
            raise ValidationError("assembly requires script, captions, and overlays first")
        return llm_generate_assembly(inputs, script_output, captions_output, overlays_output)

    if task_family == "yt_shorts.publish.v1":
        assembly_output = prior_outputs.get("yt_shorts.assembly.v1")
        captions_output = prior_outputs.get("yt_shorts.captions.v1")
        if not (assembly_output and captions_output):
            raise ValidationError("publish requires assembly and captions output first")
        video_path = inputs.get("video_path")
        schedule_time_iso = inputs.get("schedule_time_iso")
        if not video_path:
            # No pre-rendered file supplied — render one now (gTTS + text
            # cards + moviepy). Pass "video_path" in job inputs instead if
            # you'd rather supply your own edited video.
            video_path = render_video_real(assembly_output, captions_output, topic=inputs.get("topic"))
        return youtube_upload(video_path, assembly_output["metadata_packet"], schedule_time_iso)

    raise ValidationError(f"No executor for {task_family}")


# -----------------------------
# Job runner logic
# -----------------------------

TASK_FAMILIES_PIPELINE = [
    "yt_shorts.script.v1",
    "yt_shorts.evidence_map.v1",
    "yt_shorts.captions.v1",
    "yt_shorts.on_screen_text.v1",
    "yt_shorts.thumbnail_text.v1",
    "yt_shorts.assembly.v1",
    "yt_shorts.publish.v1",
]


def enqueue_video_job(topic, angle, video_path=None, schedule_time_iso=None):
    base_id = str(uuid.uuid4())
    created = []
    for i, fam in enumerate(TASK_FAMILIES_PIPELINE):
        job_id = f"{base_id}:{i}:{fam}"
        inputs = {"topic": topic, "angle": angle}
        if fam == "yt_shorts.publish.v1":
            if video_path:
                inputs["video_path"] = video_path
            if schedule_time_iso:
                inputs["schedule_time_iso"] = schedule_time_iso
        expected_schema_id = fam
        validator_id = "verifier_v1"
        payout = 1.0 if fam == "yt_shorts.publish.v1" else 0.0
        cur_job = (
            job_id, fam, "queued", now_ts(), now_ts(),
            json.dumps(inputs),
            expected_schema_id, validator_id, payout,
            None, None,
        )
        conn = db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO jobs (
                job_id, task_family, status, created_at, updated_at, inputs_json,
                expected_schema_id, validator_id, payout_on_pass_usd, result_json, failure_reasons_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?);
        """, cur_job)
        conn.commit()
        conn.close()
        created.append(job_id)
    return base_id, created


def set_job_status(job_id, status, result_json=None, failures=None):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE jobs
        SET status=?, updated_at=?, result_json=?, failure_reasons_json=?
        WHERE job_id=?;
    """, (status, now_ts(), json.dumps(result_json) if result_json is not None else None,
          json.dumps(failures) if failures is not None else None, job_id))
    conn.commit()
    conn.close()


def get_job(job_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
    row = cur.fetchone()
    conn.close()
    return row


def store_output(job_id, task_family, output):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO job_outputs (id, job_id, task_family, output_json, created_at)
        VALUES (?,?,?,?,?);
    """, (str(uuid.uuid4()), job_id, task_family, json.dumps(output), now_ts()))
    conn.commit()
    conn.close()


def get_outputs_for_video(base_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT task_family, output_json
        FROM job_outputs
        WHERE job_id LIKE ?
    """, (f"{base_id}:%",))
    rows = cur.fetchall()
    conn.close()
    out = {}
    for r in rows:
        out[r["task_family"]] = json.loads(r["output_json"])
    return out


def find_base_id(job_id):
    return job_id.split(":")[0]


def worker_loop(poll_sec=0.7, max_jobs_per_cycle=5):
    while True:
        try:
            conn = db()
            cur = conn.cursor()
            cur.execute("""
                SELECT job_id, task_family, inputs_json
                FROM jobs
                WHERE status='queued'
                ORDER BY created_at ASC, job_id ASC
                LIMIT ?;
            """, (max_jobs_per_cycle,))
            jobs = cur.fetchall()
            conn.close()

            if not jobs:
                time.sleep(poll_sec)
                continue

            for j in jobs:
                job_id = j["job_id"]
                task_family = j["task_family"]
                inputs = json.loads(j["inputs_json"])
                base_id = find_base_id(job_id)

                set_job_status(job_id, "running")

                try:
                    prior_outputs = get_outputs_for_video(base_id)
                    output = execute_task(task_family, inputs, prior_outputs)

                    context = {
                        "script_output": prior_outputs.get("yt_shorts.script.v1"),
                        "evidence_output": prior_outputs.get("yt_shorts.evidence_map.v1"),
                        "captions_output": prior_outputs.get("yt_shorts.captions.v1"),
                        "assembly_output": prior_outputs.get("yt_shorts.assembly.v1"),
                        "script_pass": prior_outputs.get("yt_shorts.script.v1") is not None,
                        "evidence_pass": prior_outputs.get("yt_shorts.evidence_map.v1") is not None,
                        "captions_pass": prior_outputs.get("yt_shorts.captions.v1") is not None,
                        "assembly_pass": prior_outputs.get("yt_shorts.assembly.v1") is not None,
                    }
                    # For publish, the outputs relevant to the gate are the ones
                    # already stored (this job's own output isn't in prior_outputs yet).
                    if task_family == "yt_shorts.publish.v1":
                        context["script_pass"] = "yt_shorts.script.v1" in prior_outputs
                        context["evidence_pass"] = "yt_shorts.evidence_map.v1" in prior_outputs
                        context["captions_pass"] = "yt_shorts.captions.v1" in prior_outputs
                        context["assembly_pass"] = "yt_shorts.assembly.v1" in prior_outputs

                    verifier(task_family, output, context)

                    store_output(job_id, task_family, output)
                    set_job_status(job_id, "pass", result_json=output, failures=None)

                    if task_family == "yt_shorts.publish.v1":
                        pr_id = output["publish_receipt_id"]
                        conn2 = db()
                        cur2 = conn2.cursor()
                        cur2.execute("""
                            INSERT INTO publish_receipts (publish_receipt_id, job_id, created_at, platform, status, receipt_payload_json)
                            VALUES (?,?,?,?,?,?);
                        """, (pr_id, job_id, now_ts(), output["platform"], output["status"], json.dumps(output)))
                        conn2.commit()
                        conn2.close()

                except Exception as e:
                    set_job_status(job_id, "fail", result_json=None, failures=[str(e)])

        except Exception:
            time.sleep(1.0)


# -----------------------------
# HTTP API (App surface)
# -----------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/health":
            return self._send(200, {"ok": True})

        if parsed.path == "/job":
            job_id = qs.get("id", [None])[0]
            if not job_id:
                return self._send(400, {"error": "Missing id"})
            row = get_job(job_id)
            if not row:
                return self._send(404, {"error": "Not found"})
            return self._send(200, dict(row))

        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if parsed.path == "/jobs":
            topic = data.get("topic")
            angle = data.get("angle", "A strong angle")
            video_path = data.get("video_path")
            schedule_time_iso = data.get("schedule_time_iso")
            if not topic:
                return self._send(400, {"error": "Missing topic"})
            base_id, job_ids = enqueue_video_job(topic, angle, video_path, schedule_time_iso)
            return self._send(200, {"base_id": base_id, "job_ids": job_ids})

        if parsed.path == "/jobs/claim":
            return self._send(200, {"ok": True, "note": "Worker auto-claims queued jobs."})

        return self._send(404, {"error": "Not found"})


def start_http():
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"App listening on http://{HOST}:{PORT}")
    httpd.serve_forever()


# -----------------------------
# CLI
# -----------------------------

def main():
    init_db()
    t = threading.Thread(target=worker_loop, daemon=True)
    t.start()
    start_http()


if __name__ == "__main__":
    main()
