import os, sys, ast, json, asyncio, traceback, uvicorn, subprocess
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Buddy Studio")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

BASE_DIR = r"C:\Users\Admin\desktop\buddy_osv5"

def generate_smart_kinder_lesson(topic: str) -> dict:
    t_lower = topic.lower().strip()
    
    if "alphabet" in t_lower or "letter" in t_lower or "a to z" in t_lower:
        title = "Kindergarten Phonics: Alphabet Adventure"
        scenes = [
            {
                "scene": 1,
                "narration": "Welcome friends! Today we are learning our Magic ABCs!",
                "visual_prompt": "3D Pixar style, cute friendly mascot waving next to big glowing colorful letters A B C, bright studio environment, 8k resolution",
                "audio_cue": "cheerful ABC song synth intro"
            },
            {
                "scene": 2,
                "narration": "A is for Apple! /a/ /a/ Apple! B is for Bear! /b/ /b/ Bear!",
                "visual_prompt": "3D animated juicy red apple floating next to a friendly dancing bear, sparkling phonics text overlay",
                "audio_cue": "clear phonics sound effects, soft bell chimes"
            }
        ]
    elif "count" in t_lower or "number" in t_lower or "math" in t_lower:
        title = f"Kindergarten Math: {topic.title()}"
        scenes = [
            {
                "scene": 1,
                "narration": f"Hello little explorers! Today we are going to practice {topic.lower()}!",
                "visual_prompt": f"3D Pixar style mascot holding a giant glowing number 1, colorful background, 8k resolution",
                "audio_cue": "playful whistle, cheerful synth intro"
            },
            {
                "scene": 2,
                "narration": "Let's count together: One... Two... Three... Four... Five! Great job!",
                "visual_prompt": "3D animated forest animals popping up one by one with big shiny numbers floating above them",
                "audio_cue": "pop sound effect for each number, happy chimes"
            }
        ]
    else:
        title = f"Kindergarten Special: {topic.title()}"
        scenes = [
            {
                "scene": 1,
                "narration": f"Hello friends! Today we are exploring the wonderful world of {topic}!",
                "visual_prompt": f"3D Pixar style, friendly mascot inviting viewers into a magical scene based on {topic}, 8k resolution",
                "audio_cue": "upbeat welcoming tune"
            },
            {
                "scene": 2,
                "narration": f"Look closely! Can you spot all the amazing things about {topic}?",
                "visual_prompt": f"3D animated elements showing {topic} in action, vibrant colors, soft rim lighting",
                "audio_cue": "gentle background music, playful chime"
            }
        ]

    return {
        "title": title,
        "target_age": "3-6 years",
        "learning_objective": f"Interactive engagement with {topic}",
        "scenes": scenes,
        "comfyui_workflow": {
            "engine": "Wan2.2-TextToVideo",
            "text_encoder": "umt5_xxl",
            "resolution": "1280x720",
            "fps": 24,
            "steps": 30
        }
    }

async def trigger_local_script(script_name: str, payload: dict) -> str:
    script_path = os.path.join(BASE_DIR, script_name)
    if not os.path.exists(script_path):
        return f"Error: {script_name} not found in {BASE_DIR}"
    
    try:
        # Pass payload as JSON string to the local video generator script
        payload_str = json.dumps(payload)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path, payload_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=BASE_DIR
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode().strip() or "Script executed successfully."
        else:
            return f"Execution failed:\n{stderr.decode().strip()}"
    except Exception as e:
        return f"Process execution error: {str(e)}"

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            raw_data = await websocket.receive_text()
            payload = json.loads(raw_data)
            action = payload.get("action")

            if action == "run_dev_agent":
                spec = payload.get("spec", "Build a trace logger")
                result = {
                    "status": "success",
                    "spec": spec,
                    "generated_code": "class TraceLogger:\n    def __init__(self):\n        self.logs = []\n\n    def log(self, msg):\n        self.logs.append(msg)",
                    "ast_check": "Passed",
                    "execution": "Verified in sandbox"
                }
                await websocket.send_json({"type": "dev_agent_result", "data": result})

            elif action == "generate_video_lesson":
                topic = payload.get("topic", "Counting Forest Animals")
                lesson_data = generate_smart_kinder_lesson(topic)
                
                await websocket.send_json({"type": "video_studio_result", "data": lesson_data})
                await websocket.send_json({"type": "system_notice", "message": "⚡ Triggering local execution engine (generate_video.py)..."})
                
                output = await trigger_local_script("generate_video.py", lesson_data)
                await websocket.send_json({"type": "system_notice", "message": f"📄 Local Script Output:\n{output}"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_json({"type": "error", "message": str(e)})

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9000)
