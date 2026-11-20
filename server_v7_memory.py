"""
Buddy OS v7.0 - Autonomous Multi-Modal Agent
Single-file server: Mission Control, Guarded Sandbox, Knowledge Mesh,
Skill Registry, Multi-Modal Workers, Async Swarm, Memory Logger.
"""
import os
import sys
import stat
import shutil
import asyncio
import ast
import re
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn
from openai import OpenAI

from core.sandbox_guard import GuardedSandbox
from core.knowledge_mesh import KnowledgeMesh
from core.skills_registry import SkillRegistry
from core.lessons_store import LessonsStore
from core.workers import (
    CodeWorker, WebBuilderWorker, GameBuilderWorker,
    PresentationWorker, ImageWorker, VideoWorker, ResearchWorker
)
from core.mission_control import MissionControl
from buddy_memory import BuddyMemory

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL = "qwen3-coder:30b"

sandbox = GuardedSandbox(root="workspace")
knowledge = KnowledgeMesh()
skills = SkillRegistry(skills_dir="workspace/skills")
lessons = LessonsStore()
memory = BuddyMemory()

workers = {
    "code": CodeWorker(client, MODEL),
    "web": WebBuilderWorker(client, MODEL),
    "game": GameBuilderWorker(client, MODEL),
    "presentation": PresentationWorker(client, MODEL),
    "image": ImageWorker(client, MODEL),
    "video": VideoWorker(client, MODEL),
    "research": ResearchWorker(client, MODEL, knowledge),
}

mission_control = MissionControl(client, MODEL, workers, skills, knowledge, sandbox, lessons)

