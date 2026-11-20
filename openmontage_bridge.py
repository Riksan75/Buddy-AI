import subprocess
import os
from pathlib import Path

# Paths to native sub-repository
SKILL_DIR = Path(__file__).parent.parent.parent / "skills" / "OpenMontage"
VENV_PYTHON = SKILL_DIR / ".venv" / "Scripts" / "python.exe"

def run_openmontage_pipeline(prompt: str, pipeline_type: str = "animated_explainer"):
    """
    Executes an OpenMontage pipeline using its local virtual environment.
    """
    if not VENV_PYTHON.exists():
        raise FileNotFoundError(f"OpenMontage venv not found at {VENV_PYTHON}")

    # Pass command directly to OpenMontage runner script
    cmd = [
        str(VENV_PYTHON),
        "main.py",
        "--pipeline", pipeline_type,
        "--prompt", prompt
    ]

    print(f"[Buddy OS] Triggering OpenMontage: {prompt}")
    result = subprocess.run(cmd, cwd=str(SKILL_DIR), capture_output=True, text=True)
    
    if result.returncode == 0:
        print("[Buddy OS] Render complete!")
        return result.stdout
    else:
        print(f"[Buddy OS Error]: {result.stderr}")
        return None
