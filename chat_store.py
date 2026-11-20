"""
chat_store.py — Persistent Chat History Store
===============================================
Manages conversation sessions per user with:
- JSON file persistence (survives server restarts)
- Session metadata (id, title, created_at, updated_at, message_count)
- Auto-titling from first user message
- Rename and delete operations
- Listing with most-recent-first ordering

Storage format (per user):
    chat_history/{user_id}/sessions.json  # session metadata
    chat_history/{user_id}/messages/      # message files per session
"""

import json
import os
import time
import uuid
from typing import Optional

CHAT_DIR = "chat_history"


class ChatStore:
    """Persistent chat history store with session management."""

    def __init__(self, base_dir: str = CHAT_DIR):
        self.base_dir = base_dir

    def _user_dir(self, user_id: str) -> str:
        """Get directory path for a user's chat data."""
        return os.path.join(self.base_dir, user_id)

    def _sessions_file(self, user_id: str) -> str:
        """Get path to sessions metadata file."""
        return os.path.join(self._user_dir(user_id), "sessions.json")

    def _messages_file(self, user_id: str, session_id: str) -> str:
        """Get path to messages file for a session."""
        return os.path.join(self._user_dir(user_id), "messages", f"{session_id}.json")

    def _ensure_user_dir(self, user_id: str):
        """Ensure user's chat directories exist."""
        os.makedirs(os.path.join(self._user_dir(user_id), "messages"), exist_ok=True)

    # ---- Session metadata ------------------------------------------------

    def _load_sessions(self, user_id: str) -> list:
        """Load session metadata list for a user."""
        path = self._sessions_file(user_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save_sessions(self, user_id: str, sessions: list):
        """Save session metadata list for a user."""
        self._ensure_user_dir(user_id)
        with open(self._sessions_file(user_id), "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2)

    # ---- Messages --------------------------------------------------------

    def _load_messages(self, user_id: str, session_id: str) -> list:
        """Load messages for a session."""
        path = self._messages_file(user_id, session_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save_messages(self, user_id: str, session_id: str, messages: list):
        """Save messages for a session."""
        self._ensure_user_dir(user_id)
        with open(self._messages_file(user_id, session_id), "w", encoding="utf-8") as f:
            json.dump(messages, f, indent=2)

    # ---- Auto-titling ----------------------------------------------------

    def _auto_title(self, messages: list) -> str:
        """Generate a title from the first user message or return default."""
        for msg in messages:
            if msg.get("role") == "user":
                text = msg.get("text", "").strip()
                if text:
                    # Truncate to first ~50 chars
                    if len(text) > 50:
                        return text[:47] + "..."
                    return text
        return "New Chat"

    # ---- Public API ------------------------------------------------------

    def list_sessions(self, user_id: str) -> list:
        """List all sessions for a user, most recent first."""
        sessions = self._load_sessions(user_id)
        sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        return sessions

    def create_session(self, user_id: str, title: str = None) -> str:
        """Create a new session. Returns session_id."""
        session_id = str(uuid.uuid4())
        now = time.time()
        sessions = self._load_sessions(user_id)
        sessions.append({
            "id": session_id,
            "title": title or "New Chat",
            "created_at": now,
            "updated_at": now,
            "message_count": 0,
        })
        self._save_sessions(user_id, sessions)
        # Create empty messages file
        self._save_messages(user_id, session_id, [])
        return session_id

    def get_messages(self, user_id: str, session_id: str) -> list:
        """Get all messages for a session."""
        return self._load_messages(user_id, session_id)

    def add_message(self, user_id: str, session_id: str, role: str, text: str) -> list:
        """Add a message to a session. Returns updated message list.
        Auto-generates title from first user message."""
        messages = self._load_messages(user_id, session_id)
        messages.append({
            "role": role,
            "text": text,
            "timestamp": time.time(),
        })
        self._save_messages(user_id, session_id, messages)

        # Update session metadata
        sessions = self._load_sessions(user_id)
        for s in sessions:
            if s["id"] == session_id:
                s["updated_at"] = time.time()
                s["message_count"] = len(messages)
                # Auto-title from first user message
                if s["title"] == "New Chat" and role == "user":
                    s["title"] = self._auto_title(messages)
                break
        self._save_sessions(user_id, sessions)

        return messages

    def rename_session(self, user_id: str, session_id: str, title: str) -> bool:
        """Rename a session. Returns True if successful."""
        sessions = self._load_sessions(user_id)
        for s in sessions:
            if s["id"] == session_id:
                s["title"] = title.strip() or "Untitled"
                s["updated_at"] = time.time()
                self._save_sessions(user_id, sessions)
                return True
        return False

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """Delete a session and its messages. Returns True if successful."""
        # Remove from sessions list
        sessions = self._load_sessions(user_id)
        new_sessions = [s for s in sessions if s["id"] != session_id]
        if len(new_sessions) == len(sessions):
            return False  # Not found
        self._save_sessions(user_id, new_sessions)

        # Delete messages file
        msg_path = self._messages_file(user_id, session_id)
        if os.path.exists(msg_path):
            try:
                os.remove(msg_path)
            except OSError:
                pass

        # If no sessions remain, ensure at least the default
        if not new_sessions:
            self.create_session(user_id, "Default Chat")

        return True

    def get_or_create_default(self, user_id: str) -> str:
        """Get the most recent session, or create a default one."""
        sessions = self.list_sessions(user_id)
        if sessions:
            return sessions[0]["id"]
        return self.create_session(user_id, "Default Chat")

    def stats(self, user_id: str = None) -> dict:
        """Get storage statistics."""
        if user_id:
            sessions = self._load_sessions(user_id)
            msg_count = sum(
                len(self._load_messages(user_id, s["id"]))
                for s in sessions
            )
            return {
                "session_count": len(sessions),
                "total_messages": msg_count,
                "storage_path": self._user_dir(user_id),
            }

        # Global stats
        total_users = 0
        total_sessions = 0
        total_messages = 0
        if os.path.exists(self.base_dir):
            for uid in os.listdir(self.base_dir):
                user_path = os.path.join(self.base_dir, uid)
                if os.path.isdir(user_path):
                    total_users += 1
                    sessions = self._load_sessions(uid)
                    total_sessions += len(sessions)
                    for s in sessions:
                        total_messages += len(self._load_messages(uid, s["id"]))

        return {
            "total_users": total_users,
            "total_sessions": total_sessions,
            "total_messages": total_messages,
            "storage_path": self.base_dir,
        }
