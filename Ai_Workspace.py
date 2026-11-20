from pathlib import Path
import ollama

# Define your secure workspace folder
WORKSPACE_DIR = (Path(__file__).resolve().parent.parent / "AI_Workspace").resolve()

def verify_path(file_path: str) -> Path:
    """Ensure the model cannot escape the target workspace folder."""
    target = (WORKSPACE_DIR / file_path).resolve()
    if not target.is_relative_to(WORKSPACE_DIR):
        raise PermissionError("Access denied: Outside workspace bounds.")
    return target

# Skill: Read File
def tool_read_file(file_path: str) -> str:
    path = verify_path(file_path)
    return path.read_text(encoding="utf-8")

# Skill: Write/Save File
def tool_save_file(file_path: str, content: str):
    path = verify_path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Successfully saved to {file_path}"