app = FastAPI()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Buddy OS v7.0 - Autonomous Agent</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/lucide@latest"></script>
<style>
  .scrollbar-thin::-webkit-scrollbar { width: 6px; }
  .scrollbar-thin::-webkit-scrollbar-track { background: #0f172a; }
  .scrollbar-thin::-webkit-scrollbar-thumb { background: #312e81; border-radius: 3px; }
  .mission-card { animation: slideIn 0.3s ease-out; white-space: pre-wrap; word-break: break-word; }
  @keyframes slideIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
</style>
</head>
<body class="bg-gradient-to-br from-gray-950 via-slate-900 to-indigo-950 text-indigo-50 min-h-screen font-sans flex flex-col">

<header class="border-b border-indigo-800/40 bg-indigo-950/60 backdrop-blur-xl sticky top-0 z-50 px-6 py-4 flex items-center justify-between shadow-2xl">
  <div class="flex items-center space-x-3">
    <div class="bg-indigo-600/20 border border-indigo-400/40 p-2.5 rounded-2xl text-indigo-300 shadow-inner">
      <i data-lucide="cpu" class="w-6 h-6 animate-pulse"></i>
    </div>
    <div>
      <h1 class="font-extrabold text-lg tracking-wider text-indigo-100 flex items-center space-x-2">
        <span>Buddy OS</span>
        <span class="text-indigo-400 text-xs px-2.5 py-0.5 rounded-full bg-indigo-500/10 border border-indigo-400/30">v7.0 Autonomous</span>
      </h1>
      <p class="text-xs text-indigo-300/60 font-mono">Self-Healing - Auto-Skill - Multi-Modal - Internet-Enabled</p>
    </div>
  </div>

  <div class="flex items-center space-x-4">
    <div id="connection-badge" class="flex items-center space-x-2 bg-indigo-900/40 border border-indigo-700/50 px-3.5 py-1.5 rounded-full text-xs text-indigo-200 backdrop-blur-md">
      <span id="connection-dot" class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping"></span>
      <span id="connection-text" class="font-medium">Core Link Secure</span>
    </div>
    <button onclick="setMode('mission')" id="btn-mission" class="bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg">Mission Mode</button>
    <button onclick="setMode('chat')" id="btn-chat" class="bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg">Chat Mode</button>
  </div>
</header>

<main class="flex-1 p-6 max-w-7xl mx-auto w-full grid grid-cols-1 lg:grid-cols-4 gap-6">
  <div class="space-y-6 lg:col-span-1">
    <div class="bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-5 rounded-3xl shadow-2xl">
      <h2 class="text-xs font-bold tracking-widest uppercase text-indigo-300/80 mb-4 flex items-center space-x-2">
        <i data-lucide="activity" class="w-4 h-4 text-indigo-400"></i>
        <span>Telemetry Matrix</span>
      </h2>
      <div class="space-y-3 font-mono text-xs">
        <div class="bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center">
          <span class="text-indigo-300/70">Model Node</span>
          <span class="font-bold text-indigo-300">30B Coder</span>
        </div>
        <div class="bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center">
          <span class="text-indigo-300/70">AST Firewall</span>
          <span class="font-bold text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded-lg border border-amber-500/20">Guarded</span>
        </div>
        <div class="bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center">
          <span class="text-indigo-300/70">Active Pipeline</span>
          <span class="font-bold text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded-lg border border-indigo-500/20">Async Swarm</span>
        </div>
        <div class="bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center">
          <span class="text-indigo-300/70">Skills Loaded</span>
          <span id="skill-count" class="font-bold text-emerald-400">0</span>
        </div>
        <div class="bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center">
          <span class="text-indigo-300/70">Missions Active</span>
          <span id="mission-count" class="font-bold text-emerald-400">0</span>
        </div>
        <div class="bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center">
          <span class="text-indigo-300/70">Memories</span>
          <span id="memory-count" class="font-bold text-cyan-400">0</span>
        </div>
        <div class="bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center">
          <span class="text-indigo-300/70">Success Rate</span>
          <span id="success-rate" class="font-bold text-cyan-400">--%</span>
        </div>
      </div>
    </div>

    <div class="bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-5 rounded-3xl shadow-2xl">
      <h2 class="text-xs font-bold tracking-widest uppercase text-indigo-300/80 mb-4 flex items-center space-x-2">
        <i data-lucide="zap" class="w-4 h-4 text-indigo-400"></i>
        <span>Quick Actions</span>
      </h2>
      <div class="space-y-2">
        <button onclick="quickAction('Clone https://github.com/tiangolo/fastapi.git and integrate as skill')" class="w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all">Clone & Integrate FastAPI</button>
        <button onclick="quickAction('Build a React task manager web app with Node backend')" class="w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all">Build Full-Stack Web App</button>
        <button onclick="quickAction('Generate a Three.js space shooter game')" class="w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all">Build HTML5 Game</button>
        <button onclick="quickAction('Research latest AI coding trends and build a presentation')" class="w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all">Research + Presentation</button>
        <button onclick="quickAction('Generate a logo for an AI startup called NeuroForge')" class="w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all">Generate Image</button>
      </div>
    </div>
  </div>

  <div class="lg:col-span-3 space-y-6 flex flex-col">
    <div class="bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-5 rounded-3xl shadow-2xl flex-1 flex flex-col min-h-[400px]">
      <div class="flex items-center justify-between mb-4 border-b border-indigo-800/30 pb-3">
        <h2 class="text-sm font-semibold text-indigo-200 flex items-center space-x-2">
          <i data-lucide="terminal" class="w-4 h-4 text-indigo-400"></i>
          <span id="stream-title">Autonomous Execution Stream</span>
        </h2>
        <div class="flex items-center space-x-2">
          <span id="mode-badge" class="text-[10px] uppercase tracking-wider bg-indigo-500/20 text-indigo-300 px-2 py-1 rounded-lg border border-indigo-500/30">Mission Mode</span>
          <button onclick="document.getElementById('log-container').innerHTML=''" class="text-[10px] uppercase tracking-wider text-indigo-400/60 hover:text-indigo-200 transition-colors">Clear</button>
        </div>
      </div>
      <div id="log-container" class="bg-gray-950/90 border border-indigo-900/60 rounded-2xl p-4 font-mono text-xs text-indigo-300/90 space-y-2.5 flex-1 overflow-y-auto shadow-inner max-h-[480px] scrollbar-thin">
        <div class="text-indigo-400/60">[System Matrix] Buddy OS v7.0 initialized. Guarded sandbox active. Async swarm ready. Knowledge mesh online. Memory logger connected.</div>
      </div>
    </div>

    <div class="bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-4 rounded-3xl shadow-2xl">
      <div class="flex items-center space-x-3">
        <input type="text" id="prompt-input" placeholder="Describe your mission: Build, clone, generate, research, analyze..."
          class="flex-1 bg-gray-950/60 border border-indigo-800/60 rounded-2xl px-4 py-3.5 text-sm text-indigo-100 placeholder-indigo-400/40 focus:outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/30 shadow-inner transition-all">
        <button id="send-btn" type="button" class="bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white px-6 py-3.5 rounded-2xl text-sm font-bold transition-all shadow-lg hover:shadow-indigo-500/25 flex items-center space-x-2 cursor-pointer active:scale-95">
          <span>Execute</span>
          <i data-lucide="send" class="w-4 h-4"></i>
        </button>
      </div>
    </div>
  </div>
</main>

<script>
  let currentMode = "mission";
  let socket = null;

  document.addEventListener("DOMContentLoaded", function() {
    lucide.createIcons();
    connectWebSocket();
    updateStats();
    setInterval(updateStats, 5000);
  });

  function connectWebSocket() {
    const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
    const wsUrl = protocol + window.location.host + "/ws";
    socket = new WebSocket(wsUrl);
    const dot = document.getElementById("connection-dot");
    const text = document.getElementById("connection-text");

    socket.onopen = function() {
      if (dot) dot.className = "w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse";
      if (text) text.innerText = "Core Link Secure";
    };

    socket.onmessage = function(event) {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "stats") {
          document.getElementById("skill-count").innerText = data.skills;
          document.getElementById("mission-count").innerText = data.missions;
          document.getElementById("memory-count").innerText = data.memories;
          document.getElementById("success-rate").innerText = data.success_rate + "%";
          return;
        }
        appendLog(data.message, data.color || "text-indigo-200");
      } catch {
        appendLog(event.data, "text-indigo-200");
      }
    };

    socket.onclose = function() {
      if (dot) dot.className = "w-2.5 h-2.5 rounded-full bg-rose-500 animate-pulse";
      if (text) text.innerText = "Link Lost - Reconnecting...";
      setTimeout(connectWebSocket, 3000);
    };
  }

  function appendLog(message, colorClass) {
    colorClass = colorClass || "text-indigo-200";
    const container = document.getElementById("log-container");
    if (!container) return;
    const timeStr = new Date().toTimeString().split(" ")[0];
    const div = document.createElement("div");
    div.className = colorClass + " leading-relaxed font-mono mission-card";
    div.innerText = "[" + timeStr + "] " + message;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function setMode(mode) {
    currentMode = mode;
    document.getElementById("mode-badge").innerText = mode === "mission" ? "Mission Mode" : "Chat Mode";
    document.getElementById("stream-title").innerText = mode === "mission" ? "Autonomous Execution Stream" : "Chat Stream";
    document.getElementById("btn-mission").className = mode === "mission"
      ? "bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg"
      : "bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg";
    document.getElementById("btn-chat").className = mode === "chat"
      ? "bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg"
      : "bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg";
  }

  function quickAction(text) {
    document.getElementById("prompt-input").value = text;
    sendPrompt();
  }

  function sendPrompt() {
    const input = document.getElementById("prompt-input");
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    appendLog("USER [" + currentMode.toUpperCase() + "]: " + text, "text-indigo-400 font-bold");
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({mode: currentMode, prompt: text}));
    } else {
      appendLog("ERROR: WebSocket node connection dropped.", "text-rose-400");
    }
    input.value = "";
  }

  function updateStats() {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({type: "stats"}));
    }
  }

  document.getElementById("send-btn").addEventListener("click", sendPrompt);
  document.getElementById("prompt-input").addEventListener("keydown", function(e) {
    if (e.key === "Enter") sendPrompt();
  });
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    return HTML_TEMPLATE


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
                mode = payload.get("mode", "mission")
                user_input = payload.get("prompt", "")
                msg_type = payload.get("type", "")
            except Exception:
                mode = "mission"
                user_input = raw
                msg_type = ""

            if msg_type == "stats":
                stats = memory.get_stats()
                await send_json(websocket, {
                    "type": "stats",
                    "skills": len(skills.list_skills()),
                    "missions": len([m for m in mission_control.missions.values() if m.status == "running"]),
                    "memories": stats.get("total_interactions", 0),
                    "success_rate": stats.get("overall_success_rate", 0)
                })
                continue

            if not user_input:
                continue

            # Start memory tracking for this interaction
            task_id = memory.start_task(user_input, model_used=MODEL)

            if mode == "mission":
                await handle_mission_mode(user_input, websocket, task_id)
            else:
                await handle_chat_mode(user_input, websocket, task_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await send_json(websocket, {"message": f"[Critical Error] {str(e)}", "color": "text-rose-400"})


async def handle_mission_mode(user_input: str, websocket: WebSocket, task_id: str):
    lower = user_input.lower()

    # --- Memory slash commands ---
    if user_input.startswith("/memory") or user_input.startswith("/stats"):
        stats = memory.get_stats()
        lines = [
            f"[Memory Engine] Session: {stats['session_id']}",
            f"  Total interactions: {stats['total_interactions']}",
            f"  Session interactions: {stats['session_interactions']}",
            f"  Overall success rate: {stats['overall_success_rate']}%",
            f"  Interactions since last growth: {stats['interactions_since_growth']}",
            "  Task breakdown:"
        ]
        for ttype, count in stats["task_breakdown"].items():
            lines.append(f"    - {ttype}: {count}")
        await send_json(websocket, {"message": "\n".join(lines), "color": "text-cyan-300"})
        memory.complete_task("Memory stats displayed", success=True)
        return

    if user_input.startswith("/context"):
        ctx = memory.get_recent_context(limit=10)
        await send_json(websocket, {"message": f"[Memory Engine] Recent context:\n{ctx}", "color": "text-cyan-300"})
        memory.complete_task("Context displayed", success=True)
        return

    if user_input.startswith("/grow"):
        await send_json(websocket, {"message": "[Memory Engine] Triggering growth cycle...", "color": "text-amber-300"})
        ok = memory.trigger_growth(force=True)
        if ok:
            await send_json(websocket, {"message": "[Growth Engine] Evolution cycle complete. Buddy is now smarter.", "color": "text-emerald-400"})
        else:
            await send_json(websocket, {"message": "[Growth Engine] Cycle failed or threshold not met.", "color": "text-rose-400"})
        memory.complete_task("Growth triggered manually", success=ok)
        return

    if user_input.startswith("/success"):
        memory.mark_success(feedback=user_input[8:].strip())
        await send_json(websocket, {"message": "[Memory Engine] Last response marked as SUCCESS.", "color": "text-emerald-300"})
        memory.complete_task("Marked success", success=True)
        return

    if user_input.startswith("/fail"):
        memory.mark_failure(feedback=user_input[5:].strip())
        await send_json(websocket, {"message": "[Memory Engine] Last response marked as FAILURE.", "color": "text-rose-300"})
        memory.complete_task("Marked failure", success=False)
        return

    url_match = re.search(r"(https?://\S+\.git)", user_input)
    if not url_match:
        url_match = re.search(r"(https?://github\.com/[^\s/]+/[^\s/]+)", user_input)
    if url_match:
        repo_url = url_match.group(1).rstrip("/")
        if not repo_url.endswith(".git"):
            repo_url += ".git"

        await send_json(websocket, {
            "message": f"[Mission] Initiating clone & integration for {repo_url}",
            "color": "text-amber-300"
        })

        try:
            skill = skills.clone(repo_url)
            await send_json(websocket, {
                "message": f"[Mission] Successfully cloned and registered skill: {skill.name}",
                "color": "text-emerald-400"
            })

            mission = await mission_control.launch(
                f'Analyze architecture of skill "{skill.name}" and document its API.',
                websocket
            )
            memory.complete_task(f"Cloned and analyzed {skill.name}", success=True)
        except Exception as e:
            await send_json(websocket, {
                "message": f"[Mission Error] Failed to clone {repo_url}: {str(e)}",
                "color": "text-rose-400"
            })
            memory.complete_task(f"Clone failed: {str(e)}", success=False)
        return

    if user_input.startswith("/write ") or user_input.startswith("/create "):
        parts = user_input.split(" ", 2)
        if len(parts) >= 3:
            filepath, content = parts[1], parts[2]
            path = Path("workspace") / filepath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            await send_json(websocket, {"message": f"[File System] Written {len(content)} chars to {filepath}", "color": "text-emerald-300"})
            memory.complete_task(f"Wrote file {filepath}", success=True)
        else:
            await send_json(websocket, {"message": "[File System] Usage: /write path/to/file.py print(hello)", "color": "text-rose-400"})
            memory.complete_task("Write command failed: bad syntax", success=False)
        return

    if user_input.startswith("/read "):
        filepath = user_input.split(" ", 1)[1]
        path = Path("workspace") / filepath
        if path.exists():
            content = path.read_text(encoding="utf-8")[:3000]
            await send_json(websocket, {"message": f"[File System] {filepath}:\n{content}", "color": "text-indigo-200"})
            memory.complete_task(f"Read file {filepath}", success=True)
        else:
            await send_json(websocket, {"message": f"[File System] File not found: {filepath}", "color": "text-rose-400"})
            memory.complete_task(f"Read failed: {filepath} not found", success=False)
        return

    if user_input.startswith("/exec "):
        code = user_input.split(" ", 1)[1]
        await execute_code_payload(code, websocket)
        memory.complete_task("Executed code payload", success=True)
        return

    if user_input.startswith("/install "):
        package = user_input.split(" ", 1)[1]
        await send_json(websocket, {"message": f"[Package Manager] Installing {package}...", "color": "text-amber-300"})
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", package,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            await send_json(websocket, {"message": f"[Package Manager] {package} installed successfully", "color": "text-emerald-300"})
            memory.complete_task(f"Installed {package}", success=True)
        else:
            await send_json(websocket, {"message": f"[Package Manager] Failed: {stderr.decode()[:200]}", "color": "text-rose-400"})
            memory.complete_task(f"Install failed: {package}", success=False)
        return

    if user_input.startswith("/skills") or user_input.startswith("/list"):
        skill_names = skills.list_skills()
        if skill_names:
            lines = [f"[Skills] Loaded {len(skill_names)} skills:"]
            for name in skill_names[:20]:
                lines.append(f"  - {name}")
            await send_json(websocket, {"message": "\n".join(lines), "color": "text-emerald-300"})
        else:
            await send_json(websocket, {"message": "[Skills] No skills loaded yet.", "color": "text-amber-300"})

        health = skills.health_check()
        if health["missing"]:
            await send_json(websocket, {"message": f"[Skills] Missing on disk: {health['missing'][:3]}", "color": "text-rose-400"})
        memory.complete_task("Listed skills", success=True)
        return

    if user_input.startswith("/skill "):
        skill_query = user_input.split(" ", 1)[1]
        skill = skills.get(skill_query)
        if skill:
            await send_json(websocket, {"message": f"[Skill Engine] {skill.overview()}", "color": "text-emerald-300"})
            memory.complete_task(f"Viewed skill {skill.name}", success=True)
        else:
            matches = skills.search_all(skill_query, max_per_skill=5)
            if matches:
                lines = [f'[Skill Engine] No skill named "{skill_query}", but found matches:']
                for name, hits in matches.items():
                    lines.append(f"  {name}: {len(hits)} match(es)")
                await send_json(websocket, {"message": "\n".join(lines), "color": "text-amber-300"})
            else:
                available = ", ".join(skills.list_skills()[:10]) or "none"
                await send_json(websocket, {"message": f'[Skill Engine] No skill or match found for "{skill_query}". Available: {available}', "color": "text-rose-400"})
            memory.complete_task(f"Searched skill {skill_query}", success=True)
        return

    if user_input.startswith("/lessons"):
        stats = lessons.stats()
        lines = [f"[Lessons Engine] {stats['successes']} successes, {stats['failures']} recorded failures logged so far."]
        top = lessons.recall_all_types(limit=10)
        if top:
            lines.append("Top recurring pitfalls:")
            for l in top:
                entry = f"  [{l['task_type']}] {l['description'][:80]} -> {l['detail'][:100]}"
                if l["occurrences"] > 1:
                    entry += f" (seen {l['occurrences']}x)"
                lines.append(entry)
        await send_json(websocket, {"message": "\n".join(lines), "color": "text-emerald-300"})
        memory.complete_task("Viewed lessons", success=True)
        return

    if user_input.startswith("/test "):
        filepath = user_input.split(" ", 1)[1]
        path = Path("workspace") / filepath
        if path.exists():
            code = path.read_text(encoding="utf-8")
            tests = await workers["code"].generate_tests(code)
            out_path = Path("workspace/output/tests") / f"test_{path.name}"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(tests, encoding="utf-8")
            await send_json(websocket, {"message": f"[Test Engine] Tests written to {out_path}", "color": "text-emerald-300"})
            memory.complete_task(f"Generated tests for {filepath}", success=True)
        else:
            await send_json(websocket, {"message": f"[Test Engine] File not found: {filepath}", "color": "text-rose-400"})
            memory.complete_task(f"Test failed: {filepath} not found", success=False)
        return

    if user_input.startswith("/debug "):
        parts = user_input.split(" | Error: ")
        if len(parts) == 2:
            fixed = await workers["code"].debug(parts[0], parts[1])
            await send_json(websocket, {"message": f"[Debug Engine] {fixed}", "color": "text-emerald-300"})
            memory.complete_task("Debugged code", success=True)
        else:
            await send_json(websocket, {"message": "[Debug Engine] Usage: /debug code | Error: message", "color": "text-rose-400"})
            memory.complete_task("Debug failed: bad syntax", success=False)
        return

    if user_input.startswith("/image "):
        prompt = user_input.split(" ", 1)[1]
        out = "workspace/output/image_direct.svg"
        result = await workers["image"].generate(prompt, out)
        await send_json(websocket, {"message": f"[Image Engine] Saved: {result}", "color": "text-emerald-300"})
        memory.complete_task(f"Generated image: {prompt[:40]}", success=True)
        return

    if user_input.startswith("/web "):
        spec = user_input.split(" ", 1)[1]
        files = await workers["web"].build(spec)
        for fname, fcontent in files.items():
            path = Path(f"workspace/output/web/{fname}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fcontent, encoding="utf-8")
        await send_json(websocket, {"message": f"[Web Engine] Built {len(files)} files in workspace/output/web/", "color": "text-emerald-300"})
        memory.complete_task(f"Built web app: {spec[:40]}", success=True)
        return

    if user_input.startswith("/game "):
        spec = user_input.split(" ", 1)[1]
        files = await workers["game"].build(spec)
        for fname, fcontent in files.items():
            path = Path(f"workspace/output/game/{fname}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fcontent, encoding="utf-8")
        await send_json(websocket, {"message": f"[Game Engine] Built {len(files)} files in workspace/output/game/", "color": "text-emerald-300"})
        memory.complete_task(f"Built game: {spec[:40]}", success=True)
        return

    if user_input.startswith("/research "):
        query = user_input.split(" ", 1)[1]
        result = await workers["research"].research(query)
        await send_json(websocket, {"message": f"[Research Engine] {result[:2000]}", "color": "text-indigo-200"})
        memory.complete_task(f"Researched: {query[:40]}", success=True)
        return

    await send_json(websocket, {"message": f"[Mission Control] Launching: {user_input[:80]}...", "color": "text-amber-300"})
    try:
        await mission_control.launch(user_input, websocket)
        memory.complete_task(f"Mission completed: {user_input[:80]}", success=True)
    except Exception as e:
        await send_json(websocket, {"message": f"[Mission Error] {str(e)}", "color": "text-rose-400"})
        memory.complete_task(f"Mission failed: {str(e)}", success=False)


# Words that signal "go do a real task", not just "answer a question".
ACTION_KEYWORDS = (
    "build", "create", "write", "generate", "make", "fix", "debug",
    "clone", "integrate", "refactor", "implement", "add ", "update ",
    "research", "design", "deploy", "run "
)

def _looks_like_action(user_input: str) -> bool:
    lower = user_input.lower().strip()
    return lower.startswith(ACTION_KEYWORDS) or any(f" {kw}" in lower for kw in ACTION_KEYWORDS)


async def handle_chat_mode(user_input: str, websocket: WebSocket, task_id: str):
    lower_input = user_input.lower()

    # 0) SKILL SELF-AWARENESS — if user asks about skills, answer with REAL data
    skill_query_keywords = [
        "what skills", "what can you do", "your skills", "your capabilities",
        "list skills", "show skills", "available skills", "loaded skills",
        "how many skills", "do you have skills", "can you see skills",
        "what repos", "what repositories", "loaded repos", "skill registry"
    ]
    if any(kw in lower_input for kw in skill_query_keywords):
        skill_names = skills.list_skills()
        forged = [n for n in skill_names if n.endswith("_skill")]
        cloned = [n for n in skill_names if not n.endswith("_skill")]
        lines = [
            f"[Buddy Skill Matrix] I have {len(skill_names)} skills loaded:",
            f"  🔥 Auto-forged: {len(forged)}",
            f"  🔄 Cloned repos: {len(cloned)}",
            "",
        ]
        if forged:
            lines.append("Forged skills (from knowledge graph):")
            for name in forged[:15]:
                sk = skills.get(name)
                fc = sk.metadata.get("function_count", "?") if sk else "?"
                lines.append(f"  • {name} ({fc} funcs)")
            if len(forged) > 15:
                lines.append(f"  ... and {len(forged)-15} more")
        if cloned:
            lines.append("Cloned repositories:")
            for name in cloned[:10]:
                lines.append(f"  • {name}")
            if len(cloned) > 10:
                lines.append(f"  ... and {len(cloned)-10} more")
        lines.append("")
        lines.append("Ask me to use any skill by name, or type /skills for the full list.")
        await send_json(websocket, {"message": "\n".join(lines), "color": "text-emerald-300"})
        memory.complete_task("Listed skills for user", success=True)
        return

    # 0b) SKILL DETAIL — if user asks about a specific skill
    if lower_input.startswith("tell me about ") or lower_input.startswith("what is ") or "skill" in lower_input:
        mentioned = None
        for name in skills.list_skills():
            if name.lower() in lower_input:
                mentioned = name
                break
        if mentioned:
            sk = skills.get(mentioned)
            if sk:
                ov = sk.overview()
                await send_json(websocket, {"message": f"[Skill Engine] {ov}", "color": "text-cyan-300"})
                # Also show a sample of functions if forged
                if sk.metadata.get("forged"):
                    try:
                        content = sk.path.read_text(encoding="utf-8", errors="ignore")
                        funcs = [line.strip() for line in content.splitlines() if line.strip().startswith('"name":')][:5]
                        if funcs:
                            await send_json(websocket, {"message": "Sample functions:\n" + "\n".join(funcs), "color": "text-indigo-200"})
                    except Exception:
                        pass
                memory.complete_task(f"Described skill {mentioned}", success=True)
                return

    # 1) If this looks like a real task, hand it to Mission Control so it gets
    #    full skill grounding, tool use, and actually writes/executes things.
    if _looks_like_action(user_input):
        await send_json(websocket, {
            "message": "[Chat -> Mission] Detected an actionable request, routing through skill-grounded engine...",
            "color": "text-amber-300"
        })
        try:
            await mission_control.launch(user_input, websocket)
            memory.complete_task(f"Chat-routed mission: {user_input[:80]}", success=True)
        except Exception as e:
            await send_json(websocket, {"message": f"[Mission Error] {str(e)}", "color": "text-rose-400"})
            memory.complete_task(f"Chat-routed mission failed: {str(e)}", success=False)
        return

    # 2) Otherwise, it is conversational - but still ground the reply with any
    #    matching skill content so answers reflect what Buddy actually knows.
    skill_context = ""
    try:
        matches = skills.search_all(user_input, max_per_skill=3)
        if matches:
            snippets = []
            for name, hits in matches.items():
                for hit in hits[:2]:
                    snippets.append(f"[{name}] {hit}")
            if snippets:
                skill_context = "\n\nRelevant skill context (use this if applicable):\n" + "\n".join(snippets[:10])
    except Exception:
        pass

    # 3) Inject recent conversation history from memory for continuity
    recent_context = memory.get_recent_context(limit=6)

    # Build a compact skill list for the LLM's context
    skill_names = skills.list_skills()
    forged_names = [n.replace("_skill", "") for n in skill_names if n.endswith("_skill")]
    cloned_names = [n for n in skill_names if not n.endswith("_skill")]
    skill_summary = ""
    if forged_names or cloned_names:
        parts = ["\n\nLoaded skills you can reference:"]
        if forged_names:
            parts.append("Forged: " + ", ".join(forged_names[:10]))
        if cloned_names:
            parts.append("Cloned: " + ", ".join(cloned_names[:5]))
        skill_summary = "\n".join(parts)

    system_prompt = (
        "You are Buddy OS v7.0, a local developer assistant. "
        "You control a local workspace and have loaded skills you can draw on. "
        "You can generate code, files, web apps, games, images, and videos when the user "
        "actually asks for one of those things. "
        "For plain conversation (greetings, questions, small talk), just reply naturally "
        "in plain text - do not produce code blocks unless the user is asking you to build "
        "or write something."
        + skill_summary
        + f"\n\n{recent_context}"
        + skill_context
    )

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input}
                ],
                temperature=0.2,
                max_tokens=800
            )
        )
        reply = response.choices[0].message.content

        for chunk in [reply[i:i+500] for i in range(0, len(reply), 500)]:
            await send_json(websocket, {"message": chunk, "color": "text-indigo-200"})
            await asyncio.sleep(0.05)

        # Log completion and check for growth trigger
        memory.complete_task(reply, success=True)
        if memory.should_trigger_growth():
            await send_json(websocket, {
                "message": "[Memory Engine] Growth threshold reached. Triggering evolution cycle...",
                "color": "text-amber-300"
            })
            ok = memory.trigger_growth()
            if ok:
                await send_json(websocket, {
                    "message": "[Growth Engine] Evolution cycle complete. Buddy is now smarter.",
                    "color": "text-emerald-400"
                })

        code_blocks = re.findall(r"`{3}python\s*(.*?)\s*`{3}", reply, re.DOTALL)
        for code in code_blocks:
            await execute_code_payload(code, websocket)

    except Exception as e:
        await send_json(websocket, {"message": f"[Chat Error] {str(e)}", "color": "text-rose-400"})
        memory.complete_task(str(e), success=False)


