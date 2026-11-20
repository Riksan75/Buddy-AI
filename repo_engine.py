import os
import tempfile
import git
import shutil

class RepoEngine:
    @staticmethod
    def clone_and_parse(repo_url: str):
        temp_dir = tempfile.mkdtemp()
        try:
            git.Repo.clone_from(repo_url, temp_dir)
            file_list = []
            
            # Walk through the cloned repository
            for root, dirs, files in os.walk(temp_dir):
                # Ignore hidden directories like .git
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for file in files:
                    if file.endswith(('.py', '.js', '.html', '.css', '.md', '.json', '.txt', '.cpp', '.h', '.java', '.go', '.rs')):
                        file_path = os.path.join(root, file)
                        rel_path = os.path.relpath(file_path, temp_dir)
                        try:
                            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                content = f.read()
                            file_list.append({"path": rel_path, "content": content})
                        except Exception:
                            pass
                            
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"status": "success", "files": file_list}
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"status": "error", "message": str(e)}
