"""Configurable API guard: API key authentication and in-memory rate limits.

Environment variables
  BUDDY_API_KEY            shared secret expected in the x-api-key header
  BUDDY_RATE_LIMIT         max requests per window per client (default 60)
  BUDDY_MAX_AUTH_FAILURES  max wrong-key attempts per window per client (default 10)

Behaviour when BUDDY_API_KEY is NOT set: only local clients (127.0.0.1 / ::1)
are allowed. Anyone else gets 403. (Previously every client was allowed.)
"""
from __future__ import annotations

from collections import defaultdict, deque
import hmac
import os
import threading
import time

_LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_PUBLIC_PATHS = ("/", "/health")
_SWEEP_THRESHOLD = 1000  # sweep idle clients once this many are tracked


def _int_env(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


class ApiGuard:
    def __init__(self, window_seconds=60):
        self.window_seconds = window_seconds
        self._hits = defaultdict(deque)
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, dq, now):
        while dq and now - dq[0] >= self.window_seconds:
            dq.popleft()

    def _sweep(self, now):
        for table in (self._hits, self._fails):
            for key in list(table):
                self._prune(table[key], now)
                if not table[key]:
                    del table[key]

    def check(self, client_id, provided_key, path):
        """Return None if allowed, else (status_code, detail)."""
        if path in _PUBLIC_PATHS:
            return None
        expected = os.environ.get("BUDDY_API_KEY", "").strip()
        if not expected and client_id not in _LOCAL_CLIENTS:
            return (403, "BUDDY_API_KEY is not configured; only local clients are allowed")

        limit = _int_env("BUDDY_RATE_LIMIT", 60)
        max_fail = _int_env("BUDDY_MAX_AUTH_FAILURES", 10)
        now = time.monotonic()
        with self._lock:
            if len(self._hits) + len(self._fails) > _SWEEP_THRESHOLD:
                self._sweep(now)

            fails = self._fails[client_id]
            self._prune(fails, now)
            if len(fails) >= max_fail:
                return (429, "Too many failed authentication attempts")

            if expected:
                ok = hmac.compare_digest(
                    (provided_key or "").encode("utf-8"), expected.encode("utf-8")
                )
                if not ok:
                    fails.append(now)
                    return (401, "Invalid or missing API key")
            if not fails:
                del self._fails[client_id]

            hits = self._hits[client_id]
            self._prune(hits, now)
            if len(hits) >= limit:
                return (429, "Rate limit exceeded")
            hits.append(now)
        return None