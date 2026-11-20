"""
Buddy OS Advanced Memory Subsystem
Trace-based semantic recall with Chroma + SQLite + Prefrontal Scheduler.
"""
import json
import os
import uuid
import threading
from datetime import datetime
from typing import List, Optional
from pathlib import Path
from collections import deque

import sqlite3
import chromadb
from sentence_transformers import SentenceTransformer


class Trace:
    def __init__(self, trace_id, type_, content, salience, created,
                 last_accessed, access_count, consolidated, source_session, associations,
                 embedding=None):
        self.trace_id = trace_id
        self.type = type_
        self.content = content
        self.embedding = embedding
        self.salience = salience
        self.created = created
        self.last_accessed = last_accessed
        self.access_count = access_count
        self.consolidated = consolidated
        self.source_session = source_session
        self.associations = associations


class PrefrontalScheduler:
    """Ranks memory traces by similarity × recency × salience × graph activation."""
    def __init__(self, working_set_size: int = 16000):
        self.working_set_size = working_set_size

    def schedule(self, candidates: List[Trace], query_embedding: List[float],
                 active_trace_ids: List[str] = None):
        if not candidates:
            return [], 0

        scored = []
        for trace in candidates:
            sim = self._cosine_similarity(trace.embedding, query_embedding)
            recency = self._calc_recency(trace.created)
            graph = 1.0 if active_trace_ids and trace.trace_id in active_trace_ids else 0.5
            score = (0.45 * sim + 0.25 * recency + 0.20 * trace.salience + 0.10 * graph)
            scored.append((score, trace))

        scored.sort(key=lambda x: x[0], reverse=True)

        selected, total_tokens = [], 0
        budget = int(self.working_set_size * 0.9)
        for _, trace in scored:
            tokens = max(1, len(trace.content) // 4)
            if total_tokens + tokens <= budget:
                selected.append(trace)
                total_tokens += tokens
            else:
                break
        return selected, total_tokens

    @staticmethod
    def _cosine_similarity(a, b):
        if not a or not b:
            return 0.0
        dot, na, nb = 0.0, 0.0, 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
        if na == 0 or nb == 0:
            return 0.0
        return max(0.0, min(1.0, dot / ((na**0.5) * (nb**0.5))))

    @staticmethod
    def _calc_recency(created_str):
        created = datetime.fromisoformat(created_str)
        hours = (datetime.utcnow() - created).total_seconds() / 3600
        return max(0.1, 1.0 / (1 + hours * 0.15))


class AdvancedMemory:
    """
    Semantic long-term memory: every message is embedded, stored, and retrievable.
    """
    def __init__(self, workspace_dir: str = "./workspace", session_id: str = "default",
                 consolidation_interval: int = 300, enable_consolidation: bool = True):
        os.makedirs(workspace_dir, exist_ok=True)
        self.workspace = workspace_dir
        self.session_id = session_id
        self.encoder = SentenceTransformer('all-MiniLM-L6-v2')

        self.vector_db = chromadb.PersistentClient(path=f"{workspace_dir}/chroma")
        self.collection = self.vector_db.get_or_create_collection("traces")

        self.db_path = f"{workspace_dir}/cortex.sqlite"
        self._init_sqlite()

        self.stm = deque(maxlen=50)
        self.scheduler = PrefrontalScheduler()

        self._consolidation_interval = consolidation_interval
        self._enable_consolidation = enable_consolidation
        self._consolidation_timer = None
        if enable_consolidation:
            self._start_consolidation_daemon()

    def _init_sqlite(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY,
                type TEXT,
                content TEXT,
                salience REAL,
                created TEXT,
                last_accessed TEXT,
                access_count INTEGER,
                consolidated INTEGER,
                source_session TEXT,
                associations TEXT
            )
        """)
        conn.commit()
        conn.close()

    def perceive(self, type_: str, content: str, source_session: str = None):
        """
        Store an event as a memory trace.
        type_: 'episodic' | 'semantic' | 'procedural' | 'emotional'
        """
        session = source_session or self.session_id
        embedding = self.encoder.encode(content).tolist()
        trace = Trace(
            trace_id=str(uuid.uuid4()),
            type_=type_,
            content=content,
            embedding=embedding,
            salience=1.0,
            created=datetime.utcnow().isoformat(),
            last_accessed=datetime.utcnow().isoformat(),
            access_count=1,
            consolidated=0,
            source_session=session,
            associations=[]
        )
        self.stm.append(trace)

        self.collection.add(
            ids=[trace.trace_id],
            embeddings=[trace.embedding],
            metadatas=[{"type": trace.type, "content": trace.content[:100]}]
        )

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO traces VALUES (?,?,?,?,?,?,?,?,?,?)",
            (trace.trace_id, trace.type, trace.content, trace.salience,
             trace.created, trace.last_accessed, trace.access_count,
             0, trace.source_session, json.dumps(trace.associations))
        )
        conn.commit()
        conn.close()
        return trace

    def recall(self, query: str, k: int = 20) -> List[Trace]:
        """Retrieve and rank the most relevant traces for the current query."""
        query_embedding = self.encoder.encode(query).tolist()
        results = self.collection.query(query_embeddings=[query_embedding], n_results=k*2)
        ids = results['ids'][0] if results and results['ids'] else []

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        traces = []
        for trace_id in ids:
            cursor.execute("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
            row = cursor.fetchone()
            if row:
                traces.append(Trace(
                    trace_id=row[0], type_=row[1], content=row[2],
                    salience=row[3], created=row[4], last_accessed=row[5],
                    access_count=row[6], consolidated=bool(row[7]),
                    source_session=row[8], associations=json.loads(row[9])
                ))
        conn.close()

        stm_traces = list(self.stm)
        traces.extend(stm_traces)
        active_ids = [t.trace_id for t in stm_traces]
        selected, _ = self.scheduler.schedule(traces, query_embedding, active_ids)
        return selected

    @staticmethod
    def format_traces(traces: List[Trace]) -> str:
        """Format traces for prompt injection."""
        if not traces:
            return "(No specific past memories retrieved.)"
        lines = []
        for t in traces:
            lines.append(f"[{t.type}][{t.created[:10]}] {t.content}")
        return "\n".join(lines)

    def consolidate(self):
        """Background daemon: decay salience, garbage collect old traces."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE traces SET salience = salience * 0.95 WHERE julianday('now') - julianday(created) > 1")
        conn.execute("DELETE FROM traces WHERE salience < 0.1")
        conn.commit()
        conn.close()

    def _start_consolidation_daemon(self):
        def _loop():
            self.consolidate()
            if self._enable_consolidation:
                self._consolidation_timer = threading.Timer(self._consolidation_interval, _loop)
                self._consolidation_timer.daemon = True
                self._consolidation_timer.start()

        self._consolidation_timer = threading.Timer(self._consolidation_interval, _loop)
        self._consolidation_timer.daemon = True
        self._consolidation_timer.start()

    def shutdown(self):
        """Graceful cleanup: stop daemon, run final consolidation."""
        self._enable_consolidation = False
        if self._consolidation_timer:
            self._consolidation_timer.cancel()
        self.consolidate()
