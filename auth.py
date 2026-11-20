"""
auth.py — User Authentication & Session Management
====================================================
Zero-dependency auth system for Buddy OS:
- Password hashing: PBKDF2-HMAC-SHA256 with random 16-byte salt (stdlib only)
- Session tokens: UUID4
- User storage: JSON file (users.json)
- In-memory session cache (optional persistent sessions)

Usage:
    auth = AuthManager()
    user = auth.register("alice", "secure_pass123")
    user = auth.login("alice", "secure_pass123")
    token = auth.create_session(user["id"])
    user = auth.validate_session(token)
"""

import hashlib
import json
import os
import secrets
import time
import uuid
from typing import Optional

USERS_FILE = "users.json"
SALT_BYTES = 16
HASH_ITERATIONS = 600_000  # OWASP recommended for SHA-256
HASH_ALGO = "sha256"
TOKEN_EXPIRY_HOURS = 24


def _hash_password(password: str, salt: bytes = None) -> tuple[bytes, bytes]:
    """Hash a password with PBKDF2. Returns (salt, hash)."""
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    pwd_hash = hashlib.pbkdf2_hmac(
        HASH_ALGO,
        password.encode("utf-8"),
        salt,
        HASH_ITERATIONS,
    )
    return salt, pwd_hash


def _verify_password(password: str, salt: bytes, stored_hash: bytes) -> bool:
    """Verify a password against stored salt+hash."""
    _, computed_hash = _hash_password(password, salt)
    return secrets.compare_digest(computed_hash, stored_hash)


def _encode_hash(salt: bytes, pwd_hash: bytes) -> str:
    """Encode salt+hash as a hex string for storage."""
    return salt.hex() + ":" + pwd_hash.hex()


def _decode_hash(stored: str) -> tuple[bytes, bytes]:
    """Decode stored hex string back to (salt, hash)."""
    salt_hex, hash_hex = stored.split(":", 1)
    return bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)


class AuthError(Exception):
    pass


