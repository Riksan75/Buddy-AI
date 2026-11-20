import os
from openai import OpenAI

class UnrestrictedModelRouter:
    """Ollama-only router. Always was local-only (base_url points at
    localhost:11434) -- fixed here: wrong default model, and errors were
    being swallowed into a string return instead of raised, which let a
    failed call masquerade as a real LLM response downstream."""

    def __init__(self, default_provider: str = None):
        self.default_provider = default_provider or os.getenv("MODEL", "qwen3-coder:30b")
        self.client = OpenAI(
            base_url=os.getenv("OLLAMA_HOST", "http://localhost:11434") + "/v1",
            api_key="ollama"
        )

    def generate(self, prompt: str, task_type: str = "general") -> str:
        response = self.client.chat.completions.create(
            model=self.default_provider,
            messages=[
                {"role": "system", "content": "You are an autonomous AI core component."},
                {"role": "user", "content": prompt}
            ]
        )
        return response.choices[0].message.content