async def execute_code_payload(code: str, websocket: WebSocket):
    mission_id = hashlib.md5(code.encode()).hexdigest()[:8]

    await send_json(websocket, {"message": "[Guarded Sandbox] Scanning payload...", "color": "text-amber-300"})

    scan = sandbox.scan(code, mission_id)

    if not scan["allowed"]:
        await send_json(websocket, {"message": f"[Guard BLOCKED] {scan['violations']}", "color": "text-rose-400"})
        return

    if scan.get("warnings"):
        for w in scan["warnings"]:
            await send_json(websocket, {"message": f"[Guard Warning] {w}", "color": "text-amber-400"})

    await send_json(websocket, {"message": "[Guarded Sandbox] Executing payload...", "color": "text-emerald-300"})

    try:
        # SANDBOX FIX: Removed subprocess, os, sys from local scope.
        # If code needs filesystem, it can use Path. If it needs network,
        # it fails here - which is the point of a sandbox.
        local_scope = {
            "json": json,
            "re": re,
            "Path": Path,
            "print": lambda *args, **kwargs: asyncio.create_task(
                send_json(websocket, {"message": " ".join(str(a) for a in args), "color": "text-indigo-200"})
            )
        }
        exec(code, local_scope)
        await send_json(websocket, {"message": "[Execution Engine] Payload resolved successfully.", "color": "text-emerald-300"})
    except Exception as e:
        await send_json(websocket, {"message": f"[Runtime Exception] {str(e)}", "color": "text-rose-400"})
        if scan.get("snapshot"):
            await send_json(websocket, {"message": f"[Rollback] Snapshot available for mission {scan['snapshot']}", "color": "text-amber-400"})


async def send_json(websocket: WebSocket, data: dict):
    try:
        await websocket.send_text(json.dumps(data))
    except Exception as e:
        print(f"[WebSocket Send Error] {e}")


if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
