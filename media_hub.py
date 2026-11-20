"""
media_hub.py  -  ONE place that drives ComfyUI, Voicebox and Blender-MCP.

    python media_hub.py health                  check all three backends + ffmpeg
    python media_hub.py probe                   list the REST routes Voicebox really exposes
    python media_hub.py produce omer-drama.json build a narrated video, scene by scene
    python media_hub.py produce cuts.json --only 2
    python media_hub.py speak "Hello there"     one narration clip
    python media_hub.py comfy "a lost child in a jungle"   one ComfyUI clip
    python media_hub.py blender                 render one Blender test frame

Per scene the hub does:   visuals (ComfyUI -> Blender-MCP -> plain title card)
                        + narration (Voicebox, optional)
                        + ffmpeg mux, then concatenates all scenes into final.mp4.
Whatever is down is skipped and reported in "warnings"; the pipeline still finishes.

Only the standard library is used. ffmpeg/ffprobe must be on PATH.
Config: media_config.json in the project root and/or BUDDY_* environment variables (see DEFAULTS).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


# --------------------------------------------------------------------------- config
def _find_root() -> Path:
    """Project root. Order: BUDDY_ROOT, then the nearest folder (here or up to 3 levels up) that holds one of
    the root-only files below, then a folder containing workspace/. Never just "the nearest workspace/":
    stray core/workspace folders are created by older code and used to fool this check."""
    env = os.environ.get("BUDDY_ROOT")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve().parent
    candidates = (here, *list(here.parents)[:3])
    markers = ("soul.md", "capabilities.md", "run_buddy.py", "api.py", ".env", "dashboard.html")
    for p in candidates:
        try:
            names = {x.name.lower() for x in p.iterdir()}
        except OSError:
            continue
        if any(m in names for m in markers):
            return p
    for p in candidates:
        if (p / "workspace").is_dir():
            return p
    return here


ROOT = _find_root()

DEFAULTS = {
    "comfy_url": "http://127.0.0.1:8188",
    "comfy_workflow": "",          # path to a workflow exported with "Save (API Format)"
    "comfy_prompt_nodes": {},      # optional: {"12": "positive_prompt"}  (node id -> input name)
    "comfy_overrides": {},         # optional: {"7": {"width": 832, "height": 480, "num_frames": 33}}
    "comfy_timeout": 1800,
    "voicebox_urls": ["http://127.0.0.1:17493", "http://127.0.0.1:8000"],
    "voicebox_profile_id": "",
    "voicebox_language": "en",
    "voicebox_extra": {},          # extra JSON fields for POST /generate (engine, voice, ...)
    "voicebox_timeout": 300,
    "blender_host": "127.0.0.1",
    "blender_port": 9876,
    "blender_timeout": 120,
    "visual_order": ["comfyui", "blender", "card"],
    "width": 1280,
    "height": 720,
    "fps": 25,
    "default_seconds": 5,
    "output_dir": "",
}


def load_config(path: str | None = None, **overrides) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else ROOT / "media_config.json"
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as e:
            print(f"[media_hub] ignoring unreadable {p}: {e}")
    env = os.environ
    if env.get("BUDDY_COMFY_URL"):
        cfg["comfy_url"] = env["BUDDY_COMFY_URL"]
    if env.get("BUDDY_COMFY_WORKFLOW"):
        cfg["comfy_workflow"] = env["BUDDY_COMFY_WORKFLOW"]
    if env.get("BUDDY_VOICEBOX_URL"):
        cfg["voicebox_urls"] = [env["BUDDY_VOICEBOX_URL"]]
    if env.get("BUDDY_VOICEBOX_PROFILE"):
        cfg["voicebox_profile_id"] = env["BUDDY_VOICEBOX_PROFILE"]
    if env.get("BUDDY_BLENDER_PORT"):
        cfg["blender_port"] = int(env["BUDDY_BLENDER_PORT"])
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    if not cfg["output_dir"]:
        cfg["output_dir"] = str(ROOT / "media_output")  # anchored to the project root, never the cwd
    return cfg


# --------------------------------------------------------------------------- helpers
class BackendError(RuntimeError):
    pass


class BackendUnavailable(BackendError):
    pass


def _http(method: str, url: str, data=None, timeout: float = 30):
    body = None
    headers = {}
    if isinstance(data, (dict, list)):
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif isinstance(data, bytes):
        body = data
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        raise BackendUnavailable(f"cannot reach {url}: {e}") from e


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(cmd: list[str], timeout: int = 900) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise BackendError(f"{cmd[0]} not found - install ffmpeg and put it on PATH") from e
    if p.returncode != 0:
        raise BackendError(f"{cmd[0]} failed: {(p.stderr or '')[-600:]}")
    return p.stdout


def _duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path)], timeout=60).strip()
    return float(out) if out else 0.0


_VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".gif", ".avi"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


# --------------------------------------------------------------------------- ComfyUI
class ComfyUIClient:
    """Talks to ComfyUI's HTTP API (POST /prompt, GET /history, GET /view)."""

    def __init__(self, cfg: dict):
        self.base = cfg["comfy_url"].rstrip("/")
        self.workflow_path = cfg["comfy_workflow"]
        self.prompt_nodes = cfg.get("comfy_prompt_nodes") or {}
        self.overrides = cfg.get("comfy_overrides") or {}
        self.timeout = cfg.get("comfy_timeout", 1800)
        self.client_id = uuid.uuid4().hex

    def health(self) -> dict:
        try:
            status, _, raw = _http("GET", f"{self.base}/system_stats", timeout=5)
        except BackendUnavailable as e:
            return {"ok": False, "detail": str(e)}
        if status != 200:
            return {"ok": False, "detail": f"HTTP {status}"}
        detail = f"{self.base} up"
        if not self.workflow_path:
            return {"ok": False, "detail": detail + ", but no workflow set (comfy_workflow / BUDDY_COMFY_WORKFLOW)"}
        if not Path(self.workflow_path).exists():
            return {"ok": False, "detail": detail + f", workflow file not found: {self.workflow_path}"}
        return {"ok": True, "detail": detail + f", workflow {Path(self.workflow_path).name}"}

    def load_workflow(self, path: str | None = None) -> dict:
        if not (path or self.workflow_path):
            raise BackendError("no ComfyUI workflow configured: set comfy_workflow in media_config.json "
                               "(a workflow exported with 'Save (API Format)')")
        p = Path(path or self.workflow_path)
        if not p.is_file():
            raise BackendError(f"workflow file not found: {p}")
        wf = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(wf, dict) and "nodes" in wf and "links" in wf:
            raise BackendError(
                "this workflow is in UI format. In ComfyUI open Settings, enable 'Dev mode', then use "
                "Workflow > Export (API) / 'Save (API Format)' and point comfy_workflow at that file.")
        if not isinstance(wf, dict) or not all(isinstance(v, dict) and "class_type" in v for v in wf.values()):
            raise BackendError("workflow is not in ComfyUI API format (every entry needs a class_type)")
        return wf

    def apply(self, wf: dict, prompt: str, seed: int | None = None) -> tuple[dict, list[str]]:
        """Return a copy of wf with the prompt text (and seeds) filled in, plus a log of what changed."""
        wf = copy.deepcopy(wf)
        applied: list[str] = []
        if self.prompt_nodes:
            for nid, key in self.prompt_nodes.items():
                if nid in wf:
                    wf[nid].setdefault("inputs", {})[key] = prompt
                    applied.append(f"prompt -> node {nid}.{key}")
        else:
            for nid, node in wf.items():
                ct = node["class_type"]
                title = ((node.get("_meta") or {}).get("title") or "").lower()
                if "TextEncode" not in ct or "neg" in title:
                    continue
                inputs = node.get("inputs", {})
                for key in ("positive_prompt", "text", "prompt"):
                    if isinstance(inputs.get(key), str):  # a string, not a link to another node
                        inputs[key] = prompt
                        applied.append(f"prompt -> node {nid} ({ct}).{key}")
                        break
                if applied:
                    break  # first positive text node only
        if not applied:
            raise BackendError("could not find a positive text-prompt node; set comfy_prompt_nodes, "
                               'e.g. {"12": "positive_prompt"}')
        seed = random.randint(0, 2**31 - 1) if seed is None else seed
        for nid, node in wf.items():
            inputs = node.get("inputs", {})
            for key in ("seed", "noise_seed"):
                if isinstance(inputs.get(key), int) and not isinstance(inputs.get(key), bool):
                    inputs[key] = seed
                    applied.append(f"seed -> node {nid}.{key}")
        for nid, vals in self.overrides.items():
            if nid in wf:
                wf[nid].setdefault("inputs", {}).update(vals)
                applied.append(f"overrides -> node {nid}: {sorted(vals)}")
        return wf, applied

    def queue(self, wf: dict) -> str:
        status, _, raw = _http("POST", f"{self.base}/prompt", {"prompt": wf, "client_id": self.client_id}, timeout=60)
        data = _json(raw) or {}
        if status != 200:
            msg = (data.get("error") or {}).get("message") or raw[:300].decode("utf-8", "replace")
            nodes = data.get("node_errors") or {}
            detail = "; ".join(f"node {n}: " + ", ".join(e.get("message", "?") for e in v.get("errors", []))
                               for n, v in nodes.items())
            raise BackendError(f"ComfyUI rejected the workflow: {msg} {detail}".strip())
        return data["prompt_id"]

    def wait(self, prompt_id: str, poll: float = 2.0) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            status, _, raw = _http("GET", f"{self.base}/history/{prompt_id}", timeout=30)
            hist = (_json(raw) or {}).get(prompt_id)
            if hist:
                st = hist.get("status") or {}
                if st.get("status_str") == "error":
                    msgs = [m for m in st.get("messages", []) if m and m[0] == "execution_error"]
                    raise BackendError(f"ComfyUI execution error: {msgs[-1][1] if msgs else st}")
                if st.get("completed") or hist.get("outputs"):
                    return hist
            time.sleep(poll)
        raise BackendError(f"ComfyUI did not finish within {self.timeout}s")

    def download(self, hist: dict, dest: Path, stem: str) -> dict:
        dest.mkdir(parents=True, exist_ok=True)
        videos, images = [], []
        for node_out in (hist.get("outputs") or {}).values():
            for kind in ("videos", "gifs", "images"):
                for item in node_out.get(kind, []):
                    if item.get("type") == "temp" and kind == "images":
                        continue  # previews
                    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                                "type": item.get("type", "output")})
                    status, _, raw = _http("GET", f"{self.base}/view?{q}", timeout=120)
                    if status != 200:
                        continue
                    ext = Path(item["filename"]).suffix.lower()
                    out = dest / f"{stem}_{len(videos) + len(images):02d}{ext}"
                    out.write_bytes(raw)
                    (videos if ext in _VIDEO_EXT else images if ext in _IMAGE_EXT else []).append(out)
        return {"videos": videos, "images": images}

    def generate(self, prompt: str, dest: Path, stem: str, seed: int | None = None) -> dict:
        wf, applied = self.apply(self.load_workflow(), prompt, seed)
        pid = self.queue(wf)
        out = self.download(self.wait(pid), dest, stem)
        out["applied"] = applied
        if not out["videos"] and not out["images"]:
            raise BackendError("ComfyUI finished but returned no video/image outputs "
                               "(does the workflow end in a Save/Video Combine node?)")
        return out


