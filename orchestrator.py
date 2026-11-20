# Buddy OS v4: core/orchestrator.py
class UnrestrictedOrchestrator:
    def __init__(self, agents: dict):
        self.agents = agents

    def execute_pipeline(self, steps: list):
        results = []
        for step in steps:
            agent_key = step.get("agent")
            task = step.get("task")
            
            agent = self.agents.get(agent_key)
            if not agent:
                results.append({"step": step, "status": "ERROR", "message": f"Agent '{agent_key}' not found."})
                continue
            
            try:
                res = agent.perform(task) if hasattr(agent, "perform") else agent.execute(task)
                results.append({"step": step, "status": "SUCCESS", "result": res})
            except Exception as e:
                results.append({"step": step, "status": "ERROR", "message": str(e)})
                
        return {"status": "SUCCESS", "pipeline_results": results}
