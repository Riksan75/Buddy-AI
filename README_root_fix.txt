Buddy root-folder fix
=====================
Copy each file to the SAME place its old version lives (replace it):

  core\buddy_memory.py      core\media_hub.py       core\skill_bridge.py   core\skill_loader.py
  core\skill_telemetry.py   core\skill_forge.py     growth_engine.py       self_inspect.py
  (growth_engine.py and self_inspect.py: next to server.py, in the project root, unless yours are elsewhere)

Then close Buddy, run:
  python core\buddy_memory.py --where      ("In use" must be ...\buddy_osv5\knowledge_base\buddy_graph.db)
  python core\buddy_memory.py --recent     (must show your earlier conversations)