# --------------------------------------------------------------------------- Voicebox
class VoiceboxClient:
    """Voicebox REST API (docs at <base>/docs). Routes are discovered, not assumed: run `probe`."""

    AUDIO_ROUTES = ("/audio/{id}", "/generations/{id}/audio", "/generate/{id}/audio", "/history/{id}/audio",
                    "/generations/{id}/download", "/generations/{id}/file")

    def __init__(self, cfg: dict):
        self.urls = [u.rstrip("/") for u in cfg["voicebox_urls"]]
        self.profile_id = cfg.get("voicebox_profile_id") or ""
        self.language = cfg.get("voicebox_language", "en")
        self.extra = cfg.get("voicebox_extra") or {}
        self.timeout = cfg.get("voicebox_timeout", 300)
        self.base: str | None = None

    def _find_base(self) -> str:
        if self.base:
            return self.base
        last = "no URL configured"
        for u in self.urls:
            try:
                status, _, raw = _http("GET", f"{u}/openapi.json", timeout=4)
            except BackendUnavailable as e:
                last = str(e)
                continue
            if status == 200 and "/generate" in raw.decode("utf-8", "replace"):
                self.base = u
                return u
            last = f"{u} answered but does not look like Voicebox (HTTP {status})"
        raise BackendUnavailable(last + ". Start the Voicebox app/backend first.")

    def health(self) -> dict:
        try:
            base = self._find_base()
            return {"ok": True, "detail": f"{base} up"}
        except BackendUnavailable as e:
            return {"ok": False, "detail": str(e)}

    def probe(self) -> list[str]:
        status, _, raw = _http("GET", f"{self._find_base()}/openapi.json", timeout=10)
        paths = (_json(raw) or {}).get("paths", {})
        return sorted(f"{m.upper():6} {p}" for p, ops in paths.items() for m in ops)

    def profiles(self) -> list[dict]:
        status, _, raw = _http("GET", f"{self._find_base()}/profiles", timeout=15)
        data = _json(raw)
        if isinstance(data, dict):
            data = data.get("profiles") or data.get("items") or []
        return data if isinstance(data, list) else []

    @staticmethod
    def _is_audio(headers: dict, body: bytes) -> bool:
        return (headers.get("content-type", "").startswith("audio/") or body[:4] in (b"RIFF", b"OggS", b"fLaC")
                or body[:3] == b"ID3" or body[4:8] == b"ftyp")

    def speak(self, text: str, out_path: Path, profile_id: str | None = None,
              language: str | None = None, extra: dict | None = None) -> Path:
        base = self._find_base()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pid = profile_id or self.profile_id
        if not pid:
            profs = self.profiles()
            if not profs:
                raise BackendError("Voicebox has no voice profiles - create one in the Voicebox app "
                                   "(or set voicebox_profile_id).")
            pid = str(profs[0].get("id") or profs[0].get("profile_id"))
        payload = {"text": text, "profile_id": pid, "language": language or self.language, **self.extra, **(extra or {})}
        status, headers, body = _http("POST", f"{base}/generate", payload, timeout=self.timeout)
        if status >= 400:
            raise BackendError(f"Voicebox /generate -> HTTP {status}: {body[:300].decode('utf-8', 'replace')}")
        if self._is_audio(headers, body):
            out_path.write_bytes(body)
            return out_path
        data = _json(body) or {}
        for key in ("audio_path", "file_path", "path", "output_path"):  # local app: path on this machine
            if isinstance(data.get(key), str) and Path(data[key]).exists():
                shutil.copyfile(data[key], out_path)
                return out_path
        gid = data.get("id") or data.get("generation_id")
        urls = [data[k] for k in ("audio_url", "url") if isinstance(data.get(k), str)]
        if gid:
            urls += [r.replace("{id}", str(gid)) for r in self.AUDIO_ROUTES]
        if not urls:
            raise BackendError(f"unexpected /generate response: {str(data)[:300]} - run `probe` to see the routes")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            for u in urls:
                full = u if u.startswith("http") else base + u
                try:
                    st, hd, bd = _http("GET", full, timeout=60)
                except BackendUnavailable:
                    continue
                if st == 200 and self._is_audio(hd, bd):
                    out_path.write_bytes(bd)
                    return out_path
            status_txt = str(data.get("status", "")).lower()
            if status_txt in ("failed", "error"):
                raise BackendError(f"Voicebox generation failed: {data}")
            time.sleep(1.5)
        raise BackendError("Voicebox accepted the text but no audio route returned a file; "
                           "run `python media_hub.py probe` and set voicebox_extra / routes accordingly.")


