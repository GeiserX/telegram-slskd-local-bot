from __future__ import annotations

from .database import Database


class SettingsRepository:
    """Per-chat settings that must survive restarts (currently: auto-mode)."""

    def __init__(self, db: Database) -> None:
        self._conn = db.connection

    def get_auto_mode(self, chat_id: int) -> bool | None:
        """Return the stored auto-mode flag, or None when the chat never set one."""
        cursor = self._conn.execute(
            "SELECT auto_mode FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = cursor.fetchone()
        return None if row is None else bool(row["auto_mode"])

    def set_auto_mode(self, chat_id: int, enabled: bool) -> None:
        self._conn.execute(
            "INSERT INTO chat_settings (chat_id, auto_mode, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(chat_id) DO UPDATE SET auto_mode = excluded.auto_mode, updated_at = datetime('now')",
            (chat_id, int(enabled)),
        )
        self._conn.commit()
