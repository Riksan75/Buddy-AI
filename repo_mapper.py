import os

class RepoMapper:
    @staticmethod
    def generate_map(start_path=".") -> str:
        """Generates a structural map of the workspace directory for the agent."""
        tree_str = "[Aider-Style Repository Map]\n"
        ignore_dirs = {".git", "__pycache__", "venv", ".venv", "node_modules"}
        
        for root, dirs, files in os.walk(start_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            level = root.replace(start_path, "").count(os.sep)
            if level > 3:  # Depth limit
                continue
            indent = " " * 4 * level
            tree_str += f"{indent}{os.path.basename(root)}/\n"
            sub_indent = " " * 4 * (level + 1)
            for file in files:
                if file.endswith((".py", ".md", ".json", ".env")):
                    tree_str += f"{sub_indent}{file}\n"
                    
        return tree_str
