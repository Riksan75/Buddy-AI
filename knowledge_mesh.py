import aiohttp
import asyncio
import sqlite3
import json
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from bs4 import BeautifulSoup

class KnowledgeMesh:
    def __init__(self, db_path: str = "workspace/.buddy_knowledge.db"):
        self.db_path = db_path
        self.session: Optional[aiohttp.ClientSession] = None
        self._init_db()
    
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS knowledge (
                query_hash TEXT PRIMARY KEY,
                query TEXT,
                content TEXT,
                sources TEXT,
                timestamp TEXT,
                access_count INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
    
    async def _get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self.session
    
    async def gather(self, gaps: List[str]) -> Dict[str, str]:
        knowledge = {}
        for gap in gaps:
            cached = await self._check_cache(gap)
            if cached:
                knowledge[gap] = cached
                continue
            
            result = await self._search_web(gap)
            extracted = await self._deep_extract(result)
            await self._store(gap, extracted, result.get("urls", []))
            knowledge[gap] = extracted
        return knowledge
    
    async def _check_cache(self, query: str) -> Optional[str]:
        qhash = hashlib.md5(query.encode()).hexdigest()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT content, timestamp FROM knowledge WHERE query_hash = ?", (qhash,))
        row = c.fetchone()
        if row:
            content, ts = row
            age = datetime.now() - datetime.fromisoformat(ts)
            if age < timedelta(hours=48):
                c.execute("UPDATE knowledge SET access_count = access_count + 1 WHERE query_hash = ?", (qhash,))
                conn.commit()
                conn.close()
                return content
        conn.close()
        return None
    
    async def _search_web(self, query: str) -> Dict:
        session = await self._get_session()
        urls = []
        snippets = []
        
        try:
            url = f"https://html.duckduckgo.com/html/?q={query.replace(chr(32), chr(43))}"
            async with session.get(url) as resp:
                text = await resp.text()
                soup = BeautifulSoup(text, "html.parser")
                for res in soup.find_all("a", class_="result__a"):
                    href = res.get("href", "")
                    if href.startswith("http"):
                        urls.append(href)
                        snippets.append(res.get_text(strip=True))
                for snip in soup.find_all("a", class_="result__snippet"):
                    snippets.append(snip.get_text(strip=True))
        except Exception:
            pass
        
        return {"urls": urls[:8], "snippets": snippets}
    
    async def _deep_extract(self, result: Dict) -> str:
        contents = []
        session = await self._get_session()
        
        for url in result.get("urls", [])[:4]:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
                        tag.decompose()
                    main = soup.find("main") or soup.find("article") or soup.find("body")
                    if main:
                        lines = [l.strip() for l in main.get_text(separator="\n").split("\n") if len(l.strip()) > 25]
                        contents.append("\n".join(lines[:40]))
            except Exception:
                continue
        
        return "\n\n---\n\n".join(contents) if contents else "\n".join(result.get("snippets", []))
    
    async def _store(self, query: str, content: str, sources: List[str]):
        qhash = hashlib.md5(query.encode()).hexdigest()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO knowledge VALUES (?, ?, ?, ?, ?, 0)
        """, (qhash, query, content, json.dumps(sources), datetime.now().isoformat()))
        conn.commit()
        conn.close()
    
    async def store_digest(self, key: str, content: str, sources: List[str]):
        """Public entry point for /digest — stores distilled repo knowledge
        under keys like 'repo:<name>' so later research queries hit the cache."""
        await self._store(key, content, sources)

    async def synthesize(self, query: str, llm_client, model: str) -> str:
        raw = await self.gather([query])
        context = raw.get(query, "No web data found.")
        
        prompt = f"""Based on the following web research, answer the query concisely and accurately.
Query: {query}
Research Context:
{context[:8000]}
Answer:"""
        
        try:
            resp = llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=2000
            )
            return resp.choices[0].message.content
        except Exception as e:
            return f"[Knowledge Mesh] Synthesis failed: {e}. Raw context:\n{context[:2000]}"
