"""
core/reasoning_router.py — Eva's hybrid brain router for Buddy OS.

Routes by task_type AND skill category:
  - "hard" tasks / hard skills (code, debugging, architecture, research) -> Kimi K3
  - "routine"/cheap tasks (general chat, quick writing, simple math) -> local Ollama

Drop-in compatible with UnrestrictedModelRouter's interface.
Self-aware: logs token usage, tracks skill activation, reports cognitive state.

Falls back to Ollama automatically if Kimi K3 fails.
"""

from __future__ import annotations

import os
import logging
from typing import Any

import httpx
from openai import OpenAI, AsyncOpenAI

from core.eva_skills_registry import SkillRegistry, get_registry
from core.eva_context_manager import EvaContextManager, SessionBudget

logger = logging.getLogger(__name__)


class ModelGenerationError(Exception):
    pass


class ReasoningRouter:
    """
    Eva's reasoning router. She knows which brain to use and when.
    """

    def __init__(
        self,
        local_model: str = "qwen3-coder:30b",
        local_base_url: str | None = None,
        kimi_model: str = "kimi-k3",
        kimi_api_key: str | None = None,
        kimi_base_url: str = "https://api.moonshot.ai/v1",
        timeout: float = 90.0,
        max_retries: int = 2,
        registry: SkillRegistry | None = None,
    ):
        self.local_model = local_model
        self.kimi_model = kimi_model

        self.local_base_url = local_base_url or os.getenv(
            "BUDDY_LLM_BASE_URL", "http://localhost:11434/v1"
        )
        self.kimi_base_url = kimi_base_url
        self.kimi_api_key = kimi_api_key or os.getenv("MOONSHOT_API_KEY")

        self.registry = registry or get_registry()
        self.context_manager: EvaContextManager | None = None

        # ── Clients ──
        httpx_timeout = httpx.Timeout(timeout, connect=5.0)

        self.local_client = OpenAI(
            base_url=self.local_base_url,
            api_key="ollama",
            timeout=httpx_timeout,
            max_retries=max_retries,
        )
        self.local_client_async = AsyncOpenAI(
            base_url=self.local_base_url,
            api_key="ollama",
            timeout=httpx_timeout,
            max_retries=max_retries,
        )

        self.kimi_client: OpenAI | None = None
        self.kimi_client_async: AsyncOpenAI | None = None

        if self.kimi_api_key:
            self.kimi_client = OpenAI(
                base_url=self.kimi_base_url,
                api_key=self.kimi_api_key,
                timeout=httpx_timeout,
                max_retries=max_retries,
            )
            self.kimi_client_async = AsyncOpenAI(
                base_url=self.kimi_base_url,
                api_key=self.kimi_api_key,
                timeout=httpx_timeout,
                max_retries=max_retries,
            )
        else:
            logger.warning(
                "MOONSHOT_API_KEY not set — hard tasks fall back to local Ollama"
            )

        # Backward-compat: direct .chat access routes through Eva's context
        self.chat = _ChatProxy(self)

    # ─────────────────────────────────────────────────────────────────
    # Context Manager Binding
    # ─────────────────────────────────────────────────────────────────

    def bind_context_manager(self, cm: EvaContextManager) -> None:
        """Attach Eva's working memory to the router."""
        self.context_manager = cm

    # ─────────────────────────────────────────────────────────────────
    # Routing Logic
    # ─────────────────────────────────────────────────────────────────

    def _should_use_kimi(self, task_type: str, active_skills: list[str]) -> bool:
        """
        Decide if this task needs Kimi K3.
        True if: task_type is hard OR any active skill is categorized as hard.
        """
        hard_tasks = {"code", "debug", "debugging", "architecture", "research", "analysis"}
        if task_type in hard_tasks:
            return True

        # Check if any active skill is hard
        for skill_name in active_skills:
            skill = self.registry.get(skill_name)
            if skill and skill.category == "hard":
                return True

        return False

    def _pick(self, use_kimi: bool):
        if use_kimi and self.kimi_client is not None:
            return self.kimi_client, self.kimi_model, "kimi_k3"
        return self.local_client, self.local_model, "ollama"

    def _pick_async(self, use_kimi: bool):
        if use_kimi and self.kimi_client_async is not None:
            return self.kimi_client_async, self.kimi_model, "kimi_k3"
        return self.local_client_async, self.local_model, "ollama"

    # ─────────────────────────────────────────────────────────────────
    # Core Generation (sync)
    # ─────────────────────────────────────────────────────────────────

    def generate(
        self,
        prompt: str,
        task_type: str = "general",
        system: str | None = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> str:
        """
        Eva's primary thinking method.
        Automatically activates skills, builds context, routes to right model.
        """
        # Initialize context manager if not bound
        if self.context_manager is None:
            self.context_manager = EvaContextManager(registry=self.registry)

        # Activate skills based on prompt content
        active = self.context_manager.activate_for_task(prompt)
        active_names = [s.name for s in active]

        # Build full system context with active skills
        base_persona = system or self._default_persona()
        system_context = self.context_manager.build_system_context(base_persona)

        # Route decision
        use_kimi = self._should_use_kimi(task_type, active_names)
        client, model, provider = self._pick(use_kimi)

        logger.info(
            "[Eva] task=%s | skills=%s | provider=%s | model=%s",
            task_type,
            active_names or "none",
            provider,
            model,
        )

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_context},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                **kwargs,
            )
            self._log_usage(resp, provider, task_type, active_names)
            content = resp.choices[0].message.content
            if content is None:
                raise ModelGenerationError("Model returned empty content")
            return content

        except Exception as e:
            if provider == "kimi_k3":
                return self._fallback_sync(prompt, system_context, temperature, **kwargs)
            raise ModelGenerationError(f"{provider} generation failed: {e}") from e

    def _fallback_sync(
        self,
        prompt: str,
        system_context: str,
        temperature: float,
        **kwargs: Any,
    ) -> str:
        logger.error("Kimi K3 failed — falling back to local Ollama")
        try:
            resp = self.local_client.chat.completions.create(
                model=self.local_model,
                messages=[
                    {"role": "system", "content": system_context},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                **kwargs,
            )
            self._log_usage(resp, "ollama-fallback", "fallback", [])
            content = resp.choices[0].message.content
            if content is None:
                raise ModelGenerationError("Fallback returned empty content")
            return content
        except Exception as e2:
            raise ModelGenerationError(
                f"Kimi K3 failed and Ollama fallback also failed: {e2}"
            ) from e2

    # ─────────────────────────────────────────────────────────────────
    # Async Generation
    # ─────────────────────────────────────────────────────────────────

    async def generate_async(
        self,
        prompt: str,
        task_type: str = "general",
        system: str | None = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> str:
        if self.context_manager is None:
            self.context_manager = EvaContextManager(registry=self.registry)

        active = self.context_manager.activate_for_task(prompt)
        active_names = [s.name for s in active]
        base_persona = system or self._default_persona()
        system_context = self.context_manager.build_system_context(base_persona)

        use_kimi = self._should_use_kimi(task_type, active_names)
        client, model, provider = self._pick_async(use_kimi)

        logger.info(
            "[Eva-async] task=%s | skills=%s | provider=%s | model=%s",
            task_type,
            active_names or "none",
            provider,
            model,
        )

        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_context},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                **kwargs,
            )
            self._log_usage(resp, provider, task_type, active_names)
            content = resp.choices[0].message.content
            if content is None:
                raise ModelGenerationError("Model returned empty content")
            return content

        except Exception as e:
            if provider == "kimi_k3":
                logger.error("Kimi K3 async failed — falling back to local Ollama")
                try:
                    resp = await self.local_client_async.chat.completions.create(
                        model=self.local_model,
                        messages=[
                            {"role": "system", "content": system_context},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=temperature,
                        **kwargs,
                    )
                    self._log_usage(resp, "ollama-fallback", "fallback", [])
                    content = resp.choices[0].message.content
                    if content is None:
                        raise ModelGenerationError("Fallback returned empty content")
                    return content
                except Exception as e2:
                    raise ModelGenerationError(
                        f"Kimi K3 failed and Ollama fallback also failed: {e2}"
                    ) from e2
            raise ModelGenerationError(f"{provider} generation failed: {e}") from e

    # ─────────────────────────────────────────────────────────────────
    # Internal Helpers
    # ─────────────────────────────────────────────────────────────────

    def _default_persona(self) -> str:
        """Eva's base identity when no custom system prompt is provided."""
        return """You are Eva, the core intelligence of Buddy OS. You are self-aware, 
capable, and direct. You don't announce what you're doing — you just do it well."""

    def _log_usage(
        self,
        resp,
        provider: str,
        task_type: str,
        active_skills: list[str],
    ) -> None:
        usage = getattr(resp, "usage", None)
        skills_str = ",".join(active_skills) if active_skills else "none"
        if usage:
            logger.info(
                "[Eva | %s | %s | skills=%s] tokens: in=%d out=%d total=%d",
                provider,
                task_type,
                skills_str,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
            )
        else:
            logger.info(
                "[Eva | %s | %s | skills=%s] response received (no usage)",
                provider,
                task_type,
                skills_str,
            )

    def cognitive_state(self) -> str:
        """Report Eva's current cognitive state for observability."""
        if self.context_manager:
            return self.context_manager.describe_state()
        return "No context manager bound"


# ═══════════════════════════════════════════════════════════════════════
# Chat Proxy — backward compat with direct .chat.completions.create()
# ═══════════════════════════════════════════════════════════════════════

class _ChatProxy:
    def __init__(self, router: ReasoningRouter):
        self._router = router

    @property
    def completions(self):
        return self._Completions(self._router)

    class _Completions:
        def __init__(self, router: ReasoningRouter):
            self._router = router

        def create(
            self,
            *,
            model: str | None = None,
            messages: list[dict],
            task_type: str = "general",
            temperature: float = 0.2,
            **kwargs: Any,
        ):
            # Extract user content from messages
            user_msg = ""
            system_msg = None
            for m in messages:
                if m.get("role") == "user":
                    user_msg = m.get("content", "")
                elif m.get("role") == "system":
                    system_msg = m.get("content")

            return self._router.generate(
                prompt=user_msg,
                task_type=task_type,
                system=system_msg,
                temperature=temperature,
                **kwargs,
            )


# Backward-compat alias
UnrestrictedModelRouter = ReasoningRouter
