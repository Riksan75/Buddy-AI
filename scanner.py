import os
import ast

class CodeScanner:
    @staticmethod
    def check_imports(directory: str = "core") -> str:
        missing = []
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith('.py'):
                    path = os.path.join(root, file)
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            tree = ast.parse(f.read(), filename=path)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Import):
                                for n in node.names:
                                    __import__(n.name.split('.')[0])
                            elif isinstance(node, ast.ImportFrom):
                                if node.module:
                                    __import__(node.module.split('.')[0])
                    except Exception as e:
                        missing.append(f"{path}: {str(e)}")
        return "All imports verified successfully." if not missing else f"Import issues found: {missing}"
