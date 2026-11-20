import json
import os
import heapq
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum
import threading


class Priority(Enum):
    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3
    BACKLOG = 4


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """A cognitive task in the system."""
    id: str
    title: str
    description: str
    priority: Priority
    status: TaskStatus
    created_at: str
    due_at: Optional[str]
    tags: List[str]
    dependencies: List[str]
    estimated_effort: int  # minutes
    actual_effort: int
    agent_assigned: Optional[str]
    context: Dict[str, Any]
    completion_criteria: str
    
    def to_dict(self):
        return {
            **asdict(self),
            "priority": self.priority.name,
            "status": self.status.value,
        }
    
    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["priority"] = Priority[d.get("priority", "MEDIUM")]
        d["status"] = TaskStatus(d.get("status", "pending"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class KnowledgeNode:
    """A node in the knowledge graph."""
    id: str
    content: str
    category: str
    source: str
    confidence: float
    created_at: str
    related_ids: List[str]
    access_count: int
    last_accessed: Optional[str]


class CognitiveAugmentation:
    """
    Cognitive augmentation system providing:
    - Task queue with priority and dependency management
    - Knowledge graph for information organization
    - Cognitive load tracking
    - Note-taking and context preservation
    """
    
    def __init__(self, data_dir: str = "./data/cognitive"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.tasks_file = os.path.join(data_dir, "tasks.json")
        self.knowledge_file = os.path.join(data_dir, "knowledge.json")
        self.notes_file = os.path.join(data_dir, "notes.jsonl")
        
        self._lock = threading.RLock()
        self.tasks: Dict[str, Task] = {}
        self.knowledge_graph: Dict[str, KnowledgeNode] = {}
        self._task_heap: List[tuple] = []  # (priority_value, created_at, task_id)
        
        self._load_data()
    
    def _load_data(self):
        if os.path.exists(self.tasks_file):
            try:
                with open(self.tasks_file, 'r') as f:
                    data = json.load(f)
                    for tid, td in data.items():
                        self.tasks[tid] = Task.from_dict(td)
                        self._heap_push(self.tasks[tid])
            except Exception:
                pass
        
        if os.path.exists(self.knowledge_file):
            try:
                with open(self.knowledge_file, 'r') as f:
                    data = json.load(f)
                    for kid, kd in data.items():
                        self.knowledge_graph[kid] = KnowledgeNode(**kd)
            except Exception:
                pass
    
    def _save_tasks(self):
        with self._lock:
            with open(self.tasks_file, 'w') as f:
                json.dump({k: v.to_dict() for k, v in self.tasks.items()}, f, indent=2)
    
    def _save_knowledge(self):
        with self._lock:
            with open(self.knowledge_file, 'w') as f:
                json.dump({k: asdict(v) for k, v in self.knowledge_graph.items()}, f, indent=2)
    
    def _heap_push(self, task: Task):
        heapq.heappush(self._task_heap, (task.priority.value, task.created_at, task.id))
    
    def create_task(self, title: str, description: str = "", 
                    priority: Priority = Priority.MEDIUM,
                    tags: List[str] = None, 
                    dependencies: List[str] = None,
                    estimated_effort: int = 30,
                    agent: str = None,
                    context: Dict = None) -> str:
        """Create a new task and return its ID."""
        task_id = str(uuid.uuid4())[:8]
        task = Task(
            id=task_id,
            title=title,
            description=description,
            priority=priority,
            status=TaskStatus.PENDING,
            created_at=datetime.now().isoformat(),
            due_at=None,
            tags=tags or [],
            dependencies=dependencies or [],
            estimated_effort=estimated_effort,
            actual_effort=0,
            agent_assigned=agent,
            context=context or {},
            completion_criteria=""
        )
        with self._lock:
            self.tasks[task_id] = task
            self._heap_push(task)
            self._save_tasks()
        return task_id
    
    def get_next_task(self) -> Optional[Task]:
        """Get the highest priority available task."""
        with self._lock:
            while self._task_heap:
                _, _, task_id = heapq.heappop(self._task_heap)
                if task_id in self.tasks:
                    task = self.tasks[task_id]
                    if task.status == TaskStatus.PENDING:
                        # Check dependencies
                        deps_satisfied = all(
                            self.tasks.get(did, Task("", "", "", Priority.LOW, TaskStatus.COMPLETED, "", None, [], [], 0, 0, None, {}, "")).status == TaskStatus.COMPLETED
                            for did in task.dependencies
                        )
                        if deps_satisfied:
                            task.status = TaskStatus.IN_PROGRESS
                            self._save_tasks()
                            return task
                        else:
                            task.status = TaskStatus.BLOCKED
            return None
    
    def complete_task(self, task_id: str, success: bool = True, notes: str = ""):
        """Mark a task as completed or failed."""
        with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id].status = TaskStatus.COMPLETED if success else TaskStatus.FAILED
                self._save_tasks()
                # Check if any blocked tasks can now proceed
                for task in self.tasks.values():
                    if task.status == TaskStatus.BLOCKED:
                        deps_ok = all(
                            self.tasks.get(did, Task("", "", "", Priority.LOW, TaskStatus.COMPLETED, "", None, [], [], 0, 0, None, {}, "")).status == TaskStatus.COMPLETED
                            for did in task.dependencies
                        )
                        if deps_ok:
                            task.status = TaskStatus.PENDING
                            self._heap_push(task)
                self._save_tasks()
    
    def add_knowledge(self, content: str, category: str = "general", 
                      source: str = "user", confidence: float = 0.8,
                      related: List[str] = None) -> str:
        """Add a node to the knowledge graph."""
        node_id = str(uuid.uuid4())[:8]
        node = KnowledgeNode(
            id=node_id,
            content=content,
            category=category,
            source=source,
            confidence=confidence,
            created_at=datetime.now().isoformat(),
            related_ids=related or [],
            access_count=0,
            last_accessed=None
        )
        with self._lock:
            self.knowledge_graph[node_id] = node
            self._save_knowledge()
        return node_id
    
    def search_knowledge(self, query: str, category: str = None, top_k: int = 5) -> List[KnowledgeNode]:
        """Search knowledge graph by content similarity (simple keyword match)."""
        query_lower = query.lower()
        results = []
        for node in self.knowledge_graph.values():
            if category and node.category != category:
                continue
            score = 0
            for word in query_lower.split():
                if word in node.content.lower():
                    score += 1
            if score > 0:
                results.append((score, node))
        
        results.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in results[:top_k]]
    
    def take_note(self, content: str, tags: List[str] = None, session_id: str = None):
        """Take a persistent note."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "content": content,
            "tags": tags or [],
            "session_id": session_id,
        }
        with self._lock:
            with open(self.notes_file, 'a') as f:
                f.write(json.dumps(entry) + "\\n")
    
    def get_cognitive_load(self) -> Dict:
        """Calculate current cognitive load metrics."""
        with self._lock:
            active = sum(1 for t in self.tasks.values() if t.status == TaskStatus.IN_PROGRESS)
            pending = sum(1 for t in self.tasks.values() if t.status == TaskStatus.PENDING)
            blocked = sum(1 for t in self.tasks.values() if t.status == TaskStatus.BLOCKED)
            
            total_effort = sum(t.estimated_effort for t in self.tasks.values() if t.status != TaskStatus.COMPLETED)
            
            return {
                "active_tasks": active,
                "pending_tasks": pending,
                "blocked_tasks": blocked,
                "total_outstanding_effort_min": total_effort,
                "cognitive_load_score": min(1.0, (active * 0.3 + pending * 0.1 + blocked * 0.2)),
                "recommendation": "delegate" if active > 3 else "proceed" if active > 0 else "idle",
            }
    
    def get_task_board(self) -> Dict[str, List[Dict]]:
        """Get tasks organized by status (Kanban-style)."""
        board = {"pending": [], "in_progress": [], "blocked": [], "completed": [], "failed": []}
        for task in sorted(self.tasks.values(), key=lambda t: t.priority.value):
            board[task.status.value].append(task.to_dict())
        return board
