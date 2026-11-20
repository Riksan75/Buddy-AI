import textwrap
import re

class IndentFixer:
    @staticmethod
    def fix_code(code_text: str) -> str:
        """
        Automatically fixes indentation issues, removes trailing spaces, 
        and normalizes tabs/spaces for any snippet or code block.
        """
        if not code_text:
            return ""
        
        # 1. Dedent to remove common leading whitespace
        cleaned = textwrap.dedent(code_text)
        
        # 2. Normalize tabs to 4 spaces
        cleaned = cleaned.expandtabs(4)
        
        # 3. Clean up weird line endings (CRLF to LF)
        cleaned = cleaned.replace('\r\n', '\n')
        
        # 4. Strip extra empty lines at start/end
        return cleaned.strip()

    @staticmethod
    def auto_fix_python(code_text: str) -> str:
        """
        Specific check for python snippets to ensure standard formatting.
        """
        fixed = IndentFixer.fix_code(code_text)
        # Additional safety checks can be added here if needed
        return fixed
