"""Tracks to search again later: nothing was found, or a better copy is wanted (/wishlist)."""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field

from music_downloader.metadata.spotify import TrackInfo

from .database import Database
from .pending_repo import _from_fields

WANTED_ANY = "any"
WANTED_BETTER = "better"


@dataclass
class Wish:
    """One wishlist row. *baseline_tier* is set for a "better" wish only."""

    chat_id: int
    track: TrackInfo
    profile: str
    wanted: str
    user_id: int | None = None
    baseline_tier: int | None = None
    id: int | None = None
    created_at: float = field(default_factory=time.time)
    last_checked_at: float | None = None
    checks: int = 0
    notified_at: float | None = None


def _row_to_wish(row) -> Wish:
    return Wish(
        id=row["id"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        track=_from_fields(TrackInfo, json.loads(row["track"])),
        profile=row["profile"],
        wanted=row["wanted"],
        baseline_tier=row["baseline_tier"],
        created_at=row["created_at"],
        last_checked_at=row["last_checked_at"],
        checks=row["checks"],
        notified_at=row["notified_at"],
    )


class WishlistRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.connection

    def add(self, wish: Wish) -> Wish:
        """Store *wish* and return it with its new id."""
        cursor = self._conn.execute(
            "INSERT INTO wishlist (chat_id, user_id, track, profile, wanted, baseline_tier, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                wish.chat_id,
                wish.user_id,
                json.dumps(dataclasses.asdict(wish.track)),
                wish.profile,
                wish.wanted,
                wish.baseline_tier,
                wish.created_at,
            ),
        )
        self._conn.commit()
        return dataclasses.replace(wish, id=cursor.lastrowid)

    def get(self, wish_id: int) -> Wish | None:
        row = self._conn.execute("SELECT * FROM wishlist WHERE id = ?", (wish_id,)).fetchone()
        return None if row is None else _row_to_wish(row)

    def list_for_chat(self, chat_id: int) -> list[Wish]:
        rows = self._conn.execute("SELECT * FROM wishlist WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
        return [_row_to_wish(r) for r in rows]

    def list_all(self) -> list[Wish]:
        return [_row_to_wish(r) for r in self._conn.execute("SELECT * FROM wishlist ORDER BY id").fetchall()]

    def remove(self, chat_id: int, wish_id: int) -> bool:
        """Delete a wish of *chat_id*; False when it is gone or belongs to another chat."""
        cursor = self._conn.execute("DELETE FROM wishlist WHERE id = ? AND chat_id = ?", (wish_id, chat_id))
        self._conn.commit()
        return cursor.rowcount > 0

    def mark_checked(self, wish_id: int, at: float) -> None:
        """One more search ran for the wish at *at*."""
        self._conn.execute(
            "UPDATE wishlist SET last_checked_at = ?, checks = checks + 1 WHERE id = ?",
            (at, wish_id),
        )
        self._conn.commit()

    def mark_notified(self, wish_id: int, at: float) -> None:
        """A search at *at* found a copy and the chat was shown it."""
        self._conn.execute(
            "UPDATE wishlist SET last_checked_at = ?, notified_at = ?, checks = checks + 1 WHERE id = ?",
            (at, at, wish_id),
        )
        self._conn.commit()
