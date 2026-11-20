import subprocess
import sys
from pathlib import Path
import ollama

# Define secure workspace folder
WORKSPACE_DIR = Path("./AI_Workspace").resolve()
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

def verify_path(file_path: str) -> Path:
    """Ensure the model cannot escape the target workspace folder."""
    target = (WORKSPACE_DIR / file_path).resolve()
    if not str(target).startswith(str(WORKSPACE_DIR)):
        raise PermissionError("Access denied: Outside workspace bounds.")
    return target

# --- File & Execution Skills ---
def tool_read_file(file_path: str) -> str:
    path = verify_path(file_path)
    if not path.exists():
        return f"Error: File '{file_path}' does not exist."
    return path.read_text(encoding="utf-8")

def tool_save_file(file_path: str, content: str) -> str:
    path = verify_path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"Successfully saved to {file_path}"

def tool_list_files() -> str:
    files = [str(f.relative_to(WORKSPACE_DIR)) for f in WORKSPACE_DIR.rglob("*") if f.is_file()]
    return f"Files in workspace: {files}" if files else "Workspace is currently empty."

def tool_run_python(file_path: str) -> str:
    path = verify_path(file_path)
    if not path.exists():
        return f"Error: File '{file_path}' does not exist."
    try:
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(WORKSPACE_DIR)
        )
        output = f"--- STDOUT ---\n{result.stdout}\n"
        if result.stderr:
            output += f"--- STDERR ---\n{result.stderr}\n"
        output += f"Exit Code: {result.returncode}"
        return output
    except Exception as e:
        return f"Execution Error: {str(e)}"

# Map tools for dispatch
AVAILABLE_TOOLS = {
    "read_file": tool_read_file,
    "save_file": tool_save_file,
    "list_files": tool_list_files,
    "run_python": tool_run_python
}

# --- Ollama Agent Loop ---
def run_agent(prompt: str, model_name: str = "yefx/minicpm3_4b"):
    messages = [{"role": "user", "content": prompt}]
    
    # Define tool schemas for Ollama
    tools = [
        {
            "type": "function",
            "function": {
                "name": "save_file",
                "description": "Create or save content to a file in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Name or relative path of the file"},
                        "content": {"type": "string", "description": "Text content to write"}
                    },
                    "required": ["file_path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_python",
                "description": "Execute a python script in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Python script file to execute"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List all files in the workspace.",
                "parameters": {"type": "object", "properties": {}}
            }
        }
    ]

    print(f"Sending prompt to model {model_name}...")
    response = ollama.chat(model=model_name, messages=messages, tools=tools)
    
    if response.get('message', {}).get('tool_calls'):
        for tool in response['message']['tool_calls']:
            func_name = tool['function']['name']
            func_args = tool['function']['arguments']
            print(f"\n[Model requested tool: {func_name} with args {func_args}]")
            
            if func_name in AVAILABLE_TOOLS:
                result = AVAILABLE_TOOLS[func_name](**func_args)
                print(f"[Tool Output]:\n{result}")
            else:
                print(f"Error: Unknown tool {func_name}")
    else:
        print("\n[Model Response]:", response['message']['content'])

if __name__ == "__main__":
    # Test prompting the model to build and run a script
    run_agent("Create a python script named calc.py that prints 5 plus 5, then run it.")