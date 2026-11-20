"""
Buddy OS LLM Provider (v2 — HybridRouter removed)

HybridRouter's multi-provider fallback chain (NVIDIA -> OpenRouter -> Ollama,
with circuit breakers and tier scoring) was the root cause of chat hangs:
a single bad/expired cloud key would eat its full timeout on every message
before ever reaching local Ollama. That whole concept is gone now.

This version calls Ollama directly. Nothing to route, nothing to fall back
from, nothing to hang on a dead cloud key. If you later want cloud escalation
back for hard tasks specifically, add it deliberately and narrowly — not as
an always-tried-first chain.
"""
import os
import httpx
from typing import Optional, List, Dict


class _Message:
    def __init__(self, content: str):
        self.content = content
        self.role = 'assistant'


class _Choice:
    def __init__(self, content: str):
        self.message = _Message(content)


class _ChatResponse:
    def __init__(self, content: str, model: str = "unknown"):
        self.choices = [_Choice(content)]
        self.model = model


class _Chat:
    """Matches the OpenAI-style client.chat.completions.create(...) shape
    that server.py / workers.py / mobile_worker.py all call."""
    def __init__(self, provider: 'LLMProvider'):
        self.completions = _Completions(provider)


class _Completions:
    def __init__(self, provider: 'LLMProvider'):
        self._provider = provider

    def create(self, model=None, messages=None, temperature=0.7, max_tokens=4096, **kwargs):
        """OpenAI-compatible interface, kept so server.py / workers.py / etc.
        don't need to change how they call `client.chat.completions.create()`.
        Any extra kwargs (e.g. a leftover `tier=`) are accepted and ignored —
        there's nothing left to route with them."""
        messages = messages or []
        system = ""
        prompt = ""
        for m in messages:
            if m.get('role') == 'system':
                system = m['content']
            elif m.get('role') == 'user':
                prompt = m['content']

        reply = self._provider.generate(prompt, system_prompt=system, model=model,
                                         max_tokens=max_tokens, temperature=temperature)
        return _ChatResponse(reply, model=model or self._provider.default_model)


class LLMProvider:
    """
    Ollama-only LLM interface. Optional direct-Anthropic path remains available
    ONLY if you explicitly set BUDDY_LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY
    — it is never tried automatically or as a fallback.
    """
    def __init__(self, default_provider: str = "ollama"):
        self.provider = os.getenv("BUDDY_LLM_PROVIDER", default_provider)
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.ollama_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.default_model = os.getenv("MODEL", "qwen2.5-coder:7b")

        self.chat = _Chat(self)

        # simple call counter — used by /router-style status commands that
        # used to read HybridRouter's usage_report(); no cost tracking since
        # local Ollama is free.
        self._call_count = 0

        print(f"[LLMProvider] Ollama-only mode. Model: {self.default_model} "
              f"@ {self.ollama_url}")

    def generate(self, prompt: str, system_prompt: Optional[str] = None,
                 model: Optional[str] = None, task_type: str = "conversation",
                 max_tokens: int = 4096, temperature: float = 0.7) -> str:
        self._call_count += 1
        if self.provider == "anthropic" and self.anthropic_key:
            return self._call_anthropic(prompt, system_prompt,
                                         model or "claude-3-5-sonnet-20241022",
                                         max_tokens)
        return self._call_ollama(prompt, system_prompt, model or self.default_model,
                                  max_tokens, temperature)

    def _call_anthropic(self, prompt: str, system: Optional[str], model: str,
                         max_tokens: int) -> str:
        with httpx.Client(timeout=60.0) as client:
            headers = {
                "x-api-key": self.anthropic_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            payload = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]
            }
            if system:
                payload["system"] = system
            response = client.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers)
            if response.status_code != 200:
                raise Exception(f"Anthropic API Error: {response.text}")
            return response.json()["content"][0]["text"]

    def _call_ollama(self, prompt: str, system: Optional[str], model: str,
                      max_tokens: int, temperature: float) -> str:
        with httpx.Client(timeout=180.0) as client:
            full_prompt = f"System: {system}\n\nUser: {prompt}" if system else prompt
            payload = {
                "model": model,
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "num_predict": max_tokens,
                    "temperature": temperature,
                }
            }
            try:
                response = client.post(f"{self.ollama_url}/api/generate", json=payload)
            except httpx.ConnectError as e:
                raise Exception(
                    f"Cannot reach Ollama at {self.ollama_url} — is `ollama serve` "
                    f"running? ({e})"
                )
            if response.status_code != 200:
                raise Exception(f"Ollama Error ({response.status_code}): {response.text}")
            return response.json().get("response", "")

    # ------------------------------------------------------------------
    # Compatibility shims for commands that used to call HybridRouter's
    # multi-provider status methods (e.g. server.py's /router command,
    # the dashboard's stats websocket message). Ollama-only, so these are
    # simple and honest rather than faking a multi-provider report.
    # ------------------------------------------------------------------
    def available_providers(self) -> List[str]:
        return ["ollama"]

    def usage_report(self) -> Dict:
        return {
            "providers_online": ["ollama"],
            "totals": {"calls": self._call_count, "cost_usd": 0.0},
            "by_provider": {
                "ollama": {"calls": self._call_count, "cost_usd": 0.0}
            },
        }

    def health_check(self) -> Dict:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self.ollama_url}/api/tags")
            return {"ollama": "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"ollama": f"UNREACHABLE ({e})"}