# --------------------------------------------------------------------------- Blender-MCP
class BlenderMCPClient:
    """Raw JSON over TCP (default 127.0.0.1:9876); the BlenderMCP add-on must be started in Blender."""

    PALETTE = [(0.9, 0.35, 0.2), (0.2, 0.55, 0.9), (0.25, 0.75, 0.4), (0.85, 0.7, 0.15), (0.6, 0.3, 0.8)]
    SHAPES = ["primitive_cube_add", "primitive_uv_sphere_add", "primitive_cone_add",
              "primitive_torus_add", "primitive_cylinder_add"]

    def __init__(self, cfg: dict):
        self.host = cfg["blender_host"]
        self.port = int(cfg["blender_port"])
        self.timeout = cfg.get("blender_timeout", 120)
        self.w, self.h = cfg["width"], cfg["height"]

    def command(self, type_: str, params: dict | None = None, timeout: float | None = None):
        try:
            s = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as e:
            raise BackendUnavailable(
                f"Blender is not listening on {self.host}:{self.port} - open Blender, press N, "
                f"BlenderMCP tab, Connect ({e})") from e
        with s:
            s.settimeout(timeout or self.timeout)
            s.sendall(json.dumps({"type": type_, "params": params or {}}).encode("utf-8"))
            buf = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout as e:
                    raise BackendError(f"Blender did not answer '{type_}' in time (long render? raise blender_timeout)") from e
                if not chunk:
                    raise BackendError("Blender closed the connection before replying")
                buf += chunk
                try:
                    resp = json.loads(buf.decode("utf-8"))
                    break
                except ValueError:
                    continue
        if resp.get("status") == "error":
            raise BackendError(f"Blender error: {resp.get('message')}")
        return resp.get("result")

    def health(self) -> dict:
        try:
            self.command("get_scene_info", timeout=10)
            return {"ok": True, "detail": f"{self.host}:{self.port} up"}
        except BackendError as e:
            return {"ok": False, "detail": str(e)}

    def execute(self, code: str, timeout: float | None = None):
        return self.command("execute_code", {"code": code}, timeout)

    def scene_code(self, caption: str, index: int, out_png: Path) -> str:
        r, g, b = self.PALETTE[index % len(self.PALETTE)]
        shape = self.SHAPES[index % len(self.SHAPES)]
        return f"""
import bpy, math
bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()
sc = bpy.context.scene
for eng in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE', 'BLENDER_WORKBENCH'):
    try:
        sc.render.engine = eng; break
    except TypeError:
        pass
sc.render.resolution_x, sc.render.resolution_y = {self.w}, {self.h}
sc.render.filepath = {out_png.as_posix()!r}
sc.world = sc.world or bpy.data.worlds.new('W'); sc.world.use_nodes = True
sc.world.node_tree.nodes['Background'].inputs[0].default_value = (0.03, 0.03, 0.05, 1)
bpy.ops.mesh.{shape}(location=(0, 0, 0))
obj = bpy.context.active_object
obj.rotation_euler = ({0.3 * (index + 1)}, 0.4, {0.5 * (index + 1)})
mat = bpy.data.materials.new('M'); mat.diffuse_color = ({r}, {g}, {b}, 1); obj.data.materials.append(mat)
bpy.ops.object.light_add(type='SUN', location=(4, -4, 6)); bpy.context.active_object.data.energy = 4
bpy.ops.object.text_add(location=(0, 0, -1.9), rotation=(math.radians(90), 0, 0))
t = bpy.context.active_object; t.data.body = {caption[:120]!r}; t.data.align_x = 'CENTER'; t.data.size = 0.28
bpy.ops.object.camera_add(location=(0, -7, 0.4), rotation=(math.radians(90), 0, 0))
sc.camera = bpy.context.active_object
bpy.ops.render.render(write_still=True)
"""

    def render_still(self, caption: str, index: int, out_png: Path) -> Path:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        self.execute(self.scene_code(caption, index, out_png))
        if not out_png.exists() or out_png.stat().st_size < 100:
            raise BackendError(f"Blender reported success but {out_png} was not written")
        return out_png


