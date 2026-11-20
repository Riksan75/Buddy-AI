import os
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SERVICES_DIR = BASE_DIR / "services"

SERVICES = {
    "openmontage": {
        "dir": SERVICES_DIR / "OpenMontage",
        "python": SERVICES_DIR / "OpenMontage" / ".venv" / "Scripts" / "python.exe"
    },
    "comfyui": {
        "dir": SERVICES_DIR / "ComfyUI",
        "python": SERVICES_DIR / "ComfyUI" / ".venv" / "Scripts" / "python.exe",
        "main": "main.py --listen 127.0.0.1 --port 8188"
    },
    "voicebox": {
        "dir": SERVICES_DIR / "Voicebox"
    }
}

def check_services_status():
    """Report directory presence only; this does not prove service health."""
    status = {}
    for name, config in SERVICES.items():
        status[name] = config["dir"].exists()
    return status

if __name__ == "__main__":
    print("Buddy OS Services Status:", check_services_status())
