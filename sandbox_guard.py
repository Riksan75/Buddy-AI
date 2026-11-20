import ast
import os
import shutil
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

class GuardedSandbox:
    """Not a cage. A safety harness with rollback."""
    
    def __init__(self, root: str = "workspace"):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir = self.root / ".snapshots"
        self.snapshots_dir.mkdir(exist_ok=True)
        
        # ALLOWED for autonomy (this is the KEY change from Strict)
        self.allowed_cmds = {"git", "pip", "npm", "python", "ffmpeg", "curl", "wget"}
        
        # BLOCKED: only truly destructive commands
        self.blocked_patterns = [
            "rm -rf /", "rm -rf /*", "mkfs.", ":(){:|:&};:", 
            "dd if=/dev/zero", "> /dev/sda", "format c:", "shutdown", "reboot", "halt"
        ]
        self.blocked_modules = {"ctypes"}
        
    def scan(self, code: str, mission_id: str) -> Dict:
        """Scan code. Block destruction. Allow creation."""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return {"allowed": False, "error": f"Syntax error: {e}"}
        
        violations = []
        warnings = []
        has_mutation = False
        
        for node in ast.walk(tree):
            # Check imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in self.blocked_modules:
                        violations.append(f"Blocked module: {alias.name}")
            
            if isinstance(node, ast.ImportFrom):
                if node.module in self.blocked_modules:
                    violations.append(f"Blocked module: {node.module}")
            
            # Check calls
            if isinstance(node, ast.Call):
                func_name = self._get_func_name(node.func)
                
                # Block eval/exec on dynamic input
                if func_name in ("eval", "exec"):
                    if not self._is_safe_literal(node.args[0] if node.args else None):
                        violations.append(f"Dynamic {func_name}() blocked")
                
                # Check subprocess / os.system
                if func_name in ("os.system", "subprocess.call", "subprocess.run", "subprocess.Popen"):
                    has_mutation = True
                    cmd = self._extract_first_arg(node)
                    if cmd:
                        cmd_str = str(cmd).lower()
                        for pattern in self.blocked_patterns:
                            if pattern.lower() in cmd_str:
                                violations.append(f"DESTRUCTIVE COMMAND BLOCKED: {cmd[:60]}")
                        cmd_base = cmd_str.split()[0] if cmd_str else ""
                        if cmd_base and cmd_base not in self.allowed_cmds:
                            warnings.append(f"Untrusted command: {cmd_base}")
                
                # File mutations
                if func_name in ("open", "os.remove", "shutil.rmtree", "os.mkdir", "os.makedirs"):
                    has_mutation = True
                    path = self._extract_first_arg(node)
                    if path and not self._is_in_sandbox(path):
                        violations.append(f"File escape attempt: {path}")
        
        # Snapshot before mutation
        if has_mutation and not violations:
            self._snapshot(mission_id)
        
        return {
            "allowed": len(violations) == 0,
            "violations": violations,
            "warnings": warnings,
            "sandbox": str(self.root),
            "snapshot": mission_id if has_mutation else None
        }
    
    def _snapshot(self, mission_id: str):
        snap_path = self.snapshots_dir / f"{mission_id}_{datetime.now().strftime('%H%M%S')}"
        if self.root.exists():
            shutil.copytree(self.root, snap_path, ignore=shutil.ignore_patterns(".snapshots"), dirs_exist_ok=True)
    
    def rollback(self, mission_id: str) -> bool:
        snaps = sorted(self.snapshots_dir.glob(f"{mission_id}_*"), key=lambda p: p.stat().st_mtime)
        if snaps:
            latest = snaps[-1]
            for item in self.root.iterdir():
                if item.name == ".snapshots":
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            for item in latest.iterdir():
                if item.name == ".snapshots":
                    continue
                dest = self.root / item.name
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            return True
        return False
    
    def _get_func_name(self, node) -> str:
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_func_name(node.value)}.{node.attr}"
        return ""
    
    def _extract_first_arg(self, node: ast.Call):
        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
        return ""
    
    def _is_safe_literal(self, node):
        return isinstance(node, (ast.Constant, ast.List, ast.Dict, ast.Tuple))
    
    def _is_in_sandbox(self, path: str) -> bool:
        try:
            resolved = (self.root / path).resolve()
            return str(resolved).startswith(str(self.root))
        except:
            return False
