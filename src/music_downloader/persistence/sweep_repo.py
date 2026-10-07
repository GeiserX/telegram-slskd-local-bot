"""The library sweep's state: songs audited and checked, runs, review pairs and replaced originals."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from .database import Database

RUN_RUNNING = "running"
RUN_DONE = "done"
RUN_INTERRUPTED = "interrupted"

DECISION_KEEP_MINE = "keep_mine"
DECISION_TAKE_NEW = "take_new"
DECISION_KEEP_BOTH = "keep_both"
DECISIONS = (DECISION_KEEP_MINE, DECISION_TAKE_NEW, DECISION_KEEP_BOTH)


@dataclass
class SweepSong:
    """One library file: its audit (JSON dict) at *size*/*mtime*, its last check and outcome.

    *declined_at*: the owner kept their file over a proposal (Keep mine or
    Keep both); the sweep proposes no other recording for it again.
    """

    file: str
    stem: str
    size: int
    mtime: float
    tier: str
    audit: dict
    last_checked_at: float | None = None
    outcome: str | None = None
    detail: str = ""
    declined_at: float | None = None


@dataclass
class SweepRun:
    id: int | None = None
    trigger: str = "schedule"
    force: bool = False
    owner: str = "bot"
    status: str = RUN_RUNNING
    total: int = 0
    done: int = 0
    current: str = ""
    counts: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None


@dataclass
class Review:
    """A proposal for the owner's ear: the library file *current_path* against the staged *proposal_path*."""

    stem: str
    current_path: str
    proposal_path: str
    proposal_ext: str
    both_name: str
    reason: str
    current_desc: str = ""
    proposal_desc: str = ""
    current_len: float | None = None
    proposal_len: float | None = None
    current_cutoff: float | None = None
    proposal_cutoff: float | None = None
    similarity: float | None = None
    run_id: int | None = None
    id: int | None = None
    created_at: float = field(default_factory=time.time)
    offered_at: float | None = None
    decided_at: float | None = None
    decision: str | None = None


@dataclass
class Replacement:
    """A library file the sweep replaced: the original parked at *parked_path* until purged."""

    stem: str
    new_path: str
    parked_path: str
    from_desc: str
    to_desc: str
    how: str  # "auto" or DECISION_TAKE_NEW
    run_id: int | None = None
    id: int | None = None
    replaced_at: float = field(default_factory=time.time)
    purged_at: float | None = None


def _song(row) -> SweepSong:
    return SweepSong(
        file=row["file"],
        stem=row["stem"],
        size=row["size"],
        mtime=row["mtime"],
        tier=row["tier"],
        audit=json.loads(row["audit"]),
        last_checked_at=row["last_checked_at"],
        outcome=row["outcome"],
        detail=row["detail"],
        declined_at=row["declined_at"],
    )


def _run(row) -> SweepRun:
    return SweepRun(
        id=row["id"],
        trigger=row["trigger"],
        force=bool(row["force"]),
        owner=row["owner"],
        status=row["status"],
        total=row["total"],
        done=row["done"],
        current=row["current"],
        counts=json.loads(row["counts"]),
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


_REVIEW_COLUMNS = (
    "stem",
    "current_path",
    "proposal_path",
    "proposal_ext",
    "both_name",
    "reason",
    "current_desc",
    "proposal_desc",
    "current_len",
    "proposal_len",
    "current_cutoff",
    "proposal_cutoff",
    "similarity",
    "run_id",
    "created_at",
    "offered_at",
    "decided_at",
    "decision",
)
_REPLACEMENT_COLUMNS = (
    "stem",
    "new_path",
    "parked_path",
    "from_desc",
    "to_desc",
    "how",
    "run_id",
    "replaced_at",
    "purged_at",
)


class SweepRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.connection

    # ------------------------------------------------------------------ songs

    def get_song(self, file: str) -> SweepSong | None:
        row = self._conn.execute("SELECT * FROM sweep_songs WHERE file = ?", (file,)).fetchone()
        return None if row is None else _song(row)

    def songs(self) -> list[SweepSong]:
        return [_song(r) for r in self._conn.execute("SELECT * FROM sweep_songs ORDER BY file").fetchall()]

    def save_audit(self, song: SweepSong) -> None:
        """Write a fresh audit: the file changed (or is new), so its last check no longer counts."""
        self._conn.execute(
            "INSERT INTO sweep_songs (file, stem, size, mtime, tier, audit, last_checked_at, outcome, detail, declined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '', ?) "
            "ON CONFLICT(file) DO UPDATE SET stem = excluded.stem, size = excluded.size, mtime = excluded.mtime, "
            "tier = excluded.tier, audit = excluded.audit, last_checked_at = NULL, outcome = NULL, detail = '', "
            "declined_at = excluded.declined_at",
            (song.file, song.stem, song.size, song.mtime, song.tier, json.dumps(song.audit), song.declined_at),
        )
        self._conn.commit()

    def mark_checked(self, file: str, at: float, outcome: str, detail: str = "") -> None:
        self._conn.execute(
            "UPDATE sweep_songs SET last_checked_at = ?, outcome = ?, detail = ? WHERE file = ?",
            (at, outcome, detail[:500], file),
        )
        self._conn.commit()

    def mark_declined(self, stem: str, at: float) -> None:
        self._conn.execute("UPDATE sweep_songs SET declined_at = ? WHERE stem = ?", (at, stem))
        self._conn.commit()

    def is_declined(self, stem: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sweep_songs WHERE stem = ? AND declined_at IS NOT NULL LIMIT 1", (stem,)
        ).fetchone()
        return row is not None

    def forget_missing(self, present: set[str]) -> int:
        """Drop the rows of files no longer in the library; returns how many."""
        gone = [s.file for s in self.songs() if s.file not in present]
        self._conn.executemany("DELETE FROM sweep_songs WHERE file = ?", [(f,) for f in gone])
        self._conn.commit()
        return len(gone)

    def tier_counts(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT tier, COUNT(*) AS n FROM sweep_songs GROUP BY tier").fetchall()
        return {r["tier"]: r["n"] for r in rows}

    # ------------------------------------------------------------------- runs

    def add_run(self, run: SweepRun) -> SweepRun:
        cursor = self._conn.execute(
            "INSERT INTO sweep_runs (trigger, force, owner, status, total, done, current, counts, started_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.trigger,
                int(run.force),
                run.owner,
                run.status,
                run.total,
                run.done,
                run.current,
                json.dumps(run.counts),
                run.started_at,
                run.updated_at,
            ),
        )
        self._conn.commit()
        run.id = cursor.lastrowid
        return run

    def save_run(self, run: SweepRun) -> None:
        run.updated_at = time.time()
        self._conn.execute(
            "UPDATE sweep_runs SET status = ?, total = ?, done = ?, current = ?, counts = ?, updated_at = ?, "
            "finished_at = ? WHERE id = ?",
            (
                run.status,
                run.total,
                run.done,
                run.current,
                json.dumps(run.counts),
                run.updated_at,
                run.finished_at,
                run.id,
            ),
        )
        self._conn.commit()

    def latest_run(self) -> SweepRun | None:
        row = self._conn.execute("SELECT * FROM sweep_runs ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else _run(row)

    def running_runs(self) -> list[SweepRun]:
        rows = self._conn.execute("SELECT * FROM sweep_runs WHERE status = ? ORDER BY id", (RUN_RUNNING,)).fetchall()
        return [_run(r) for r in rows]

    def last_started_at(self) -> float | None:
        row = self._conn.execute("SELECT MAX(started_at) AS t FROM sweep_runs").fetchone()
        return row["t"] if row else None

    # ---------------------------------------------------------------- reviews

    def add_review(self, review: Review) -> Review:
        values = [getattr(review, c) for c in _REVIEW_COLUMNS]
        cursor = self._conn.execute(
            f"INSERT INTO sweep_reviews ({', '.join(_REVIEW_COLUMNS)}) VALUES ({', '.join('?' * len(values))})",
            values,
        )
        self._conn.commit()
        review.id = cursor.lastrowid
        return review

    def get_review(self, review_id: int) -> Review | None:
        row = self._conn.execute("SELECT * FROM sweep_reviews WHERE id = ?", (review_id,)).fetchone()
        return None if row is None else Review(**{k: row[k] for k in (*_REVIEW_COLUMNS, "id")})

    def pending_reviews(self) -> list[Review]:
        rows = self._conn.execute("SELECT * FROM sweep_reviews WHERE decision IS NULL ORDER BY id").fetchall()
        return [Review(**{k: r[k] for k in (*_REVIEW_COLUMNS, "id")}) for r in rows]

    def pending_review_for(self, stem: str) -> Review | None:
        row = self._conn.execute(
            "SELECT * FROM sweep_reviews WHERE decision IS NULL AND stem = ? ORDER BY id LIMIT 1", (stem,)
        ).fetchone()
        return None if row is None else Review(**{k: row[k] for k in (*_REVIEW_COLUMNS, "id")})

    def mark_offered(self, review_id: int, at: float) -> None:
        self._conn.execute("UPDATE sweep_reviews SET offered_at = ? WHERE id = ?", (at, review_id))
        self._conn.commit()

    def decide(self, review_id: int, decision: str, at: float) -> bool:
        """Record the decision; False when the review was already decided (a second tap)."""
        cursor = self._conn.execute(
            "UPDATE sweep_reviews SET decision = ?, decided_at = ? WHERE id = ? AND decision IS NULL",
            (decision, at, review_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def undecide(self, review_id: int) -> None:
        """Undo decide() when applying the decision failed, so the pair is offered again."""
        self._conn.execute("UPDATE sweep_reviews SET decision = NULL, decided_at = NULL WHERE id = ?", (review_id,))
        self._conn.commit()

    # ----------------------------------------------------------- replacements

    def add_replacement(self, rep: Replacement) -> Replacement:
        values = [getattr(rep, c) for c in _REPLACEMENT_COLUMNS]
        cursor = self._conn.execute(
            f"INSERT INTO sweep_replacements ({', '.join(_REPLACEMENT_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(values))})",
            values,
        )
        self._conn.commit()
        rep.id = cursor.lastrowid
        return rep

    def replacements_for_run(self, run_id: int) -> list[Replacement]:
        rows = self._conn.execute(
            "SELECT * FROM sweep_replacements WHERE run_id = ? AND how = 'auto' ORDER BY id", (run_id,)
        ).fetchall()
        return [Replacement(**{k: r[k] for k in (*_REPLACEMENT_COLUMNS, "id")}) for r in rows]

    def unpurged_before(self, cutoff: float) -> list[Replacement]:
        rows = self._conn.execute(
            "SELECT * FROM sweep_replacements WHERE purged_at IS NULL AND replaced_at <= ? ORDER BY id", (cutoff,)
        ).fetchall()
        return [Replacement(**{k: r[k] for k in (*_REPLACEMENT_COLUMNS, "id")}) for r in rows]

    def mark_purged(self, rep_id: int, at: float) -> None:
        self._conn.execute("UPDATE sweep_replacements SET purged_at = ? WHERE id = ?", (at, rep_id))
        self._conn.commit()
