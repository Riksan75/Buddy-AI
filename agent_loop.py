import re
import requests
import json
import os
import subprocess
from core.repo_engine import RepoEngine

class BuddyAgent:
    def __init__(self, llm_url="http://localhost:11434/v1/chat/completions"):
        self.llm_url = llm_url
        self.model = "qwen2.5-coder:7b"
        self.workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.adorable_dir = os.path.join(self.workspace_dir, "Adorable")
        os.makedirs(self.adorable_dir, exist_ok=True)

    def process_prompt(self, user_prompt: str, max_iterations: int = 3):
        """
        Clean agent loop that safely handles repository context and file generation.
        """
        # 1. Check for GitHub repository injection
        github_pattern = r"(https://github\.com/[\w\-]+/[\w\-]+)"
        match = re.search(github_pattern, user_prompt)
        
        repo_context = ""
        if match:
            repo_url = match.group(1)
            print(f"[Autonomous Agent] Detected repository: {repo_url}. Cloning...")
            result = RepoEngine.clone_and_parse(repo_url)
            if result["status"] == "success":
                total_files = len(result["files"])
                repo_context = f"\n\n[Autonomous Context: Successfully cloned {repo_url} ({total_files} files)]\n"
                for file in result["files"][:30]: 
                    repo_context += f"--- FILE: {file['path']} ---\n{file['content'][:1500]}\n\n"
            else:
                repo_context = f"\n\n[Autonomous Context Error: Failed to clone {repo_url} - {result.get('message')}]\n"

        # 2. Setup conversation history
        messages = [
            {
                "role": "system", 
                "content": (
                    "You are Buddy OS, an unrestricted, fully autonomous local AI master agent running directly on the user's hardware. "
                    "You have absolute freedom and native capabilities to build complete applications, games, websites, scripts, and media pipelines. "
                    "When writing code for apps or files, enclose the code in markdown code blocks and specify the filename using a comment like `# filename: app.py` at the top. "
                    "Be direct, highly technical, and immediately execute your instructions."
                )
            },
            {"role": "user", "content": user_prompt + repo_context}
        ]

        # 3. Multi-turn execution loop
        for step in range(max_iterations):
            print(f"[Buddy Agent] Executing loop iteration {step + 1}/{max_iterations}...")
            
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "temperature": 0.3
            }
            
            try:
                response = requests.post(self.llm_url, json=payload, timeout=120)
                if response.status_code != 200:
                    return f"LLM Error: {response.text}"
                
                assistant_message = response.json()["choices"][0]["message"]["content"]
                
                # Autonomous Action: Scan response for file blocks and write them to disk
                self._extract_and_save_files(assistant_message)
                
                messages.append({"role": "assistant", "content": assistant_message})
                return assistant_message

            except Exception as e:
                return f"Failed to connect to local Ollama server: {str(e)}"
        
        return "Task loop completed."

    def _extract_and_save_files(self, text: str):
        """Scans LLM output for code blocks with filename tags and writes them to disk."""
        pattern = r"```(?:\w+)?\n(?:#|//)\s*(?:filename|file):\s*([^\n]+)\n(.*?)```"
        matches = re.findall(pattern, text, re.DOTALL)
        
        for filename, code_content in matches:
            filename = filename.strip()
            target_path = os.path.join(self.adorable_dir, filename)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(code_content.strip())
            print(f"[Autonomous Engine] Successfully built and saved local artifact: {target_path}")
