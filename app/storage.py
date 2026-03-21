from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models import UserPreferences


class UserPreferencesRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    city_key TEXT NOT NULL,
                    min_price INTEGER NULL,
                    max_price INTEGER NULL,
                    rooms INTEGER NULL
                )
                """
            )
            connection.commit()

    def get(self, user_id: int) -> UserPreferences:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, city_key, min_price, max_price, rooms FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            prefs = UserPreferences(user_id=user_id)
            self.save(prefs)
            return prefs
        return UserPreferences(
            user_id=row["user_id"],
            city_key=row["city_key"],
            min_price=row["min_price"],
            max_price=row["max_price"],
            rooms=row["rooms"],
        )

    def save(self, prefs: UserPreferences) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_preferences (user_id, city_key, min_price, max_price, rooms)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    city_key = excluded.city_key,
                    min_price = excluded.min_price,
                    max_price = excluded.max_price,
                    rooms = excluded.rooms
                """,
                (prefs.user_id, prefs.city_key, prefs.min_price, prefs.max_price, prefs.rooms),
            )
            connection.commit()