# --------------------------------------------------------------------------- the hub
class MediaHub:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.comfy = ComfyUIClient(self.cfg)
        self.voicebox = VoiceboxClient(self.cfg)
        self.blender = BlenderMCPClient(self.cfg)
        self.custom_visuals: dict = {}

    def register_visual(self, name: str, fn):
        """fn(prompt, text, index, seconds, out_path) must write a video to out_path.
        Add `name` to cfg["visual_order"] to use it."""
        self.custom_visuals[name] = fn

    def health(self) -> dict:
        return {
            "comfyui": self.comfy.health(),
            "voicebox": self.voicebox.health(),
            "blender_mcp": self.blender.health(),
            "ffmpeg": {"ok": _have_ffmpeg(), "detail": shutil.which("ffmpeg") or "not on PATH"},
        }

    # -- ffmpeg building blocks
    def _vf(self) -> str:
        w, h, fps = self.cfg["width"], self.cfg["height"], self.cfg["fps"]
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p")

    def _still_to_video(self, png: Path, seconds: float, out: Path):
        w, h, fps = self.cfg["width"], self.cfg["height"], self.cfg["fps"]
        frames = max(1, int(seconds * fps))
        _run(["ffmpeg", "-y", "-loop", "1", "-i", str(png), "-t", f"{seconds:.2f}", "-vf",
              f"scale={w * 2}:-2,zoompan=z='min(zoom+0.0006,1.15)':d={frames}:s={w}x{h}:fps={fps},format=yuv420p",
              "-c:v", "libx264", "-an", str(out)])

    def _card(self, seconds: float, index: int, out: Path):
        w, h, fps = self.cfg["width"], self.cfg["height"], self.cfg["fps"]
        r, g, b = BlenderMCPClient.PALETTE[index % 5]
        col = f"0x{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={col}:s={w}x{h}:r={fps}:d={seconds:.2f}",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)])

    def _images_to_video(self, images: list[Path], seconds: float, out: Path):
        lst = out.with_suffix(".txt")
        per = max(0.04, seconds / max(1, len(images)))
        lst.write_text("".join(f"file '{p.as_posix()}'\nduration {per:.3f}\n" for p in images)
                       + f"file '{images[-1].as_posix()}'\n", encoding="utf-8")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-vf", self._vf(), "-c:v", "libx264", str(out)])

    # -- one scene
    def produce_scene(self, cut: dict, index: int, out_dir: Path, style: str = "") -> dict:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        text = str(cut.get("text", "")).strip()
        cut_secs = float(cut.get("out_seconds", 0)) - float(cut.get("in_seconds", 0))
        want = cut_secs if cut_secs > 0 else float(self.cfg["default_seconds"])
        prompt = cut.get("prompt") or (f"{style}, {text}" if style else text) or f"scene {index + 1}"
        stem = f"scene_{index + 1:02d}"

        # 1) narration first, so its length can drive the scene length
        audio, audio_len = None, 0.0
        narration = str(cut.get("narration") or text).strip()
        if narration:
            try:
                audio = self.voicebox.speak(narration, out_dir / f"{stem}_voice.wav")
                audio_len = _duration(audio)
            except Exception as e:
                warnings.append(f"narration skipped: {type(e).__name__}: {e}")
        length = max(want, audio_len + 0.4) if audio else want

        # 2) visuals, first backend that works
        visual, used = None, None
        for backend in self.cfg["visual_order"]:
            try:
                if backend == "comfyui":
                    res = self.comfy.generate(prompt, out_dir, f"{stem}_comfy")
                    visual = res["videos"][0] if res["videos"] else None
                    if visual is None:
                        visual = out_dir / f"{stem}_comfy.mp4"
                        self._images_to_video(res["images"], length, visual)
                elif backend == "blender":
                    png = self.blender.render_still(text or prompt, index, out_dir / f"{stem}_blender.png")
                    visual = out_dir / f"{stem}_blender.mp4"
                    self._still_to_video(png, length, visual)
                elif backend in self.custom_visuals:
                    visual = out_dir / f"{stem}_{backend}.mp4"
                    try:
                        self.custom_visuals[backend](prompt, text, index, length, visual)
                    except BackendError:
                        raise
                    except Exception as e:
                        raise BackendError(f"{backend} failed: {e}") from e
                    if not visual.exists() or visual.stat().st_size == 0:
                        raise BackendError(f"{backend} reported success but wrote no video")
                elif backend == "card":
                    visual = out_dir / f"{stem}_card.mp4"
                    self._card(length, index, visual)
                else:
                    raise BackendError(f"unknown visual backend '{backend}'")
                used = backend
                break
            except Exception as e:
                warnings.append(f"{backend} unavailable: {type(e).__name__}: {e}")
        if visual is None:
            raise BackendError(f"{stem}: no visual backend produced output ({'; '.join(warnings)})")

        # 3) mux: loop/trim visuals to `length`, narration or silence, uniform encode for concat
        out = out_dir / f"{stem}.mp4"
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(visual)]
        cmd += ["-i", str(audio)] if audio else ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += ["-t", f"{length:.2f}", "-map", "0:v:0", "-map", "1:a:0", "-vf", self._vf(),
                "-c:v", "libx264", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)]
        _run(cmd)
        return {"index": index + 1, "video": str(out), "visual_backend": used, "narrated": bool(audio),
                "seconds": round(length, 2), "prompt": prompt, "warnings": warnings}

    # -- whole piece
    def produce_video(self, cuts: list[dict], out_dir: Path | None = None, style: str = "",
                      only: int | None = None, title: str = "video") -> dict:
        if not _have_ffmpeg():
            raise BackendError("ffmpeg/ffprobe not found on PATH")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out_dir = Path(out_dir) if out_dir else Path(self.cfg["output_dir"]) / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', title)[:40]}_{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        scenes = []
        for i, cut in enumerate(cuts):
            if only is not None and i + 1 != only:
                continue
            print(f"[media_hub] scene {i + 1}/{len(cuts)} ...", flush=True)
            scenes.append(self.produce_scene(cut, i, out_dir, style))
        lst = out_dir / "concat.txt"
        lst.write_text("".join(f"file '{Path(s['video']).as_posix()}'\n" for s in scenes), encoding="utf-8")
        final = out_dir / "final.mp4"
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(final)])
        report = {"final": str(final), "seconds": round(_duration(final), 2), "scenes": scenes}
        (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report


def load_cuts(path: str) -> tuple[list[dict], str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data, "", Path(path).stem
    cuts = data.get("cuts") or data.get("scenes") or []
    return cuts, str(data.get("theme", "")), str(data.get("title") or Path(path).stem)


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Buddy media hub: ComfyUI + Voicebox + Blender-MCP in one place")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    sub.add_parser("probe")
    p = sub.add_parser("produce"); p.add_argument("cuts"); p.add_argument("--only", type=int); p.add_argument("--out")
    p = sub.add_parser("speak"); p.add_argument("text"); p.add_argument("--out", default="speak_test.wav")
    p = sub.add_parser("comfy"); p.add_argument("prompt"); p.add_argument("--out", default="comfy_test")
    sub.add_parser("blender")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    hub = MediaHub(load_config(args.config))
    try:
        if args.cmd == "health":
            h = hub.health()
            for name, r in h.items():
                print(f"  {'OK  ' if r['ok'] else 'DOWN'} {name:12} {r['detail']}")
            return 0 if all(r["ok"] for r in h.values()) else 1
        if args.cmd == "probe":
            print("\n".join(hub.voicebox.probe()))
        elif args.cmd == "produce":
            cuts, style, title = load_cuts(args.cuts)
            rep = hub.produce_video(cuts, Path(args.out) if args.out else None, style, args.only, title)
            for s in rep["scenes"]:
                print(f"  scene {s['index']}: visuals={s['visual_backend']:8} narrated={s['narrated']} {s['seconds']}s")
                for w in s["warnings"]:
                    print(f"      ! {w}")
            print(f"\nFINAL: {rep['final']} ({rep['seconds']}s)")
        elif args.cmd == "speak":
            print(hub.voicebox.speak(args.text, Path(args.out)))
        elif args.cmd == "comfy":
            print(hub.comfy.generate(args.prompt, Path(args.out), "test"))
        elif args.cmd == "blender":
            print(hub.blender.render_still("Buddy test frame", 0, Path(hub.cfg["output_dir"]) / "blender_test.png"))
    except BackendError as e:
        print(f"ERROR: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
