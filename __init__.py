"""core/ — Buddy OS Core Intelligence Package"""

__all__ = [
    "Eva",
    "ReasoningRouter",
    "ModelGenerationError",
    "EvaSkillRegistry",
    "EvaSkill",
    "get_registry",
    "EvaContextManager",
    "SessionBudget",
    "EvaToolkit",
    "ToolResult",
    "EvaMetacognition",
    "EvaMemory",
    "get_memory",
    "SelfExecutionLoop",
]

def __getattr__(name: str):
    if name == "Eva":
        from core.eva_core import Eva as _Eva
        return _Eva
    if name == "EvaMetacognition":
        from core.eva_metacognition import EvaMetacognition as _EM
        return _EM
    if name == "EvaToolkit":
        from core.eva_toolkit import EvaToolkit as _ET
        return _ET
    if name == "EvaSkillRegistry":
        from core.eva_skills_registry import EvaSkillRegistry as _ESR
        return _ESR
    if name == "EvaSkill":
        from core.eva_skills_registry import EvaSkill as _ES
        return _ES
    if name == "get_registry":
        from core.eva_skills_registry import get_registry as _gr
        return _gr
    if name == "EvaContextManager":
        from core.eva_context_manager import EvaContextManager as _ECM
        return _ECM
    if name == "SessionBudget":
        from core.eva_context_manager import SessionBudget as _SB
        return _SB
    if name == "ReasoningRouter":
        from core.reasoning_router import ReasoningRouter as _RR
        return _RR
    if name == "ModelGenerationError":
        from core.reasoning_router import ModelGenerationError as _MGE
        return _MGE
    if name == "ToolResult":
        from core.eva_toolkit import ToolResult as _TR
        return _TR
    if name == "EvaMemory":
        from core.eva_memory import EvaMemory as _EMem
        return _EMem
    if name == "get_memory":
        from core.eva_memory import get_memory as _gm
        return _gm
    if name == "SelfExecutionLoop":
        from core.eva_execution_loop import SelfExecutionLoop as _SEL
        return _SEL
    raise AttributeError(f"module 'core' has no attribute '{name}'")
