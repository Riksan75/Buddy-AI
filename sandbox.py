# Buddy OS v4: core/sandbox.py
import subprocess
import os

class UnrestrictedSandbox:
    def __init__(self, working_dir: str = "."):
        self.working_dir = os.path.abspath(working_dir)

    def run_python(self, code: str):
        temp_file = os.path.join(self.working_dir, "temp_exec.py")
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(code)
        
        try:
            result = subprocess.run(
                ["python", temp_file],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=self.working_dir
            )
            return {
                "status": "SUCCESS" if result.returncode == 0 else "ERROR",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode
            }
        except Exception as e:
            return {"status": "ERROR", "message": str(e)}
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)
