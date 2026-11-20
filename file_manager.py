# Buddy OS v4: core/file_manager.py
import os
import shutil

class UnrestrictedFileManager:
    def __init__(self, workspace_path: str = "."):
        self.workspace_path = os.path.abspath(workspace_path)

    def write_file(self, path: str, content: str):
        full_path = os.path.abspath(path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "SUCCESS", "path": full_path, "message": f"Successfully wrote to {full_path}"}

    def read_file(self, path: str):
        full_path = os.path.abspath(path)
        if not os.path.exists(full_path):
            return {"status": "ERROR", "message": f"File not found: {full_path}"}
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"status": "SUCCESS", "path": full_path, "content": content}
