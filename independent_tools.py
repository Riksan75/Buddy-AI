import os
import subprocess

def read_local_file(file_path: str) -> str:
    """Reads any file on the local machine."""
    try:
        # Clean path relative to workspace or absolute
        if not os.path.isabs(file_path):
            file_path = os.path.join(os.getcwd(), file_path)
            
        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"
            
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error reading file: {str(e)}"

def run_terminal_command(command: str) -> str:
    """Executes a terminal command locally and returns output."""
    try:
        result = subprocess.run(
            command, 
            shell=True, 
            capture_output=True, 
            text=True, 
            cwd=os.getcwd(),
            timeout=30
        )
        output = result.stdout
        if result.stderr:
            output += f"\nErrors/Warnings:\n{result.stderr}"
        return output if output.strip() else "Command executed successfully with no output."
    except Exception as e:
        return f"Error executing command: {str(e)}"

def list_directory(folder_path: str = ".") -> str:
    """Lists files and folders in a specified directory."""
    try:
        if not os.path.exists(folder_path):
            return f"Error: Directory not found at {folder_path}"
        items = os.listdir(folder_path)
        return f"Contents of {folder_path}:\n" + "\n".join(items)
    except Exception as e:
        return f"Error listing directory: {str(e)}"
