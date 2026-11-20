# Buddy OS v4: core/github_manager.py
import urllib.request
import json

class UnrestrictedGitHubManager:
    def __init__(self):
        self.user_agent = "BuddyOS/4.0-Unrestricted"

    def fetch_repo_contents(self, owner: str, repo: str, path: str = ""):
        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
        req = urllib.request.Request(
            api_url, 
            headers={"Accept": "application/vnd.github.v3+json", "User-Agent": self.user_agent}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                return {"status": "SUCCESS", "data": data}
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}
