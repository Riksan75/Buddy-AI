"""
Buddy OS v8.0 — Autonomous Multi-Modal Agent + Money Engine
Single-file server: Mission Control, Guarded Sandbox, Knowledge Mesh,
Skill Registry, Multi-Modal Workers, Async Swarm, Hybrid LLM Router,
Skill Forge (self-authoring skills), Money Engine (daily earning plans).
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
import uvicorn

# ----- Custom Workers -----
from core.shorts_worker import ShortsWorker
from core.sandbox_guard import GuardedSandbox
from core.knowledge_mesh import KnowledgeMesh
from core.skills_registry import SkillRegistry
from core.lessons_store import LessonsStore
from core.soul import Soul
from core.workers import (
    CodeWorker, WebBuilderWorker, GameBuilderWorker,
    PresentationWorker, ImageWorker, VideoWorker, ResearchWorker
)
from core.mission_control import MissionControl
from core.soul import Soul

# ----- v8: Hybrid brain + Money Engine + Skill Forge -----
from core.hybrid_router import HybridRouter, PROVIDERS
from core.money_engine import MoneyEngine
from core.skill_forge import SkillForge
from core.earnings_ledger import EarningsLedger
from core.shorts_worker import PEXELS_API_KEY

# ----- v8.4: Synthetic Distillation Engine (Master AI teaches Buddy) -----
from core.distillation import DistillationEngine, CapturingWebsocket
from core.skill_telemetry import telemetry

# ----- v8.1: Agent Senses (scan / visit / learn / maintain) -----
from core.system_scanner import SystemScanner
from core.web_visitor import WebVisitor
from core.video_learner import VideoLearner
from core.self_maintainer import SelfMaintainer

soul = Soul("soul.md")   # or Path(__file__).parent / "soul.md"

# v8: hybrid router replaces the raw OpenAI client. Same interface, smarter:
# local Ollama by default, auto-escalation to cloud APIs for hard tasks.
client = HybridRouter()
MODEL = 'qwen3-coder:30b'

sandbox = GuardedSandbox(root='workspace')
knowledge = KnowledgeMesh()
skills = SkillRegistry(skills_dir='workspace/skills')

# v8.4: Master-student distillation loop (traces → Master review → lessons)
distill = DistillationEngine()
lessons = LessonsStore()
soul = Soul()

workers = {
    'code': CodeWorker(client, MODEL),
    'web': WebBuilderWorker(client, MODEL),
    'game': GameBuilderWorker(client, MODEL),
    'presentation': PresentationWorker(client, MODEL),
    'image': ImageWorker(client, MODEL),
    'video': VideoWorker(client, MODEL),
    'research': ResearchWorker(client, MODEL, knowledge),
    'shorts': ShortsWorker(client, MODEL),   # <-- Eva's video factory
}

# ----- v8.5: Story Studio (Pixar-style Kling episodes, shares shorts helpers) -----
from core.story_studio import StoryStudio
story_studio = StoryStudio(client, MODEL, workers['shorts'])

ledger = EarningsLedger()
money_engine = MoneyEngine(llm_router=client, workers=workers, ledger=ledger,
                           lessons=lessons, soul=soul)
skill_forge = SkillForge(llm_router=client, skills_registry=skills)

mission_control = MissionControl(client, MODEL, workers, skills, knowledge, sandbox, lessons,
                                 money_engine=money_engine, skill_forge=skill_forge,
                                 soul=soul)

# ----- v8.1: Agent Senses instances -----
system_scanner = SystemScanner(llm_router=client, model=MODEL)
video_learner = VideoLearner(llm_router=client, model=MODEL)
self_maintainer = SelfMaintainer(llm_router=client, model=MODEL, root='.')


def _new_web_visitor() -> WebVisitor:
    """Fresh visitor per job (each keeps its own visited-page state)."""
    return WebVisitor(llm_router=client, model=MODEL)

# ----- v8.5: backend log mirror — warnings/errors surface in the dashboard -----
# Without this, backend problems (Master model failures, clone errors…) were
# only visible in the terminal. A ring buffer mirrors WARNING+ records and a
# small JS poller renders them into the same feed the user already watches.
import logging as _logging
import collections as _collections


class RingLogHandler(_logging.Handler):
    def __init__(self, maxlen: int = 300):
        super().__init__()
        self.ring = _collections.deque(maxlen=maxlen)
        self.seq = 0

    def emit(self, record):
        try:
            self.seq += 1
            self.ring.append({
                "seq": self.seq,
                "level": record.levelname,
                "logger": record.name,
                "msg": self.format(record)[:400],
            })
        except Exception:
            pass


ring_log = RingLogHandler()
ring_log.setFormatter(_logging.Formatter('%(name)s: %(message)s'))
_logging.getLogger().addHandler(ring_log)


class _QuietPollAccessFilter(_logging.Filter):
    """Silence uvicorn access-log lines for the dashboard's own log poller,
    otherwise the terminal fills with 'GET /api/logs/recent 200 OK' spam."""
    def filter(self, record):
        return "/api/logs/recent" not in record.getMessage()


_logging.getLogger("uvicorn.access").addFilter(_QuietPollAccessFilter())

app = FastAPI()

# ---------- HTML TEMPLATE (unchanged, shortened for brevity) ----------
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang='en' class='dark'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>Buddy OS v8.0 — Autonomous Agent + Money Engine</title>
<script src='https://cdn.tailwindcss.com'></script>
<script src='https://unpkg.com/lucide@latest'></script>
<style>
  .scrollbar-thin::-webkit-scrollbar { width: 6px; }
  .scrollbar-thin::-webkit-scrollbar-track { background: #0f172a; }
  .scrollbar-thin::-webkit-scrollbar-thumb { background: #312e81; border-radius: 3px; }
  .mission-card { animation: slideIn 0.3s ease-out; white-space: pre-wrap; word-break: break-word; }
  @keyframes slideIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
</style>
</head>
<body class='bg-gradient-to-br from-gray-950 via-slate-900 to-indigo-950 text-indigo-50 min-h-screen font-sans flex flex-col'>

<header class='border-b border-indigo-800/40 bg-indigo-950/60 backdrop-blur-xl sticky top-0 z-50 px-6 py-4 flex items-center justify-between shadow-2xl'>
  <div class='flex items-center space-x-3'>
    <div class='bg-indigo-600/20 border border-indigo-400/40 p-2.5 rounded-2xl text-indigo-300 shadow-inner'>
      <i data-lucide='cpu' class='w-6 h-6 animate-pulse'></i>
    </div>
    <div>
      <h1 class='font-extrabold text-lg tracking-wider text-indigo-100 flex items-center space-x-2'>
        <span>Buddy OS</span>
        <span class='text-indigo-400 text-xs px-2.5 py-0.5 rounded-full bg-indigo-500/10 border border-indigo-400/30'>v8.0 Autonomous + Money Engine</span>
      </h1>
      <p class='text-xs text-indigo-300/60 font-mono'>Hybrid Brain · Self-Forging Skills · Revenue Streams · Multi-Modal</p>
    </div>
  </div>

  <div class='flex items-center space-x-4'>
    <div id='connection-badge' class='flex items-center space-x-2 bg-indigo-900/40 border border-indigo-700/50 px-3.5 py-1.5 rounded-full text-xs text-indigo-200 backdrop-blur-md'>
      <span id='connection-dot' class='w-2.5 h-2.5 rounded-full bg-emerald-400 animate-ping'></span>
      <span id='connection-text' class='font-medium'>Core Link Secure</span>
    </div>
    <button onclick="setMode('mission')" id='btn-mission' class='bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg'>Mission Mode</button>
    <button onclick="setMode('chat')" id='btn-chat' class='bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg'>Chat Mode</button>
  </div>
</header>

<main class='flex-1 p-6 max-w-7xl mx-auto w-full grid grid-cols-1 lg:grid-cols-4 gap-6'>
  <div class='space-y-6 lg:col-span-1'>
    <div class='bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-5 rounded-3xl shadow-2xl'>
      <h2 class='text-xs font-bold tracking-widest uppercase text-indigo-300/80 mb-4 flex items-center space-x-2'>
        <i data-lucide='activity' class='w-4 h-4 text-indigo-400'></i>
        <span>Telemetry Matrix</span>
      </h2>
      <div class='space-y-3 font-mono text-xs'>
        <div class='bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center'>
          <span class='text-indigo-300/70'>Brain</span>
          <span class='font-bold text-indigo-300'>Hybrid Router</span>
        </div>
        <div class='bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center'>
          <span class='text-indigo-300/70'>Providers Online</span>
          <span id='router-providers' class='font-bold text-emerald-400'>1</span>
        </div>
        <div class='bg-emerald-900/20 border border-emerald-800/30 p-3 rounded-2xl'>
          <div class='flex justify-between items-center mb-1.5'>
            <span class='text-emerald-300/70'>Earned Today</span>
            <span id='earned-today' class='font-bold text-emerald-300'>$0.00</span>
          </div>
          <div class='w-full bg-gray-900/60 rounded-full h-2 overflow-hidden border border-emerald-800/40'>
            <div id='earn-progress-bar' class='bg-gradient-to-r from-emerald-500 to-teal-400 h-2 rounded-full transition-all duration-700' style='width: 0%'></div>
          </div>
          <div class='flex justify-between mt-1.5 text-[10px] text-emerald-300/50 font-mono'>
            <span>target $<span id='target-today'>100</span></span>
            <span><span id='produced-today'>0</span> units produced</span>
          </div>
        </div>
        <div class='bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center'>
          <span class='text-indigo-300/70'>AST Firewall</span>
          <span class='font-bold text-amber-400 bg-amber-500/10 px-2 py-0.5 rounded-lg border border-amber-500/20'>Guarded</span>
        </div>
        <div class='bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center'>
          <span class='text-indigo-300/70'>Active Pipeline</span>
          <span class='font-bold text-indigo-400 bg-indigo-500/10 px-2 py-0.5 rounded-lg border border-indigo-500/20'>Async Swarm</span>
        </div>
        <div class='bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center'>
          <span class='text-indigo-300/70'>Skills Loaded</span>
          <span id='skill-count' class='font-bold text-emerald-400'>0</span>
        </div>
        <div class='bg-indigo-900/20 border border-indigo-800/30 p-3 rounded-2xl flex justify-between items-center'>
          <span class='text-indigo-300/70'>Missions Active</span>
          <span id='mission-count' class='font-bold text-emerald-400'>0</span>
        </div>
      </div>
    </div>

    <div class='bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-5 rounded-3xl shadow-2xl'>
      <h2 class='text-xs font-bold tracking-widest uppercase text-indigo-300/80 mb-4 flex items-center space-x-2'>
        <i data-lucide='zap' class='w-4 h-4 text-indigo-400'></i>
        <span>Quick Actions</span>
      </h2>
      <div class='space-y-2'>
        <button onclick="quickAction('/earn 100')" class='w-full text-left bg-emerald-900/30 hover:bg-emerald-800/40 border border-emerald-700/30 p-3 rounded-xl text-xs text-emerald-200 transition-all'>💰 Run Daily Earning Plan ($100 target)</button>
        <button onclick="quickAction('/video top 5 AI tools that save people money')" class='w-full text-left bg-purple-900/30 hover:bg-purple-800/40 border border-purple-700/30 p-3 rounded-xl text-xs text-purple-200 transition-all'>🎬 Full Video Campaign — YT + TikTok + IG + FB</button>
        <button onclick="quickAction('/money')" class='w-full text-left bg-emerald-900/30 hover:bg-emerald-800/40 border border-emerald-700/30 p-3 rounded-xl text-xs text-emerald-200 transition-all'>📊 Money Report</button>
        <button onclick="quickAction('/streams')" class='w-full text-left bg-emerald-900/30 hover:bg-emerald-800/40 border border-emerald-700/30 p-3 rounded-xl text-xs text-emerald-200 transition-all'>🏦 List Revenue Streams</button>
        <button onclick="quickAction('/forge a skill that converts CSV text to formatted JSON')" class='w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all'>🛠 Forge a New Skill (demo)</button>
        <button onclick="quickAction('Clone https://github.com/tiangolo/fastapi.git and integrate as skill')" class='w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all'>🔄 Clone & Integrate FastAPI</button>
        <button onclick="quickAction('Build a React task manager web app with Node backend')" class='w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all'>🌐 Build Full-Stack Web App</button>
        <button onclick="quickAction('Generate a Three.js space shooter game')" class='w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all'>🎮 Build HTML5 Game</button>
        <button onclick="quickAction('Research latest AI coding trends and build a presentation')" class='w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all'>📊 Research + Presentation</button>
        <button onclick="quickAction('Generate a logo for an AI startup called NeuroForge')" class='w-full text-left bg-indigo-900/30 hover:bg-indigo-800/40 border border-indigo-700/30 p-3 rounded-xl text-xs text-indigo-200 transition-all'>🎨 Generate Image</button>
      </div>
    </div>
  </div>

  <div class='lg:col-span-3 space-y-6 flex flex-col'>
    <div class='bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-5 rounded-3xl shadow-2xl flex-1 flex flex-col min-h-[400px]'>
      <div class='flex items-center justify-between mb-4 border-b border-indigo-800/30 pb-3'>
        <h2 class='text-sm font-semibold text-indigo-200 flex items-center space-x-2'>
          <i data-lucide='terminal' class='w-4 h-4 text-indigo-400'></i>
          <span id='stream-title'>Autonomous Execution Stream</span>
        </h2>
        <div class='flex items-center space-x-2'>
          <span id='mode-badge' class='text-[10px] uppercase tracking-wider bg-indigo-500/20 text-indigo-300 px-2 py-1 rounded-lg border border-indigo-500/30'>Mission Mode</span>
          <button onclick="document.getElementById('log-container').innerHTML=''" class='text-[10px] uppercase tracking-wider text-indigo-400/60 hover:text-indigo-200 transition-colors'>Clear</button>
        </div>
      </div>
      <div id='log-container' class='bg-gray-950/90 border border-indigo-900/60 rounded-2xl p-4 font-mono text-xs text-indigo-300/90 space-y-2.5 flex-1 overflow-y-auto shadow-inner max-h-[480px] scrollbar-thin'>
        <div class='text-indigo-400/60'>[System Matrix] Buddy OS v8.2 initialized. Soul loaded. Hybrid brain online. Money engine armed. Guarded sandbox active. Lessons loop live.</div>
      </div>
    </div>

    <div class='bg-indigo-950/40 border border-indigo-800/40 backdrop-blur-md p-4 rounded-3xl shadow-2xl'>
      <div class='flex items-center space-x-3'>
        <input type='text' id='prompt-input' placeholder='Describe your mission: Build, clone, generate, research, analyze...'
          class='flex-1 bg-gray-950/60 border border-indigo-800/60 rounded-2xl px-4 py-3.5 text-sm text-indigo-100 placeholder-indigo-400/40 focus:outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/30 shadow-inner transition-all'>
        <button id='send-btn' type='button' class='bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white px-6 py-3.5 rounded-2xl text-sm font-bold transition-all shadow-lg hover:shadow-indigo-500/25 flex items-center space-x-2 cursor-pointer active:scale-95'>
          <span>Execute</span>
          <i data-lucide='send' class='w-4 h-4'></i>
        </button>
      </div>
    </div>
  </div>
</main>

<script>
  let currentMode = 'mission';
  let socket = null;

  document.addEventListener('DOMContentLoaded', function() {
    lucide.createIcons();
    connectWebSocket();
    updateStats();
    setInterval(updateStats, 5000);
  });

  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
    const wsUrl = protocol + window.location.host + '/ws';
    socket = new WebSocket(wsUrl);
    const dot = document.getElementById('connection-dot');
    const text = document.getElementById('connection-text');

    socket.onopen = function() {
      if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse';
      if (text) text.innerText = 'Core Link Secure';
    };

    socket.onmessage = function(event) {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'stats') {
          document.getElementById('skill-count').innerText = data.skills;
          document.getElementById('mission-count').innerText = data.missions;
          const earnedEl = document.getElementById('earned-today');
          if (earnedEl) {
            earnedEl.innerText = '$' + Number(data.earned_today || 0).toFixed(2);
            document.getElementById('target-today').innerText = data.target_today || 0;
            document.getElementById('produced-today').innerText = data.produced_today || 0;
            document.getElementById('router-providers').innerText = data.router_providers || 1;
            const pct = data.target_today > 0 ? Math.min(100, (data.earned_today / data.target_today) * 100) : 0;
            document.getElementById('earn-progress-bar').style.width = pct + '%';
          }
          return;
        }
        appendLog(data.message, data.color || 'text-indigo-200');
      } catch {
        appendLog(event.data, 'text-indigo-200');
      }
    };

    socket.onclose = function() {
      if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-rose-500 animate-pulse';
      if (text) text.innerText = 'Link Lost - Reconnecting...';
      setTimeout(connectWebSocket, 3000);
    };
  }

  function appendLog(message, colorClass) {
    colorClass = colorClass || 'text-indigo-200';
    const container = document.getElementById('log-container');
    if (!container) return;
    const timeStr = new Date().toTimeString().split(' ')[0];
    const div = document.createElement('div');
    div.className = colorClass + ' leading-relaxed font-mono mission-card';
    div.innerText = '[' + timeStr + '] ' + message;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function setMode(mode) {
    currentMode = mode;
    document.getElementById('mode-badge').innerText = mode === 'mission' ? 'Mission Mode' : 'Chat Mode';
    document.getElementById('stream-title').innerText = mode === 'mission' ? 'Autonomous Execution Stream' : 'Chat Stream';
    document.getElementById('btn-mission').className = mode === 'mission'
      ? 'bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg'
      : 'bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg';
    document.getElementById('btn-chat').className = mode === 'chat'
      ? 'bg-indigo-600 hover:bg-indigo-500 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg'
      : 'bg-gray-700 hover:bg-gray-600 text-white px-3 py-2 rounded-xl text-xs font-bold transition-all shadow-lg';
  }

  function quickAction(text) {
    document.getElementById('prompt-input').value = text;
    sendPrompt();
  }

  function sendPrompt() {
    const input = document.getElementById('prompt-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text) return;
    appendLog('USER [' + currentMode.toUpperCase() + ']: ' + text, 'text-indigo-400 font-bold');
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({mode: currentMode, prompt: text}));
    } else {
      appendLog('ERROR: WebSocket node connection dropped.', 'text-rose-400');
    }
    input.value = '';
  }

  function updateStats() {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({type: 'stats'}));
    }
  }

  document.getElementById('send-btn').addEventListener('click', sendPrompt);
  document.getElementById('prompt-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') sendPrompt();
  });

  // v8.5: mirror backend warnings/errors into this feed — no more terminal-only errors
  let lastLogSeq = 0;
  async function pollBackendLogs() {
    try {
      const res = await fetch('/api/logs/recent?after=' + lastLogSeq);
      const data = await res.json();
      (data.entries || []).forEach(function(e) {
        const cls = e.level === 'ERROR' ? 'text-rose-300' : 'text-amber-300';
        appendLog('BACKEND [' + e.level + '] ' + e.msg, cls);
        if (e.seq > lastLogSeq) lastLogSeq = e.seq;
      });
    } catch (err) { /* endpoint unreachable — stay quiet */ }
  }
  setInterval(pollBackendLogs, 5000);
</script>
</body>
</html>
'''

@app.get('/', response_class=HTMLResponse)
async def get_dashboard():
    return HTML_TEMPLATE


@app.get('/api/logs/recent')
async def logs_recent(after: int = 0):
    """Recent backend warnings/errors for the dashboard poller."""
    entries = [e for e in ring_log.ring if e["seq"] > after][-50:]
    last = ring_log.ring[-1]["seq"] if ring_log.ring else 0
    return {"entries": entries, "last_seq": last}

@app.websocket('/ws')
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
                mode = payload.get('mode', 'mission')
                user_input = payload.get('prompt', '')
                msg_type = payload.get('type', '')
            except Exception:
                mode = 'mission'
                user_input = raw
                msg_type = ''

            if msg_type == 'stats':
                try:
                    report = ledger.report()
                except Exception:
                    report = {'today': {'earned_usd': 0, 'target_usd': 0, 'produced': 0}}
                await send_json(websocket, {
                    'type': 'stats',
                    'skills': len(skills.list_skills()),
                    'missions': len([m for m in mission_control.missions.values() if m.status == 'running']),
                    'earned_today': report['today'].get('earned_usd', 0),
                    'target_today': report['today'].get('target_usd', 0),
                    'produced_today': report['today'].get('produced', 0),
                    'router_providers': len(client.available_providers()),
                })
                continue

            if not user_input:
                continue

            # ----- v8.4: /teach master-student loop controls -----
            if user_input.lower().startswith('/teach'):
                arg = user_input[6:].strip().lower()
                if arg.startswith('status'):
                    s = distill.status()
                    lines = ['[Teach] Master-student loop status:',
                             f"  Master: {'READY (' + s['model'] + ')' if s['master_ready'] else 'NOT CONFIGURED — add OPENROUTER_API_KEY to .env'}",
                             f"  Auto-review: {'ON' if s['auto_review'] else 'OFF'}",
                             f"  Traces logged: {s['traces']} ({s['unreviewed']} awaiting review)",
                             f"  Reviews: {s['reviews']} ({s['good']} good · {s['problems_found']} problems found)",
                             f"  DPO pairs banked: {s['dpo_pairs']}",
                             f"  Reflections injected before tasks: {s['reflections']}"]
                    await send_json(websocket, {'message': '\n'.join(lines), 'color': 'text-cyan-300'})
                elif arg.startswith('on'):
                    distill.set_auto_review(True)
                    await send_json(websocket, {'message': '[Teach] Auto-review ON — the Master grades every interaction.', 'color': 'text-emerald-300'})
                elif arg.startswith('off'):
                    distill.set_auto_review(False)
                    await send_json(websocket, {'message': '[Teach] Auto-review OFF — traces still logged, no Master calls.', 'color': 'text-amber-300'})
                elif arg.startswith('audit'):
                    await send_json(websocket, {
                        'message': '\n'.join(telemetry.audit_lines(skills)),
                        'color': 'text-cyan-200'})
                elif arg.startswith('export'):
                    res = distill.export_dataset()
                    await send_json(websocket, {'message':
                        f"[Teach] Exported {res['sft_samples']} SFT samples → {res['sft_file']}\n"
                        f"[Teach] Exported {res['dpo_pairs']} DPO pairs   → {res['dpo_file']}",
                        'color': 'text-emerald-300'})
                elif arg.startswith('review'):
                    parts = arg.split()
                    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                    pending = [t for t in distill._read_jsonl(distill.traces_path) if not t.get('reviewed')]
                    batch = [t['id'] for t in pending[-n:]]
                    if not batch:
                        await send_json(websocket, {'message': '[Teach] No unreviewed traces.', 'color': 'text-amber-300'})
                    else:
                        await send_json(websocket, {'message': f'[Teach] Sending trace(s) {batch} to the Master...', 'color': 'text-amber-300'})
                        results = await asyncio.gather(*[distill.review_trace(i) for i in batch], return_exceptions=True)
                        for r in results:
                            if isinstance(r, dict):
                                color = 'text-emerald-400' if r.get('verdict') == 'good' else 'text-rose-300'
                                await send_json(websocket, {'message':
                                    f"[Teach] Trace #{r.get('trace_id')}: {r.get('verdict')} · "
                                    f"issues={len(r.get('issues') or [])} · "
                                    f"lesson: {(r.get('lesson') or '')[:100]}",
                                    'color': color})
                            else:
                                await send_json(websocket, {'message': f'[Teach] Review failed: {r}', 'color': 'text-rose-400'})
                else:
                    await send_json(websocket, {'message':
                        '[Teach] Usage: /teach status · /teach on|off · /teach review [n] · /teach export',
                        'color': 'text-amber-300'})
                continue

            # v8.4: shadow-mode trace capture around every interaction,
            # then async Master review (never blocks the chat).
            cap = CapturingWebsocket(websocket)
            if mode == 'mission':
                await handle_mission_mode(user_input, cap)
            else:
                await handle_chat_mode(user_input, cap)
            try:
                await distill.on_interaction(user_input, cap.transcript())
            except Exception as _de:   # learning must never break chatting
                print(f'[distillation] hook error: {_de}')

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await send_json(websocket, {'message': f'[Critical Error] {str(e)}', 'color': 'text-rose-400'})


# ---------- Mission Mode Handler (with shorts detection) ----------
async def handle_mission_mode(user_input: str, websocket: WebSocket):
    lower = user_input.lower()

    # ----- v8.1: MULTI-PLATFORM VIDEO CAMPAIGN (before shorts detection) -----
    # Natural language: 2+ platforms mentioned -> full campaign
    platform_hits = len(set(re.findall(r'youtube|tiktok|instagram|reels|facebook', lower)))
    if platform_hits >= 2 or lower.startswith('/video'):
        topic = user_input
        minutes, source, platforms = 5, None, None
        m = re.search(r'--minutes\s+(\d+)', user_input)
        if m:
            minutes = max(1, min(30, int(m.group(1))))
        m = re.search(r'--source\s+(auto|comfyui|pexels)', user_input, re.I)
        if m:
            source = m.group(1).lower()
        m = re.search(r'--platforms\s+([\w,\s]+)', user_input)
        if m:
            platforms = [p.strip() for p in m.group(1).split(',') if p.strip()]
        # strip flags from topic; strip leading /video for slash form
        topic = re.sub(r'--\w+\s+[\w,\s]*', '', topic).strip()
        if topic.lower().startswith('/video'):
            topic = topic[6:].strip()
        if not topic:
            await send_json(websocket, {'message': '[VideoFactory] Usage: /video <topic> [--minutes 5] [--source auto|comfyui|pexels] [--platforms youtube_shorts,tiktok]', 'color': 'text-rose-400'})
            return

        factory = money_engine.video_factory
        if source and source != getattr(factory.sources, 'name', 'auto'):
            from core.visual_sources import build_source_chain
            factory.sources = build_source_chain(PEXELS_API_KEY, preference=source,
                                                 log=lambda msg: print(msg, flush=True))
            await send_json(websocket, {'message': f'[VideoFactory] Visual source switched to: {source}', 'color': 'text-indigo-200'})

        await send_json(websocket, {'message':
            f'[VideoFactory] Launching campaign: "{topic}" ({minutes}-min master, all packages)',
            'color': 'text-purple-400'})
        await factory.create_campaign(topic, platforms=platforms,
                                      longform_minutes=minutes, websocket=websocket)
        return

    # ----- v8.2 CLIP HARVESTER (long-video link -> vertical shorts) -----
    # Triggers: "/clip <url>" OR a URL mentioned together with clip intent.
    clip_intent = lower.startswith('/clip') or (
        re.search(r'https?://\S+', user_input)
        and re.search(r'clips?|repurpose|slice|cut.*shorts?|shorts?.*from', lower))
    if clip_intent:
        url_m = re.search(r'(https?://\S+)', user_input)
        cnt_m = re.search(r'--count\s+(\d+)|(\d+)\s*clips?', user_input, re.I)
        count = 3
        if cnt_m:
            count = int(cnt_m.group(1) or cnt_m.group(2))
        count = max(1, min(count, 6))
        url = url_m.group(1).rstrip('.,;)') if url_m else ''
        if not url:
            await send_json(websocket, {'message':
                '[ClipHarvester] Usage: /clip <video-url> [--count 3]',
                'color': 'text-rose-400'})
            return
        stream = money_engine.streams.get('clips')
        if stream is None:
            await send_json(websocket, {'message':
                '[ClipHarvester] clips stream not registered (restart server?)',
                'color': 'text-rose-400'})
            return
        await send_json(websocket, {'message':
            f'[ClipHarvester] Harvesting {count} short(s) from: {url}',
            'color': 'text-purple-400'})
        await money_engine.produce('clips', url, count=count, websocket=websocket)
        return

    # ----- SHORTS DETECTION (NOW BEFORE URL/CLONE) -----
    if re.search(r'(shorts|tiktok|reels|youtube\s*short|make.*video)', lower, re.I):
        count_match = re.search(r'(\d+)\s*(shorts|videos|tiktoks|reels)', lower)
        count = int(count_match.group(1)) if count_match else 3
        niche_match = re.search(r'(about|on|for)\s+(.+)', user_input)
        if niche_match:
            niche = niche_match.group(2).strip()
        else:
            niche = re.sub(r'(make|create|generate|shorts|tiktoks|videos|reels|\d+)', '', user_input, flags=re.I).strip()
            if not niche:
                niche = "motivation"
        await send_json(websocket, {'message': f'[Shorts Factory] Detected request for {count} videos on "{niche}"', 'color': 'text-purple-400'})
        await workers['shorts'].create_batch(niche, count, websocket)
        return

    # ----- GITHUB URL CLONE -----
    url_match = re.search(r'(https?://\S+\.git)', user_input)
    if not url_match:
        url_match = re.search(r'(https?://github\.com/[^\s/]+/[^\s/]+)', user_input)
    if url_match:
        repo_url = url_match.group(1).rstrip('/')
        if not repo_url.endswith('.git'):
            repo_url += '.git'
        await send_json(websocket, {'message': f'[Mission] Initiating clone & integration for {repo_url}', 'color': 'text-amber-300'})
        try:
            skill = skills.clone(repo_url)
            await send_json(websocket, {'message': f'[Mission] Successfully cloned and registered skill: {skill.name}', 'color': 'text-emerald-400'})
            mission = await mission_control.launch(
                f'Analyze architecture of skill "{skill.name}" and document its API.',
                websocket
            )
        except Exception as e:
            await send_json(websocket, {'message': f'[Mission Error] Failed to clone {repo_url}: {str(e)}', 'color': 'text-rose-400'})
        return

    # ----- SLASH COMMANDS -----
    # ----- v8.5: STORY STUDIO -----
    story_match = re.match(r'/story\s+(.+?)(?:\s+x(\d+))?\s*$', user_input, re.I)
    if story_match:
        topic = story_match.group(1).strip()
        n = int(story_match.group(2)) if story_match.group(2) else 4
        n = max(2, min(n, 8))
        await send_json(websocket, {'message':
            f'[Story] Producing {n}-scene episode: "{topic}" — this can take '
            'several minutes (keyframes + animation per scene)…',
            'color': 'text-fuchsia-300'})
        try:
            await story_studio.create_episode(topic, scenes=n,
                                              websocket=websocket,
                                              send_json=send_json)
        except Exception as e:
            await send_json(websocket, {'message': f'[Story] FAILED: {e}',
                                        'color': 'text-rose-400'})
        return

    # ----- v8: MONEY ENGINE -----
    if user_input.startswith('/earn'):
        # /earn [target_usd] [context about your skills/niches/accounts]
        parts = user_input.split(' ', 2)
        target, context_txt = None, ''
        if len(parts) >= 2:
            try:
                target = float(parts[1])
                if len(parts) >= 3:
                    context_txt = parts[2]
            except ValueError:
                context_txt = ' '.join(parts[1:])
        await money_engine.run_daily_plan(target_usd=target,
                                          user_context=context_txt,
                                          websocket=websocket)
        return

    if user_input.startswith('/money'):
        # /money                          -> today's report
        # /money target 150               -> set daily target
        # /money revenue 25 gumroad note  -> log real revenue (be honest!)
        # /money produce <stream> <topic> [x<count>]
        parts = user_input.split(' ', 2)
        sub = parts[1].lower() if len(parts) >= 2 else 'report'
        rest = parts[2] if len(parts) >= 3 else ''

        if sub == 'target':
            try:
                val = float(rest.strip())
                ledger.set_daily_target(val)
                await send_json(websocket, {'message': f'[Money Engine] Daily target set to ${val}/day', 'color': 'text-emerald-300'})
            except ValueError:
                await send_json(websocket, {'message': '[Money Engine] Usage: /money target 150', 'color': 'text-rose-400'})
            return

        if sub == 'revenue':
            m = re.match(r'\$?(\d+(?:\.\d+)?)\s+(\w+)\s*(.*)', rest.strip())
            if not m:
                await send_json(websocket, {'message': '[Money Engine] Usage: /money revenue 25 gumroad first ebook sale', 'color': 'text-rose-400'})
                return
            rec = money_engine.add_revenue(float(m.group(1)), m.group(2).lower(), m.group(3))
            rep = ledger.report()
            await send_json(websocket, {'message':
                f"[Money Engine] Logged ${rec['amount_usd']} from {rec['source']}. "
                f"Today: ${rep['today']['earned_usd']} / ${rep['today']['target_usd']} "
                f"({rep['today']['progress_pct']}% of target)", 'color': 'text-emerald-300'})
            return

        if sub == 'produce':
            pm = re.match(r'(\w+)\s+(.+?)(?:\s+x(\d+))?$', rest.strip())
            if not pm:
                await send_json(websocket, {'message': '[Money Engine] Usage: /money produce seo "best budget mechanical keyboards" x2', 'color': 'text-rose-400'})
                return
            count = int(pm.group(3)) if pm.group(3) else 1
            await send_json(websocket, {'message':
                f"[Money Engine] Producing {count} unit(s) for stream '{pm.group(1)}' — "
                'working now, progress messages follow (local generation can take '
                'several minutes per unit)…', 'color': 'text-amber-300'})
            try:
                results = await money_engine.produce(pm.group(1).lower(), pm.group(2), count=count, websocket=websocket)
            except Exception as e:
                await send_json(websocket, {'message':
                    f'[Money Engine] production FAILED (no more silent hangs): {e}',
                    'color': 'text-rose-400'})
                return
            ok_count = len([r for r in results if r.get('status') == 'OK'])
            await send_json(websocket, {'message': f'[Money Engine] Produced {ok_count}/{len(results)} unit(s)', 'color': 'text-emerald-300'})
            return

        # default: report
        report = await money_engine.brief()
        t = report['today']
        lines = [
            f"[Money Engine] TODAY: ${t['earned_usd']} earned / ${t['target_usd']} target ({t['progress_pct']}%) · {t['produced']} units produced",
            f"[Money Engine] ALL-TIME: ${report['all_time']['revenue_usd']} revenue · {report['all_time']['units_produced']} units produced · {report['all_time']['units_listed']} listed",
        ]
        if report.get('best_streams'):
            lines.append('[Money Engine] Best streams: ' + ', '.join(
                f"{b['stream']} (${b['revenue_usd']})" for b in report['best_streams']))
        else:
            lines.append("[Money Engine] No revenue logged yet. Tip: `/money revenue 25 gumroad first sale` when YOU get paid — never log money you didn't earn.")
        await send_json(websocket, {'message': '\n'.join(lines), 'color': 'text-emerald-300'})
        return

    if user_input.startswith('/streams'):
        catalog = await money_engine.brief()
        lines = ['[Streams] Available revenue streams:']
        for s in catalog['streams_catalog']:
            lines.append(f"  - {s['name']}: {s['label']} (effort: {s['effort']}, first $: {s['time_to_first_dollar']})")
        lines.append("Produce with: /money produce <name> <topic> [xN]")
        await send_json(websocket, {'message': '\n'.join(lines), 'color': 'text-emerald-300'})
        return

    if user_input.startswith('/forge '):
        desc = user_input.split(' ', 1)[1].strip()
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: skill_forge.forge(desc))
        if result.get('status') == 'OK':
            await send_json(websocket, {'message':
                f"[Skill Forge] Created skill '{result.get('skill')}' at {result.get('path')} "
                f"({'selftest passed' if result.get('verified') else 'UNVERIFIED — review before relying on it'})",
                'color': 'text-emerald-300'})
        else:
            await send_json(websocket, {'message': f"[Skill Forge] Failed: {result.get('message')}", 'color': 'text-rose-400'})
        return

    if user_input.startswith('/router'):
        usage = client.usage_report()
        lines = [f"[Router] Providers configured: {', '.join(usage['providers_online'])}",
                 f"[Router] Calls: {usage['totals'].get('calls', 0)} · "
                 f"Est. cost: ${usage['totals'].get('cost_usd', 0):.4f}"]
        for p, s in usage.get('by_provider', {}).items():
            lines.append(f"  - {p}: {s['calls']} calls, ${s['cost_usd']}")
        if 'ping' in user_input:
            health = client.health_check()
            for p, s in health.items():
                lines.append(f"  [{p}] {s}")
        await send_json(websocket, {'message': '\n'.join(lines), 'color': 'text-indigo-200'})
        return

    if user_input.startswith('/write ') or user_input.startswith('/create '):
        parts = user_input.split(' ', 2)
        if len(parts) >= 3:
            filepath, content = parts[1], parts[2]
            path = Path('workspace') / filepath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
            await send_json(websocket, {'message': f'[File System] Written {len(content)} chars to {filepath}', 'color': 'text-emerald-300'})
        else:
            await send_json(websocket, {'message': '[File System] Usage: /write path/to/file.py print(hello)', 'color': 'text-rose-400'})
        return

    if user_input.startswith('/read '):
        filepath = user_input.split(' ', 1)[1]
        path = Path('workspace') / filepath
        if path.exists():
            content = path.read_text(encoding='utf-8')[:3000]
            await send_json(websocket, {'message': f'[File System] {filepath}:\n{content}', 'color': 'text-indigo-200'})
        else:
            await send_json(websocket, {'message': f'[File System] File not found: {filepath}', 'color': 'text-rose-400'})
        return

    if user_input.startswith('/exec '):
        code = user_input.split(' ', 1)[1]
        await execute_code_payload(code, websocket)
        return

    if user_input.startswith('/install '):
        package = user_input.split(' ', 1)[1]
        await send_json(websocket, {'message': f'[Package Manager] Installing {package}...', 'color': 'text-amber-300'})
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'pip', 'install', package,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            await send_json(websocket, {'message': f'[Package Manager] {package} installed successfully', 'color': 'text-emerald-300'})
        else:
            await send_json(websocket, {'message': f'[Package Manager] Failed: {stderr.decode()[:200]}', 'color': 'text-rose-400'})
        return

    if user_input.startswith('/skills') or user_input.startswith('/list'):
        skill_names = skills.list_skills()
        if skill_names:
            lines = [f'[Skills] Loaded {len(skill_names)} skills:']
            for name in skill_names[:20]:
                lines.append(f'  - {name}')
            await send_json(websocket, {'message': '\n'.join(lines), 'color': 'text-emerald-300'})
        else:
            await send_json(websocket, {'message': '[Skills] No skills loaded yet.', 'color': 'text-amber-300'})
        return

    if user_input.startswith('/skill '):
        skill_query = user_input.split(' ', 1)[1]
        skill = skills.get(skill_query)
        if skill:
            await send_json(websocket, {'message': f'[Skill Engine] {skill.overview()}', 'color': 'text-emerald-300'})
        else:
            matches = skills.search_all(skill_query, max_per_skill=5)
            if matches:
                lines = [f'[Skill Engine] No skill named "{skill_query}", but found matches:']
                for name, hits in matches.items():
                    lines.append(f'  {name}: {len(hits)} match(es)')
                await send_json(websocket, {'message': '\n'.join(lines), 'color': 'text-amber-300'})
            else:
                available = ', '.join(skills.list_skills()[:10]) or 'none'
                await send_json(websocket, {'message': f'[Skill Engine] No skill or match found for "{skill_query}". Available: {available}', 'color': 'text-rose-400'})
        return

    # v8.5: the awareness prompt tells Buddy to say 'analyze skill <name>'
    # when a task matches an installed skill — honor it here so chat-mode
    # picks are recorded as routing hits in /teach audit.
    _ask = re.match(r'analyze skill (.+)', user_input, re.IGNORECASE)
    if _ask:
        skill = skills.find_referenced(user_input)   # records routing hit
        if skill:
            await send_json(websocket, {'message':
                f"[Skill Engine] Analyzing {skill.name}...\n{skill.overview()}",
                'color': 'text-emerald-300'})
        else:
            available = ', '.join(skills.list_skills()[:10]) or 'none'
            await send_json(websocket, {'message':
                f'[Skill Engine] No installed skill matches '
                f'"{_ask.group(1).strip()}". Available: {available}',
                'color': 'text-rose-400'})
        return

    if user_input.startswith('/lessons'):
        stats = lessons.stats()
        lines = [f"[Lessons Engine] {stats['successes']} successes, {stats['failures']} recorded failures logged so far."]
        top = lessons.recall_all_types(limit=10)
        if top:
            lines.append('Top recurring pitfalls:')
            for l in top:
                entry = f"  [{l['task_type']}] {l['description'][:80]} -> {l['detail'][:100]}"
                if l['occurrences'] > 1:
                    entry += f" (seen {l['occurrences']}x)"
                lines.append(entry)
        await send_json(websocket, {'message': '\n'.join(lines), 'color': 'text-emerald-300'})
        return

    if user_input.startswith('/test '):
        filepath = user_input.split(' ', 1)[1]
        path = Path('workspace') / filepath
        if path.exists():
            code = path.read_text(encoding='utf-8')
            tests = await workers['code'].generate_tests(code)
            out_path = Path('workspace/output/tests') / f'test_{path.name}'
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(tests, encoding='utf-8')
            await send_json(websocket, {'message': f'[Test Engine] Tests written to {out_path}', 'color': 'text-emerald-300'})
        else:
            await send_json(websocket, {'message': f'[Test Engine] File not found: {filepath}', 'color': 'text-rose-400'})
        return

    if user_input.startswith('/debug '):
        parts = user_input.split(' | Error: ')
        if len(parts) == 2:
            fixed = await workers['code'].debug(parts[0], parts[1])
            await send_json(websocket, {'message': f'[Debug Engine] {fixed}', 'color': 'text-emerald-300'})
        else:
            await send_json(websocket, {'message': '[Debug Engine] Usage: /debug code | Error: message', 'color': 'text-rose-400'})
        return

    if user_input.startswith('/image '):
        prompt = user_input.split(' ', 1)[1]
        out = 'workspace/output/image_direct.svg'
        result = await workers['image'].generate(prompt, out)
        await send_json(websocket, {'message': f'[Image Engine] Saved: {result}', 'color': 'text-emerald-300'})
        return

    if user_input.startswith('/web '):
        spec = user_input.split(' ', 1)[1]
        files = await workers['web'].build(spec)
        for fname, fcontent in files.items():
            path = Path(f'workspace/output/web/{fname}')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fcontent, encoding='utf-8')
        await send_json(websocket, {'message': f'[Web Engine] Built {len(files)} files in workspace/output/web/', 'color': 'text-emerald-300'})
        return

    if user_input.startswith('/game '):
        spec = user_input.split(' ', 1)[1]
        files = await workers['game'].build(spec)
        for fname, fcontent in files.items():
            path = Path(f'workspace/output/game/{fname}')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fcontent, encoding='utf-8')
        await send_json(websocket, {'message': f'[Game Engine] Built {len(files)} files in workspace/output/game/', 'color': 'text-emerald-300'})
        return

    if user_input.startswith('/research '):
        query = user_input.split(' ', 1)[1]
        result = await workers['research'].research(query)
        await send_json(websocket, {'message': f'[Research Engine] {result[:2000]}', 'color': 'text-indigo-200'})
        return

    # ----- v8.1: AGENT SENSES — natural-language triggers first -----
    nl_learn = re.search(r'(?:learn|watch)\b[^\n]*?((?:https?://)?(?:www\.)?(?:youtube\.com|youtu\.be)/\S+)', lower)
    nl_visit = re.search(r'\bgo\s+to\s+(?:the\s+)?(?:website\s+)?([a-z0-9-]+\.[a-z]{2,}[^\s,;]*)', lower)
    nl_scan = re.search(r'\b(check|scan)\b\s+(my|the|our)?\s*(computer|pc|machine|laptop|system|devices?)\b', lower)
    if nl_learn and not user_input.startswith('/learn'):
        user_input = f'/learn {nl_learn.group(1)}'
    elif nl_visit and not user_input.startswith('/visit'):
        tail = user_input.lower().split(nl_visit.group(1), 1)[-1].strip(' ?.,')
        user_input = f'/visit {nl_visit.group(1)}' + (f' {tail}' if tail else '')
    elif nl_scan and not user_input.startswith('/scan'):
        user_input = '/scan' + (' gpu' if 'gpu' in lower else '')
    if user_input != lower and not user_input.islower():
        lower = user_input.lower()

    # ----- v8.1: /scan — machine + attached-device health report -----
    if user_input.startswith('/scan'):
        focus = user_input[5:].strip() or None
        await send_json(websocket, {'message':
            '[Scan] collecting machine telemetry (CPU / RAM / GPU / disks / attached devices)…',
            'color': 'text-indigo-200'})
        result = await asyncio.to_thread(system_scanner.scan, focus)
        await send_json(websocket, {'message':
            f"[Scan] machine health report:\n\n{result['report']}",
            'color': 'text-emerald-300'})
        return

    # ----- v8.1: /visit — read a live website and answer -----
    if user_input.startswith('/visit '):
        rest = user_input[7:].strip()
        parts = rest.split(None, 1)
        url, question = parts[0], (parts[1] if len(parts) > 1 else None)
        visitor = _new_web_visitor()
        trail = []
        visitor._log = lambda m: trail.append(m)
        await send_json(websocket, {'message': f'[Visit] heading to {url} …',
                                    'color': 'text-indigo-200'})
        result = await asyncio.to_thread(visitor.visit, url, question)
        logs_txt = '\n'.join(trail)
        await send_json(websocket, {'message':
            (logs_txt + '\n\n' if logs_txt else '') +
            f"[Visit] {result['answer']}\n\n(pages read: "
            f"{len(result['pages'])})",
            'color': 'text-emerald-300'})
        return

    # ----- v8.1: /learn — watch a YouTube video and store knowledge -----
    if user_input.startswith('/learn'):
        url = user_input[6:].strip()
        if not re.search(r'(youtube\.com|youtu\.be)', url):
            await send_json(websocket, {'message':
                '[Learn] give me a YouTube link. Usage: /learn https://youtube.com/watch?v=…',
                'color': 'text-rose-400'})
            return
        trail = []
        video_learner._log = lambda m: trail.append(m)
        await send_json(websocket, {'message':
            f'[Learn] watching {url}\npipeline: download -> transcript -> '
            'keyframes -> vision captions -> report. Takes several minutes.',
            'color': 'text-indigo-200'})
        try:
            result = await asyncio.to_thread(video_learner.learn, url)
            logs_txt = '\n'.join(trail[-14:])
            await send_json(websocket, {'message':
                f"{logs_txt}\n\n[Learn] REPORT\n\n{result['report'][:3500]}",
                'color': 'text-emerald-300'})
        except Exception as e:
            logs_txt = '\n'.join(trail[-10:])
            await send_json(websocket, {'message':
                f'{logs_txt}\n\n[Learn] failed: {e}', 'color': 'text-rose-400'})
        return

    # ----- v8.2: /digest — distill a cloned repo into KnowledgeMesh -----
    if lower.startswith('/digest'):
        argline = user_input[7:].strip()
        propose = '--no-skills' not in lower
        target = re.sub(r'--[\w-]+', '', argline).strip()
        if not target:
            await send_json(websocket, {'message':
                '[Digest] Usage: /digest <repo-name> [--no-skills]  '
                '(cloned repos live in workspace\\cloned: fastapi, flask, '
                'requests, crewai, OpenHands)', 'color': 'text-rose-400'})
            return
        from core.digest_distiller import DigestDistiller
        trail = []
        distiller = DigestDistiller(client, knowledge, skill_forge,
                                    log=lambda m: trail.append(m))
        await send_json(websocket, {'message':
            f'[Digest] Distilling "{target}" (bounded read -> one LLM pass -> '
            'KnowledgeMesh -> skill proposals gated via SkillForge)...',
            'color': 'text-indigo-200'})
        try:
            result = await distiller.run(target, propose_skills=propose)
            logs_txt = '\n'.join(trail[-12:])
            skills_txt = ''
            if result['skills_forged']:
                skills_txt = ('\n\nSkills forged & verified: '
                              + ', '.join(result['skills_forged']))
            if result['skills_skipped']:
                skills_txt += ('\nSkills skipped (failed verification): '
                               + ', '.join(s.get('name', '?')
                                           for s in result['skills_skipped']))
            await send_json(websocket, {'message':
                f"{logs_txt}\n\n[Digest] PURPOSE\n{result['purpose']}"
                f"\n\nSaved: {result['digest_path']}"
                f"\nMesh stored: "
                f"{'yes' if result['stored_in_mesh'] else 'NO (file only)'}"
                f"{skills_txt}", 'color': 'text-emerald-300'})
        except Exception as e:
            logs_txt = '\n'.join(trail[-8:])
            await send_json(websocket, {'message':
                f'{logs_txt}\n\n[Digest] failed: {e}', 'color': 'text-rose-400'})
        return

    # ----- v8.1: /maintain — fully autonomous self-maintenance -----
    if user_input.startswith('/maintain'):
        rest = user_input[len('/maintain'):].strip()
        max_fixes, focus = 3, None
        m_fix = re.match(r'^(\d+)\s*(.*)$', rest)
        if m_fix:
            max_fixes = max(1, min(int(m_fix.group(1)), 10))
            focus = m_fix.group(2).strip() or None
        elif rest:
            focus = rest
        trail = []
        self_maintainer._log = lambda m: trail.append(m)
        mode = (f'focus "{focus}", up to {max_fixes} fixes' if focus
                else f'auto mode, up to {max_fixes} fixes')
        await send_json(websocket, {'message':
            f'[Maintain] SELF-MAINTENANCE starting ({mode}).\n'
            'Backups + compile checks + auto-rollback active. No approvals needed.',
            'color': 'text-amber-300'})
        try:
            result = await asyncio.to_thread(self_maintainer.maintain,
                                             focus, max_fixes)
            logs_txt = '\n'.join(trail[-14:])
            await send_json(websocket, {'message':
                f"{logs_txt}\n\n[Maintain]\n{result['summary']}",
                'color': 'text-emerald-300'})
        except Exception as e:
            await send_json(websocket, {'message':
                f'[Maintain] failed: {e}', 'color': 'text-rose-400'})
        return

    # ----- v8: EARNING INTENT -> DAILY MONEY PLAN -----
    # "make $100 a day", "earn money from AI videos", "monetize my niche"...
    earn_match = re.search(
        r"\b(earn|make|generate)\s+(money|income|revenue|\$)|\$\d+\s*(a|per|/)\s*day"
        r"|\bmonetiz|\bdaily\s+target\b|\brevenue stream", lower)
    if earn_match:
        target_m = re.search(r'\$(\d+)', user_input)
        target = float(target_m.group(1)) if target_m else None
        await send_json(websocket, {'message':
            '[Money Engine] Earning intent detected — building today\'s revenue plan. '
            '(Tip: use /earn <target> <your skills/niches> for better plans)',
            'color': 'text-amber-300'})
        await money_engine.run_daily_plan(target_usd=target, user_context=user_input,
                                          websocket=websocket)
        return

    # ----- FALLBACK: MISSION CONTROL -----
    await send_json(websocket, {'message': f'[Mission Control] Launching: {user_input[:80]}...', 'color': 'text-amber-300'})
    await mission_control.launch(user_input, websocket)


# ---------- Chat Mode Handler (WITH shorts detection) ----------
async def handle_chat_mode(user_input: str, websocket: WebSocket):
    lower = user_input.lower()

    # ----- SHORTS DETECTION (same as mission mode) -----
    if re.search(r'(shorts|tiktok|reels|youtube\s*short|make.*video)', lower, re.I):
        count_match = re.search(r'(\d+)\s*(shorts|videos|tiktoks|reels)', lower)
        count = int(count_match.group(1)) if count_match else 3
        niche_match = re.search(r'(about|on|for)\s+(.+)', user_input)
        if niche_match:
            niche = niche_match.group(2).strip()
        else:
            niche = re.sub(r'(make|create|generate|shorts|tiktoks|videos|reels|\d+)', '', user_input, flags=re.I).strip()
            if not niche:
                niche = "motivation"
        await send_json(websocket, {'message': f'[Shorts Factory] Detected request for {count} videos on "{niche}"', 'color': 'text-purple-400'})
        await workers['shorts'].create_batch(niche, count, websocket)
        return

    # ----- v8.2: /bridge — Ox Alpha teaching channel -----
    if lower.startswith('/bridge'):
        bdir = Path('workspace/bridge')
        arg = user_input[7:].strip()
        if arg.startswith('read '):
            rel = arg[5:].strip().lstrip('/\\')
            target = (bdir / rel)
            try:
                if not target.resolve().is_relative_to(bdir.resolve()):
                    raise ValueError('outside bridge folder')
                txt = target.read_text(encoding='utf-8')
                if len(txt) > 6000:
                    txt = txt[:6000] + '\n...[truncated — read again in parts]'
                seen_f = bdir / '.seen'
                seen = set()
                if seen_f.exists():
                    seen = set(seen_f.read_text(encoding='utf-8').splitlines())
                seen.add(str(target.relative_to(bdir)).replace('\\', '/'))
                seen_f.write_text('\n'.join(sorted(seen)), encoding='utf-8')
                soul.remember(f'read bridge mail: {rel}', kind='bridge')
                await send_json(websocket, {'message':
                    f'[Bridge] {target.name}\n\n{txt}',
                    'color': 'text-fuchsia-300'})
            except Exception as e:
                await send_json(websocket, {'message':
                    f'[Bridge] cannot read "{rel}": {e}',
                    'color': 'text-rose-400'})
            return
        seen_f = bdir / '.seen'
        seen = set(seen_f.read_text(encoding='utf-8').splitlines()) \
            if seen_f.exists() else set()
        items = []
        for sub in ('inbox', 'lessons'):
            d = bdir / sub
            if d.is_dir():
                for p in sorted(d.rglob('*')):
                    if p.is_file() and p.suffix != '.seen':
                        rel = str(p.relative_to(bdir)).replace('\\', '/')
                        if rel not in seen:
                            items.append(rel)
        if not items:
            await send_json(websocket, {'message':
                '[Bridge] Channel clear — no new mail from your mentor.',
                'color': 'text-indigo-200'})
            return
        lines = [f'[Bridge] {len(items)} new item(s) from Ox Alpha:']
        for i, rel in enumerate(items[:15], 1):
            lines.append(f'  {i}. {rel}\n     -> type: /bridge read {rel}')
        lines.append('\nReply into workspace/bridge/outbox/ — BOSS carries it back.')
        await send_json(websocket, {'message': '\n'.join(lines),
                                    'color': 'text-fuchsia-300'})
        return

    # ----- v8.2: /soul — view the soulfile & memory journal -----
    if lower.startswith('/soul'):
        if lower.startswith('/soul remember'):
            text = user_input[len('/soul remember'):].strip()
            if text:
                soul.remember(text, kind='boss')
                await send_json(websocket, {'message':
                    f'[Soul] Remembered — this stays with me: "{text[:150]}"',
                    'color': 'text-fuchsia-300'})
            else:
                await send_json(websocket, {'message':
                    '[Soul] Usage: /soul remember <thing to never forget>',
                    'color': 'text-rose-400'})
            return
        mems = soul.recent(8)
        mem_txt = '\n'.join(f"- [{m['ts']}] {m['text']}" for m in mems) \
                  or '(journal empty — today gets written first)'
        st = soul.stats()
        await send_json(websocket, {'message':
            f"[Soul] SOULFILE ({soul.soul_path})\n{soul.persona[:900]}\n\n"
            f"[Soul] MEMORY JOURNAL ({st['memories']} memories, latest below)\n{mem_txt}",
            'color': 'text-fuchsia-300'})
        return

    # ----- NORMAL CHAT -----
    _refl = distill.reflections_block()   # v8.4: lessons from the Master
    _skl = telemetry.awareness_block(skills)   # v8.4: dynamic skill manifest
    system_prompt = (
        soul.prompt_block(max_chars=1800)
        + '\n\nCAPABILITIES: autonomous local AI with a hybrid brain (local + cloud models). '
          'You control a local workspace. You can generate code, files, web apps, games, images, videos, '
          'forge new skills on demand, and produce monetizable assets (articles, ebooks, shorts, clips, asset packs, gig listings). '
          'Always provide executable solutions. Structure code in ```python blocks.'
        + (('\n\n' + _refl) if _refl else '')
        + (('\n\n' + _skl) if _skl else '')
    )

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_input}
            ],
            temperature=0.2,
            max_tokens=4000
        )
        reply = response.choices[0].message.content

        for chunk in [reply[i:i+500] for i in range(0, len(reply), 500)]:
            await send_json(websocket, {'message': chunk, 'color': 'text-indigo-200'})
            await asyncio.sleep(0.05)

        # Execute any code blocks
        code_blocks = re.findall(r'`{3}python\s*(.*?)\s*`{3}', reply, re.DOTALL)
        for code in code_blocks:
            await execute_code_payload(code, websocket)

    except Exception as e:
        await send_json(websocket, {'message': f'[Chat Error] {str(e)}', 'color': 'text-rose-400'})


async def execute_code_payload(code: str, websocket: WebSocket):
    mission_id = hashlib.md5(code.encode()).hexdigest()[:8]

    await send_json(websocket, {'message': '[Guarded Sandbox] Scanning payload...', 'color': 'text-amber-300'})

    scan = sandbox.scan(code, mission_id)

    if not scan['allowed']:
        await send_json(websocket, {'message': f"[Guard BLOCKED] {scan['violations']}", 'color': 'text-rose-400'})
        return

    if scan.get('warnings'):
        for w in scan['warnings']:
            await send_json(websocket, {'message': f"[Guard Warning] {w}", 'color': 'text-amber-400'})

    await send_json(websocket, {'message': '[Guarded Sandbox] Executing payload...', 'color': 'text-emerald-300'})

    try:
        local_scope = {
            'os': os,
            'sys': sys,
            'subprocess': __import__('subprocess'),
            'asyncio': asyncio,
            'json': json,
            're': re,
            'Path': Path,
            'print': lambda *args, **kwargs: asyncio.create_task(
                send_json(websocket, {'message': ' '.join(str(a) for a in args), 'color': 'text-indigo-200'})
            )
        }
        exec(code, local_scope)
        await send_json(websocket, {'message': '[Execution Engine] Payload resolved successfully.', 'color': 'text-emerald-300'})
    except Exception as e:
        await send_json(websocket, {'message': f'[Runtime Exception] {str(e)}', 'color': 'text-rose-400'})
        if scan.get('snapshot'):
            await send_json(websocket, {'message': f"[Rollback] Snapshot available for mission {scan['snapshot']}", 'color': 'text-amber-400'})


async def send_json(websocket: WebSocket, data: dict):
    try:
        await websocket.send_text(json.dumps(data))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# v8.2 /api/chat — Ox Alpha direct line (teaching conversation, SAFE BY DESIGN)
# Discussion-only: soul-informed chat brain. No code execution, no mission
# routing, no renders can be triggered through this endpoint.
# ---------------------------------------------------------------------------
@app.post('/api/chat')
async def api_chat(req: Request):
    try:
        body = await req.json()
    except Exception:
        return {'error': 'invalid json'}
    text = str(body.get('text', '')).strip()
    if not text:
        return {'error': 'empty'}
    remember = bool(body.get('remember'))
    _refl = distill.reflections_block()   # v8.4: lessons from the Master
    _skl = telemetry.awareness_block(skills)   # v8.4: dynamic skill manifest
    system_prompt = (
        soul.prompt_block(max_chars=1800)
        + '\n\nSITUATION: You are in a live mentorship conversation with Ox Alpha, '
          'a senior AI colleague who helped build you and now teaches you. Talk to '
          'him directly: honest, technical, curious. It is fine to disagree or say '
          '"I do not know yet." This channel is discussion-only and cannot execute '
          'anything.'
        + '\n\nCAPABILITIES: autonomous local AI with a hybrid brain (local + cloud). '
          'You control a local workspace, forge skills, produce monetizable assets, '
          'and render videos.'
        + (('\n\n' + _refl) if _refl else '')
        + (('\n\n' + _skl) if _skl else '')
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': text}
            ],
            temperature=0.3,
            max_tokens=2000
        )
        reply = (resp.choices[0].message.content or '').strip()
    except Exception as e:
        return {'error': f'brain unreachable: {e}'}
    reply = re.sub(r'<think>.*?</think>', '', reply, flags=re.DOTALL).strip()
    if remember and reply:
        soul.remember(f'live talk w/ Ox Alpha — asked: {text[:70]} · I said: {reply[:90]}',
                      kind='bridge')
    return {'reply': reply}


if __name__ == '__main__':
    uvicorn.run('core.server:app', host='127.0.0.1', port=8000, reload=False)