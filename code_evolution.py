import ast
import os
import json
import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict
from collections import defaultdict
import threading


@dataclass
class CodeMetrics:
    """Metrics for a piece of code."""
    lines_of_code: int
    complexity_score: float
    function_count: int
    class_count: int
    comment_ratio: float
    test_coverage: float
    issues: List[str]
    quality_score: float  # 0-100


@dataclass
class CodePattern:
    """A learned code pattern."""
    id: str
    pattern_type: str  # "idiom", "anti-pattern", "optimization"
    language: str
    description: str
    example: str
    counter_example: str
    confidence: float
    usage_count: int


class CodeEvolution:
    """
    Code evolution and self-improvement system:
    1. Analyze code quality and complexity
    2. Learn patterns from repositories
    3. Suggest improvements
    4. Track code evolution over time
    """
    
    def __init__(self, data_dir: str = "./data/code_evolution"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.patterns_file = os.path.join(data_dir, "patterns.json")
        self.analysis_file = os.path.join(data_dir, "analysis.jsonl")
        
        self._lock = threading.RLock()
        self.patterns: Dict[str, CodePattern] = {}
        self._load_patterns()
        self._initialize_default_patterns()
    
    def _load_patterns(self):
        if os.path.exists(self.patterns_file):
            try:
                with open(self.patterns_file, 'r') as f:
                    data = json.load(f)
                    for pid, pd in data.items():
                        self.patterns[pid] = CodePattern(**pd)
            except Exception:
                pass
    
    def _save_patterns(self):
        with self._lock:
            with open(self.patterns_file, 'w') as f:
                json.dump({k: asdict(v) for k, v in self.patterns.items()}, f, indent=2)
    
    def _initialize_default_patterns(self):
        """Initialize with common Python patterns."""
        defaults = [
            CodePattern(
                id="pat_001",
                pattern_type="idiom",
                language="python",
                description="Use list comprehensions instead of for-loops for simple transformations",
                example="[x*2 for x in items]",
                counter_example="result = []\\nfor x in items:\\n    result.append(x*2)",
                confidence=0.95,
                usage_count=100
            ),
            CodePattern(
                id="pat_002",
                pattern_type="anti-pattern",
                language="python",
                description="Avoid bare except clauses",
                example="except Exception as e:",
                counter_example="except:",
                confidence=0.99,
                usage_count=200
            ),
            CodePattern(
                id="pat_003",
                pattern_type="optimization",
                language="python",
                description="Use generators for large datasets instead of lists",
                example="(x*2 for x in huge_list)",
                counter_example="[x*2 for x in huge_list]",
                confidence=0.9,
                usage_count=50
            ),
        ]
        for p in defaults:
            if p.id not in self.patterns:
                self.patterns[p.id] = p
        self._save_patterns()
    
    def analyze_code(self, code: str, language: str = "python") -> CodeMetrics:
        """Analyze code and return quality metrics."""
        lines = code.split("\\n")
        loc = len([l for l in lines if l.strip() and not l.strip().startswith("#")])
        comments = len([l for l in lines if l.strip().startswith("#")])
        
        complexity = 0
        func_count = 0
        class_count = 0
        issues = []
        
        if language == "python":
            try:
                tree = ast.parse(code)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                        complexity += 1
                    elif isinstance(node, ast.FunctionDef):
                        func_count += 1
                    elif isinstance(node, ast.ClassDef):
                        class_count += 1
            except SyntaxError:
                issues.append("Syntax error in code")
        
        # Check for anti-patterns
        if "except:" in code and "except Exception" not in code:
            issues.append("Bare except clause detected")
        if code.count("print(") > 5:
            issues.append("Excessive print statements (use logging)")
        if len(lines) > 200 and func_count == 0:
            issues.append("Long script without functions (consider modularizing)")
        
        comment_ratio = comments / len(lines) if lines else 0
        
        # Calculate quality score
        quality = 100
        quality -= len(issues) * 10
        quality -= max(0, complexity - 10) * 2
        quality -= max(0, loc - 100) * 0.1
        quality = max(0, min(100, quality))
        
        return CodeMetrics(
            lines_of_code=loc,
            complexity_score=complexity,
            function_count=func_count,
            class_count=class_count,
            comment_ratio=round(comment_ratio, 2),
            test_coverage=0.0,  # Would need test runner integration
            issues=issues,
            quality_score=round(quality, 1)
        )
    
    def suggest_improvements(self, code: str, language: str = "python") -> List[Dict]:
        """Suggest improvements based on learned patterns."""
        suggestions = []
        
        for pattern in self.patterns.values():
            if pattern.language == language and pattern.pattern_type == "anti-pattern":
                if pattern.counter_example in code:
                    suggestions.append({
                        "type": "anti-pattern",
                        "description": pattern.description,
                        "suggestion": pattern.example,
                        "confidence": pattern.confidence
                    })
        
        # Additional heuristics
        if len(code.split("\\n")) > 50 and "def " not in code:
            suggestions.append({
                "type": "structure",
                "description": "Consider breaking into functions",
                "suggestion": "Extract logical blocks into named functions",
                "confidence": 0.85
            })
        
        return suggestions
    
    def learn_from_repository(self, repo_path: str) -> Dict:
        """Learn patterns from a local repository."""
        patterns_found = defaultdict(int)
        files_analyzed = 0
        
        for root, _, files in os.walk(repo_path):
            for file in files:
                if file.endswith(".py"):
                    filepath = os.path.join(root, file)
                    try:
                        with open(filepath, 'r', encoding='utf-8') as f:
                            code = f.read()
                        
                        # Simple pattern detection
                        if "async def " in code:
                            patterns_found["async_patterns"] += 1
                        if "@dataclass" in code:
                            patterns_found["dataclass_usage"] += 1
                        if "typing." in code or "from typing import" in code:
                            patterns_found["type_hints"] += 1
                        if "pytest" in code or "unittest" in code:
                            patterns_found["testing"] += 1
                        
                        files_analyzed += 1
                    except Exception:
                        pass
        
        return {
            "files_analyzed": files_analyzed,
            "patterns_found": dict(patterns_found),
            "recommendations": [
                f"Repository uses {patterns_found.get('async_patterns', 0)} async patterns",
                f"Type hints present in {patterns_found.get('type_hints', 0)} files",
                f"Testing coverage in {patterns_found.get('testing', 0)} files",
            ]
        }
    
    def record_analysis(self, code_id: str, metrics: CodeMetrics, source: str = "user"):
        """Record a code analysis for tracking evolution."""
        entry = {
            "timestamp": __import__('datetime').datetime.now().isoformat(),
            "code_id": code_id,
            "metrics": asdict(metrics),
            "source": source,
        }
        with self._lock:
            with open(self.analysis_file, 'a') as f:
                f.write(json.dumps(entry) + "\\n")
    
    def get_evolution_report(self, code_id: str) -> Dict:
        """Track how a piece of code has evolved."""
        analyses = []
        if os.path.exists(self.analysis_file):
            try:
                with open(self.analysis_file, 'r') as f:
                    for line in f:
                        if line.strip():
                            entry = json.loads(line)
                            if entry.get("code_id") == code_id:
                                analyses.append(entry)
            except Exception:
                pass
        
        if not analyses:
            return {"message": "No evolution data found."}
        
        analyses.sort(key=lambda x: x["timestamp"])
        first = analyses[0]["metrics"]["quality_score"]
        last = analyses[-1]["metrics"]["quality_score"]
        
        return {
            "code_id": code_id,
            "versions_analyzed": len(analyses),
            "quality_first": first,
            "quality_last": last,
            "improvement": round(last - first, 1),
            "trend": "improving" if last > first else "declining" if last < first else "stable",
            "history": analyses,
        }