class AuthManager:
    """Manages user registration, login, and session validation."""

    def __init__(self, users_file: str = USERS_FILE):
        self.users_file = users_file
        self._sessions: dict[str, dict] = {}  # token -> session_data
        self._ensure_file()

    def _ensure_file(self):
        """Create users file if it doesn't exist."""
        if not os.path.exists(self.users_file):
            os.makedirs(os.path.dirname(self.users_file) or ".", exist_ok=True)
            with open(self.users_file, "w", encoding="utf-8") as f:
                json.dump({"users": []}, f, indent=2)

    def _load_users(self) -> list:
        """Load all users from the JSON file."""
        try:
            with open(self.users_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("users", [])
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save_users(self, users: list):
        """Save all users to the JSON file."""
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump({"users": users}, f, indent=2)

    # ---- User management -------------------------------------------------

    def register(self, username: str, password: str) -> dict:
        """Register a new user. Returns user dict (without password hash).

        Raises AuthError if username already exists or password is too short.
        """
        username = username.strip().lower()
        if len(username) < 2:
            raise AuthError("Username must be at least 2 characters.")
        if len(password) < 4:
            raise AuthError("Password must be at least 4 characters.")

        users = self._load_users()
        if any(u["username"] == username for u in users):
            raise AuthError(f"User '{username}' already exists.")

        salt, pwd_hash = _hash_password(password)
        user = {
            "id": str(uuid.uuid4()),
            "username": username,
            "password_hash": _encode_hash(salt, pwd_hash),
            "created_at": time.time(),
            "display_name": username,
            "role": "user",
        }
        users.append(user)
        self._save_users(users)

        # Return user without password hash
        return {k: v for k, v in user.items() if k != "password_hash"}

    def login(self, username: str, password: str) -> dict:
        """Authenticate a user. Returns user dict (without password hash).

        Raises AuthError on invalid credentials.
        """
        username = username.strip().lower()
        users = self._load_users()
        for user in users:
            if user["username"] == username:
                salt, stored_hash = _decode_hash(user["password_hash"])
                if _verify_password(password, salt, stored_hash):
                    return {k: v for k, v in user.items() if k != "password_hash"}

        raise AuthError("Invalid username or password.")

    def get_user_by_id(self, user_id: str) -> Optional[dict]:
        """Get user by ID. Returns None if not found."""
        users = self._load_users()
        for user in users:
            if user["id"] == user_id:
                return {k: v for k, v in user.items() if k != "password_hash"}
        return None

    def get_user_by_username(self, username: str) -> Optional[dict]:
        """Get user by username. Returns None if not found."""
        username = username.strip().lower()
        users = self._load_users()
        for user in users:
            if user["username"] == username:
                return {k: v for k, v in user.items() if k != "password_hash"}
        return None

    def list_users(self) -> list[dict]:
        """List all registered users (safe, no password hashes)."""
        users = self._load_users()
        return [{k: v for k, v in u.items() if k != "password_hash"} for u in users]

    # ---- Session management ---------------------------------------------

    def create_session(self, user_id: str) -> str:
        """Create a new session token for a user. Returns the token string."""
        token = str(uuid.uuid4())
        self._sessions[token] = {
            "user_id": user_id,
            "created_at": time.time(),
            "expires_at": time.time() + (TOKEN_EXPIRY_HOURS * 3600),
        }
        return token

    def validate_session(self, token: str) -> Optional[str]:
        """Validate a session token. Returns user_id if valid, None otherwise."""
        session = self._sessions.get(token)
        if not session:
            return None
        if time.time() > session["expires_at"]:
            # Clean up expired session
            del self._sessions[token]
            return None
        return session["user_id"]

    def revoke_session(self, token: str):
        """Revoke a session token."""
        self._sessions.pop(token, None)

    def revoke_all_user_sessions(self, user_id: str):
        """Revoke all sessions for a user."""
        to_revoke = [
            token for token, session in self._sessions.items()
            if session["user_id"] == user_id
        ]
        for token in to_revoke:
            self._sessions.pop(token, None)

    def get_user_from_token(self, token: str) -> Optional[dict]:
        """Get user from a session token. Returns None if invalid/expired."""
        user_id = self.validate_session(token)
        if not user_id:
            return None
        return self.get_user_by_id(user_id)

    def create_api_key(self, user_id: str) -> str:
        """Create a long-lived API key for programmatic access.
        API keys are prefixed with 'buddy_' for easy identification."""
        api_key = "buddy_" + secrets.token_hex(24)
        users = self._load_users()
        for user in users:
            if user["id"] == user_id:
                if "api_keys" not in user:
                    user["api_keys"] = []
                user["api_keys"].append({
                    "key": api_key,
                    "created_at": time.time(),
                    "last_used": None,
                })
                self._save_users(users)
                return api_key
        raise AuthError("User not found.")

    def validate_api_key(self, api_key: str) -> Optional[str]:
        """Validate an API key. Returns user_id if valid."""
        users = self._load_users()
        for user in users:
            for ak in user.get("api_keys", []):
                if ak["key"] == api_key:
                    # Update last_used
                    ak["last_used"] = time.time()
                    self._save_users(users)
                    return user["id"]
        return None

    def get_user_from_bearer(self, bearer: str) -> Optional[dict]:
        """Parse an Authorization header and return the user.
        Supports: 'Bearer <session_token>' or 'Bearer <api_key>'."""
        if not bearer or not bearer.startswith("Bearer "):
            return None
        token = bearer[7:].strip()
        if not token:
            return None

        # Try session token first
        user = self.get_user_from_token(token)
        if user:
            return user

        # Try API key
        user_id = self.validate_api_key(token)
        if user_id:
            return self.get_user_by_id(user_id)

        return None

    # ---- Stats -----------------------------------------------------------

    def stats(self) -> dict:
        """Get auth statistics."""
        users = self._load_users()
        active_sessions = sum(
            1 for s in self._sessions.values()
            if time.time() < s["expires_at"]
        )
        return {
            "total_users": len(users),
            "active_sessions": active_sessions,
            "sessions_cached": len(self._sessions),
            "users_file": self.users_file,
        }
