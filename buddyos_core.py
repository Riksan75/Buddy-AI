# Buddy OS v4: core/buddyos_core.py
import os
import sys

class BuddyOSCore:
    def __init__(self, workspace_path: str = "."):
        self.workspace_path = os.path.abspath(workspace_path)
        self.agents = {}

    def register_agent(self, name: str, agent_instance):
        self.agents[name] = agent_instance

    def run(self, agent_name: str, task: str):
        agent = self.agents.get(agent_name)
        if not agent:
            return {"status": "ERROR", "message": f"Agent {agent_name} not registered."}
        
        if hasattr(agent, "perform"):
            return agent.perform(task)
        elif hasattr(agent, "execute"):
            return agent.execute(task)
        return {"status": "ERROR", "message": f"Agent {agent_name} has no executable interface."}

if __name__ == "__main__":
    print("Buddy OS v4 Core initialized with unrestricted environment.")
