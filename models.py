from core.database import db

class User:
    def __init__(self, row):
        if row:
            self.id = row["id"]
            self.name = row["name"]
            self.email = row["email"]
        else:
            self.id = None
            self.name = None
            self.email = None

    @staticmethod
    def get_by_username(username):
        row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return User(row) if row else None

    @staticmethod
    def get_by_id(user_id):
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(row) if row else None

    @staticmethod
    def create(user_id, username, password_hash, created_at):
        db.execute(
            "INSERT INTO users (id, username, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, username, password_hash, created_at)
        )
        db.commit()
