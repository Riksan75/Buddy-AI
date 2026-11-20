"""
Buddy OS — Observability
========================
Tracing, token/cost logging, decision audit trail, and performance metrics.
"""

import json
import time
import threading
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("buddy_os.observability")


@dataclass
class TraceEvent:
    """Single trace event in a task execution."""
    timestamp: str
    event_type: str  # 'llm_call', 'tool_call', 'code_exec', 'skill_exec', 'decision', 'error'
    name: str
    duration_ms: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    trace_id: str = ""


@dataclass
class LLMMemory:
    """LLM call memory for cost tracking."""
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    timestamp: str
    tier: str  # 'local', 'cloud_cheap', 'cloud_best'
    success: bool


class Observability:
    """Central observability for Buddy OS."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.trace_file = self.workspace / "traces.jsonl"
        self.cost_file = self.workspace / "costs.jsonl"
        self.metrics_file = self.workspace / "metrics.json"

        self._trace_buffer: deque = deque(maxlen=1000)
        self._cost_buffer: List[LLMMemory] = []
        self._lock = threading.Lock()
        self._current_trace_id: Optional[str] = None
        self._event_counter = 0

        # Token pricing (approximate, per 1M tokens)
        self.pricing = {
            'local': 0.0,
            'qwen2.5-coder:7b': 0.0,
            'qwen2.5-coder:32b': 0.0,
            'llama3.1:70b': 0.0,
            'gpt-4o': 5.0,  # $5/1M input, $15/1M output
            'gpt-4o-mini': 0.15,
            'claude-3.5-sonnet': 3.0,
            'claude-3.5-haiku': 0.25,
            'gemini-1.5-pro': 1.25,
            'gemini-1.5-flash': 0.075,
        }

        self._load_metrics()

    def _load_metrics(self):
        if self.metrics_file.exists():
            try:
                data = json.loads(self.metrics_file.read_text(encoding='utf-8'))
                self.total_tokens = data.get('total_tokens', 0)
                self.total_cost_usd = data.get('total_cost_usd', 0.0)
                self.task_count = data.get('task_count', 0)
                self.success_count = data.get('success_count', 0)
                self.error_count = data.get('error_count', 0)
            except Exception:
                self._reset_metrics()
        else:
            self._reset_metrics()

    def _reset_metrics(self):
        self.total_tokens = 0
        self.total_cost_usd = 0.0
        self.task_count = 0
        self.success_count = 0
        self.error_count = 0

    def _save_metrics(self):
        try:
            data = {
                'total_tokens': self.total_tokens,
                'total_cost_usd': self.total_cost_usd,
                'task_count': self.task_count,
                'success_count': self.success_count,
                'error_count': self.error_count,
                'updated_at': datetime.now().isoformat()
            }
            self.metrics_file.write_text(json.dumps(data, indent=2), encoding='utf-8')
        except Exception as e:
            logger.warning(f"Failed to save metrics: {e}")

    def new_trace(self, task_name: str = "task") -> str:
        """Start a new trace context."""
        self._current_trace_id = f"{task_name}_{int(time.time() * 1000)}"
        self._event_counter = 0
        return self._current_trace_id

    @contextmanager
    def trace(self, event_type: str, name: str, metadata: Dict[str, Any] = None):
        """Context manager for tracing an operation."""
        trace_id = self._current_trace_id or self.new_trace("auto")
        event_id = f"{trace_id}_e{self._event_counter}"
        self._event_counter += 1

        start = time.perf_counter()
        metadata = metadata or {}

        try:
            yield
            duration_ms = (time.perf_counter() - start) * 1000
            self._record_event(TraceEvent(
                timestamp=datetime.now().isoformat(timespec='milliseconds'),
                event_type=event_type,
                name=name,
                duration_ms=duration_ms,
                metadata={**metadata, 'status': 'success'},
                parent_id=None,
                trace_id=trace_id
            ))
        except Exception as e:
            duration_ms = (time.perf_counter() - start) * 1000
            self._record_event(TraceEvent(
                timestamp=datetime.now().isoformat(timespec='milliseconds'),
                event_type=event_type,
                name=name,
                duration_ms=duration_ms,
                metadata={**metadata, 'status': 'error', 'error': str(e)},
                parent_id=None,
                trace_id=trace_id
            ))
            raise

    def _record_event(self, event: TraceEvent):
        with self._lock:
            self._trace_buffer.append(event)
            # Write to file
            try:
                with open(self.trace_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(asdict(event)) + '\n')
            except Exception:
                pass

    def record_llm_call(self, model: str, prompt_tokens: int, completion_tokens: int,
                       tier: str = 'local', success: bool = True):
        """Record an LLM call for cost tracking."""
        total = prompt_tokens + completion_tokens
        price_per_m = self.pricing.get(model, self.pricing.get(tier, 0.0))
        # Rough estimate: blend input/output pricing
        cost = (total / 1_000_000) * price_per_m

        mem = LLMMemory(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            estimated_cost_usd=cost,
            timestamp=datetime.now().isoformat(timespec='seconds'),
            tier=tier,
            success=success
        )

        with self._lock:
            self._cost_buffer.append(mem)
            self.total_tokens += total
            self.total_cost_usd += cost
            self._save_metrics()

            # Write to file
            try:
                with open(self.cost_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(asdict(mem)) + '\n')
            except Exception:
                pass

    def record_task(self, success: bool = True):
        """Record task completion."""
        with self._lock:
            self.task_count += 1
            if success:
                self.success_count += 1
            else:
                self.error_count += 1
            self._save_metrics()

    def get_trace_summary(self, trace_id: str = None) -> Dict[str, Any]:
        """Get summary of traces."""
        with self._lock:
            events = [e for e in self._trace_buffer if trace_id is None or e.trace_id == trace_id]

        if not events:
            return {'events': 0}

        by_type = {}
        total_ms = 0
        for e in events:
            by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
            total_ms += e.duration_ms

        return {
            'trace_id': trace_id or 'all',
            'events': len(events),
            'total_duration_ms': round(total_ms, 1),
            'by_type': by_type,
            'events_detail': [asdict(e) for e in events[-50:]]  # Last 50
        }

    def get_cost_summary(self) -> Dict[str, Any]:
        """Get cost summary."""
        with self._lock:
            recent = self._cost_buffer[-100:]

        by_model = {}
        by_tier = {}
        for m in recent:
            by_model[m.model] = by_model.get(m.model, 0) + m.total_tokens
            by_tier[m.tier] = by_tier.get(m.tier, 0) + m.total_tokens

        return {
            'total_tokens': self.total_tokens,
            'total_cost_usd': round(self.total_cost_usd, 4),
            'total_calls': len(self._cost_buffer),
            'recent_tokens': sum(m.total_tokens for m in recent),
            'recent_cost_usd': round(sum(m.estimated_cost_usd for m in recent), 4),
            'by_model': by_model,
            'by_tier': by_tier,
            'task_count': self.task_count,
            'success_rate': round(self.success_count / max(1, self.task_count), 3)
        }

    def get_audit_trail(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent audit trail."""
        with self._lock:
            return [asdict(e) for e in list(self._trace_buffer)[-limit:]]

    def export_traces(self, output_file: Path = None) -> Path:
        """Export all traces to a file."""
        out = output_file or self.workspace / f"traces_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with self._lock:
            events = list(self._trace_buffer)
        out.write_text(json.dumps([asdict(e) for e in events], indent=2), encoding='utf-8')
        return out


# Global instance holder
_observability: Optional[Observability] = None


def get_observability(workspace: Path = None) -> Observability:
    """Get or create global observability instance."""
    global _observability
    if _observability is None and workspace:
        _observability = Observability(workspace)
    return _observability


def init_observability(workspace: Path) -> Observability:
    """Initialize global observability."""
    global _observability
    _observability = Observability(workspace)
    return _observability


def add_observability_to_engine(engine):
    """Add observability to AutonomousEngine."""
    engine.observability = init_observability(engine.workspace)
    return engine


if __name__ == "__main__":
    # Quick test
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        obs = Observability(Path(tmp))
        trace_id = obs.new_trace("test_task")

        with obs.trace("llm_call", "generate_code", {"model": "qwen2.5-coder:7b", "tokens": 100}):
            time.sleep(0.01)

        with obs.trace("code_exec", "run_script", {"file": "test.py"}):
            time.sleep(0.005)

        obs.record_llm_call("qwen2.5-coder:7b", 100, 50, tier="local")
        obs.record_task(success=True)

        print("Trace summary:", json.dumps(obs.get_trace_summary(trace_id), indent=2))
        print("Cost summary:", json.dumps(obs.get_cost_summary(), indent=2))