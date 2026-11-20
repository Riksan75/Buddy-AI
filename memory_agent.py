# Buddy OS v4: core/memory_agent.py
import json
import os

class UnrestrictedMemoryAgent:
    def __init__(self, storage_path: str = "memory_store.json"):
        self.storage_path = os.path.abspath(storage_path)

    def save_memory(self, key: str, value: any):
        data = {}
        if os.path.exists(self.storage_path):
            with open(self.storage_path, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {}
        
        data[key] = value
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return {"status": "SUCCESS", "key": key}

    def load_memory(self, key: str):
        if not os.path.exists(self.storage_path):
            return {"status": "ERROR", "message": "Memory store not found."}
        with open(self.storage_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"status": "SUCCESS", "key": key, "value": data.get(key, None)}
