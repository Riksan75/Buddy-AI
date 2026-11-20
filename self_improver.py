"""
SelfImprover — Buddy grows every minute.
Searches the web, learns from results, fetches interesting pages,
and writes improvement notes automatically.
Non-blocking version.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from core.internet_tools import search_web, fetch_webpage


class SelfImprover:
    def __init__(self, autonomous_engine, llm_router=None, interval: int = 60):
        self.engine = autonomous_engine
        self.llm = llm_router
        self.interval = interval
        self.running = False
        self.topics = [
            "latest AI coding techniques",
            "FastAPI best practices 2026",
            "Python automation patterns",
            "React performance optimization",
            "autonomous agent architecture",
            "self-improving AI systems",
        ]

    async def start(self):
        self.running = True
        print("[SELF-IMPROVER] Started")

        # Give the server a moment to finish startup first
        await asyncio.sleep(5)

        while self.running:
            try:
                await self.run_once()
            except Exception as e:
                print(f"[SELF-IMPROVER] Cycle error: {e}")
            await asyncio.sleep(self.interval)

    async def run_once(self):
        print("\n[SELF-IMPROVER] Cycle started")

        for topic in self.topics:
            print(f"  Searching: {topic}")

            # Run blocking web search in a thread
            results = await asyncio.to_thread(search_web, topic, 3)

            if not results:
                continue

            for r in results:
                title = str(r.get("title", "unknown"))[:150]
                url = r.get("url", "")
                snippet = str(r.get("body", ""))[:400]

                with self.engine._lock:
                    self.engine.brain.learn_fact(f"web:{title}", url)
                    self.engine.brain.learn_fact(f"snippet:{title}", snippet)

                print(f"    Learned: {title}")

            await asyncio.sleep(2)

        # Deep-read one useful page
        first_topic = self.topics[0]
        results = await asyncio.to_thread(search_web, first_topic, 1)
        if results:
            page_text = await asyncio.to_thread(fetch_webpage, results[0]["url"])
            with self.engine._lock:
                self.engine.brain.learn_fact(
                    f"deep_page:{results[0]['title'][:100]}",
                    page_text[:1000]
                )
            print(f"    Deep-read: {results[0]['title'][:80]}")

        # Use LLM to suggest an improvement if available
        if self.llm:
            try:
                context = self.engine.brain.get_context(limit=10)
                prompt = (
                    "You are Buddy's self-improvement module.\n"
                    f"Recent context:\n{context}\n\n"
                    "Propose ONE concrete improvement to Buddy OS. "
                    "Return only the improvement text."
                )
                suggestion = await asyncio.to_thread(
                    self.llm.generate, prompt, "coding"
                )
                if suggestion and len(suggestion) > 20:
                    path = Path("workspace/self_improvements.txt")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with open(path, "a", encoding="utf-8") as f:
                        f.write(
                            f"\n--- {datetime.now().isoformat()} ---\n"
                            f"{suggestion.strip()}\n"
                        )
                    print("[SELF-IMPROVER] Saved improvement idea")
            except Exception as e:
                print(f"[SELF-IMPROVER] LLM suggestion failed: {e}")

        print("[SELF-IMPROVER] Cycle complete")
