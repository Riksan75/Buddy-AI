# Buddy OS v4: skills/file_handler.py
import os

class FileHandlerSkill:
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path

    def safe_write(self, relative_path: str, content: str):
        full_path = os.path.join(self.workspace_path, relative_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote to {full_path}"
