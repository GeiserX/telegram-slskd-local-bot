"""Album fetches kept in SQLite, so a restart mid-album can still say what landed.

The transfers themselves are slskd's: after a restart nothing is re-enqueued.
The job is marked interrupted and the files already on disk are still
processed (Pipeline.album_recover).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field

from music_downloader.metadata.spotify import TrackInfo
from music_downloader.search.slskd_client import SearchResult

from .database import Database

logger = logging.getLogger(__name__)

ALBUM_RUNNING = "running"
ALBUM_DONE = "done"
ALBUM_INTERRUPTED = "interrupted"


@dataclass
class FolderFile:
    """One audio file in a peer's folder, as slskd's directory listing describes it."""

    filename: str  # full remote path, backslashes
    size: int
    extension: str
    bit_rate: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    length: int | None = None  # seconds

    @property
    def basename(self) -> str:
        return self.filename.rsplit("\\", 1)[-1]

    def as_result(self, username: str) -> SearchResult:
        """The file as a search result, the shape enqueue, history and save take."""
        return SearchResult(
            username=username,
            filename=self.filename,
            size=self.size,
            bit_rate=self.bit_rate,
            bit_depth=self.bit_depth,
            sample_rate=self.sample_rate,
            length=self.length,
        )


@dataclass
class FileOutcome:
    """What happened to one file of an album: where it is now, or why it is not there."""

    filename: str  # full remote path
    path: str | None = None  # the library file (deliver "library") or the file in DOWNLOAD_DIR ("path")
    # None on success, else pipeline.fetch's ENQUEUE_FAILED / DOWNLOAD_FAILED / FILE_NOT_FOUND or "process_failed".
    error: str | None = None
    state: str = ""  # slskd transfer state behind a failure ("Timeout" when it never finished)
    transfer_id: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class AlbumJob:
    """One album fetch: the peer's folder, its audio files and one outcome slot per file."""

    username: str
    remote_dir: str
    files: list[FolderFile]
    track: TrackInfo  # the chosen copy's track: the album's fallback artist and name
    deliver: str = "library"
    outcomes: list[FileOutcome | None] = field(default_factory=list)
    status: str = ALBUM_RUNNING
    chat_id: int | None = None
    id: int | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if len(self.outcomes) < len(self.files):
            self.outcomes = self.outcomes + [None] * (len(self.files) - len(self.outcomes))

    @property
    def landed(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o is not None and o.ok]

    @property
    def failed(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o is not None and not o.ok]

    @property
    def unfinished(self) -> list[int]:
        """Indexes of the files with no outcome yet."""
        return [i for i, o in enumerate(self.outcomes) if o is None]


def _fields(cls, data: dict):
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


class AlbumRepository:
    def __init__(self, db: Database) -> None:
        self._conn = db.connection

    def add(self, job: AlbumJob) -> AlbumJob:
        """Insert *job* and set its id."""
        cursor = self._conn.execute(
            """INSERT INTO album_jobs
            (chat_id, username, remote_dir, deliver, track, files, outcomes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job.chat_id,
                job.username,
                job.remote_dir,
                job.deliver,
                *self._json(job),
                job.status,
                job.created_at,
                job.updated_at,
            ),
        )
        self._conn.commit()
        job.id = cursor.lastrowid
        return job

    def save(self, job: AlbumJob) -> None:
        """Write the job's outcomes and status again."""
        job.updated_at = time.time()
        _, _, outcomes = self._json(job)
        self._conn.execute(
            "UPDATE album_jobs SET outcomes = ?, status = ?, updated_at = ? WHERE id = ?",
            (outcomes, job.status, job.updated_at, job.id),
        )
        self._conn.commit()

    def get(self, job_id: int) -> AlbumJob | None:
        row = self._conn.execute("SELECT * FROM album_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._load(row) if row else None

    def list_by_status(self, status: str) -> list[AlbumJob]:
        rows = self._conn.execute("SELECT * FROM album_jobs WHERE status = ? ORDER BY id", (status,)).fetchall()
        return [job for job in (self._load(row) for row in rows) if job is not None]

    @staticmethod
    def _json(job: AlbumJob) -> tuple[str, str, str]:
        return (
            json.dumps(dataclasses.asdict(job.track)),
            json.dumps([dataclasses.asdict(f) for f in job.files]),
            json.dumps([dataclasses.asdict(o) if o is not None else None for o in job.outcomes]),
        )

    @staticmethod
    def _load(row) -> AlbumJob | None:
        data = dict(row)
        try:
            data["track"] = _fields(TrackInfo, json.loads(data["track"]))
            data["files"] = [_fields(FolderFile, f) for f in json.loads(data["files"])]
            data["outcomes"] = [_fields(FileOutcome, o) if o else None for o in json.loads(data["outcomes"])]
            return _fields(AlbumJob, data)
        except (TypeError, ValueError):
            logger.warning("Skipping unreadable album job %s", data.get("id"))
            return None
