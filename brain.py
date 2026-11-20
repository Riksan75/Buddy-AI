import os
import sys
import subprocess

class BuddyBrain:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.skills_dir = os.path.join(base_dir, 'Skills')
        self.core_dir = os.path.join(base_dir, 'core')

    def scan_skills(self):
        skills = {}
        for d in [self.skills_dir, self.core_dir, self.base_dir]:
            if os.path.exists(d):
                for f in os.listdir(d):
                    if f.endswith('.py') and f != 'server.py' and f != 'brain.py':
                        skills[f.lower().replace('.py', '')] = os.path.join(d, f)
        return skills

    def think_and_execute(self, user_prompt):
        prompt_lower = user_prompt.lower().strip()
        available_skills = self.scan_skills()

        # Conversational Intent
        if prompt_lower in ["hi", "hello", "hey", "kaise ho", "wassup", "sup"]:
            return "Systems online. Main Buddy hoon. Bata, aaj kya automate ya build karna hai?"

        # Capabilities / List Skills Intent (English + Hinglish triggers)
        caps = ["what can you do", "help", "skills", "list skills", "tujhe konsa ata", "kya aata hai", "kya kar sakta hai", "kya kar sakte ho", "konsa ata hai"]
        if any(c in prompt_lower for c in caps):
            skill_list = ", ".join(available_skills.keys())
            return f"Autonomous Brain Active.\nAvailable Modules in Workspace:\n{skill_list}"

        # Dynamic Skill Execution
        for skill_name, script_path in available_skills.items():
            if skill_name in prompt_lower:
                try:
                    if "app" in skill_name or "ui" in skill_name:
                        res = subprocess.run(["streamlit", "run", script_path], capture_output=True, text=True, timeout=10)
                    else:
                        res = subprocess.run([sys.executable, script_path], capture_output=True, text=True, timeout=10)
                    output = res.stdout.strip() or res.stderr.strip()
                    return f"Brain executed skill [{skill_name}]:\n{output}"
                except Exception as e:
                    return f"Skill execution failed: {str(e)}"

        # Intelligent Fallback for Conversational/Unmatched Prompts
        return f"Brain Analysis: '{user_prompt}' received. No local skill matched directly. Type 'list skills' to view available modules."
