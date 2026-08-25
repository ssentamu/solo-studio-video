"""Durable SQLite job store for Solo Studio.

The API and worker share this module instead of performing independent
read-modify-write operations on jobs.json.  The store is deliberately stdlib
only so the standalone deployment keeps the same small runtime footprint.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from package_utils import LEGACY_FLAT_RUN_ID, _open_directory_no_follow, _open_regular_descriptor


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OWNER_ID = "operator"
DEFAULT_MAX_RETRIES = 3
DEFAULT_STAGE_NAMES = (
    "research",
    "script",
    "visuals",
    "voiceover",
    "video_prompts",
    "video_generation",
    "music",
    "captions",
    "editor_export",
    "assembly",
)

LEGACY_INPUT_KEYS = frozenset({
    "id", "topic", "target_audience", "duration_seconds", "platform", "tone",
    "key_messages", "visual_style", "call_to_action", "format", "chapters", "scenes",
    "created_at", "completed_at", "error", "owner_id", "idempotency_key",
})


class JobStoreError(RuntimeError):
    """Base class for durable-store failures."""


class StoreUnavailable(JobStoreError):
    """The database cannot be opened or initialized safely."""


class InvalidStoreState(JobStoreError):
    """The database or imported state violates the store contract."""


class LeaseLost(JobStoreError):
    """A worker attempted to mutate a job it no longer owns."""


class DuplicateJob(JobStoreError):
    """An idempotency key already belongs to a different job."""


@dataclass(frozen=True)
class JobClaim:
    job: dict[str, Any]
    worker_id: str
    lease_expires_at: float


def database_path(path: str | Path | None = None) -> Path:
    """Resolve the configured database path without reading credentials."""
    if path:
        return Path(path)
    configured = os.getenv("SOLO_STUDIO_DATABASE_FILE", "").strip()
    if configured:
        return Path(configured)
    if APP_DIR == Path("/app") or APP_DIR.as_posix().startswith("/app/"):
        return Path("/app/state/solo_studio.sqlite3")
    return APP_DIR / "state" / "solo_studio.sqlite3"


def _now_epoch() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidStoreState(f"job payload is not JSON serializable: {exc}") from exc


def _load_json(value: str, *, label: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise InvalidStoreState(f"stored {label} JSON is invalid") from exc


def _read_legacy_json(path: Path) -> Any:
    """Read legacy JSON from a stable regular-file descriptor."""
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        descriptor = _open_regular_descriptor(path)
    except (OSError, ValueError) as exc:
        raise InvalidStoreState("legacy jobs.json is missing or unsafe") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise InvalidStoreState("legacy jobs.json must be a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return json.load(handle, object_pairs_hook=reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise InvalidStoreState("legacy jobs.json is malformed") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidStoreState("legacy jobs.json is missing or unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_job_id(job_id: str) -> str:
    if (
        not isinstance(job_id, str)
        or not job_id.strip()
        or len(job_id) > 128
        or job_id.strip() in {".", ".."}
        or "/" in job_id
        or "\\" in job_id
        or any(ord(character) < 32 or ord(character) == 127 for character in job_id)
    ):
        raise InvalidStoreState("job id must be a non-empty path-safe string without control characters")
    return job_id.strip()


def _validate_owner(owner_id: str) -> str:
    if (
        not isinstance(owner_id, str)
        or not owner_id.strip()
        or len(owner_id) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in owner_id)
    ):
        raise InvalidStoreState("owner id must be a non-empty string of at most 128 characters")
    return owner_id.strip()


def _validate_stage_names(stage_names: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    values = tuple(stage_names or DEFAULT_STAGE_NAMES)
    if not values or any(not isinstance(name, str) or not name.strip() for name in values):
        raise InvalidStoreState("stage names must be a non-empty sequence of strings")
    if len(set(values)) != len(values):
        raise InvalidStoreState("stage names must be unique")
    return values


def _validate_max_retries(max_retries: Any) -> int:
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 20:
        raise InvalidStoreState("max_retries must be an integer between 0 and 20")
    return max_retries


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at REAL,
    attempt INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    next_attempt_at REAL,
    error_code TEXT,
    error_message TEXT,
    completed_at TEXT,
    cancelled_at TEXT,
    input_json TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    run_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_owner_idempotency
    ON jobs(owner_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS jobs_queue_idx
    ON jobs(status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS job_stages (
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    error_message TEXT,
    artifact_json TEXT,
    PRIMARY KEY(job_id, stage_name)
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    pid INTEGER,
    hostname TEXT,
    status TEXT NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS worker_heartbeats_seen_idx ON worker_heartbeats(last_seen_at);
"""


@contextmanager
def connect(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a configured connection and convert low-level failures."""
    db_path = database_path(path)
    directory_fd = -1
    lock_fd = -1
    database_fd = -1
    connection: sqlite3.Connection | None = None
    try:
        directory_fd = _open_directory_no_follow(db_path.parent, create=True)
        lock_fd = os.open(
            f"{db_path.name}.lifecycle.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        for candidate in (db_path.name, f"{db_path.name}-wal", f"{db_path.name}-shm", f"{db_path.name}-journal"):
            try:
                candidate_stat = os.stat(candidate, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISREG(candidate_stat.st_mode):
                raise StoreUnavailable(f"unsafe SQLite state path: {candidate}")
            candidate_fd = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                os.fchmod(candidate_fd, 0o600)
            finally:
                os.close(candidate_fd)
        database_fd = os.open(
            db_path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        database_stat = os.fstat(database_fd)
        if not stat.S_ISREG(database_stat.st_mode):
            raise StoreUnavailable("SQLite database must be a regular file")
        os.fchmod(database_fd, 0o600)
        connection = sqlite3.connect(
            f"/proc/self/fd/{database_fd}",
            timeout=float(os.getenv("SOLO_STUDIO_DATABASE_TIMEOUT", "10")),
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
    except (OSError, sqlite3.Error, StoreUnavailable) as exc:
        if connection is not None:
            connection.close()
            connection = None
        if database_fd >= 0:
            os.close(database_fd)
            database_fd = -1
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            lock_fd = -1
        if directory_fd >= 0:
            os.close(directory_fd)
            directory_fd = -1
        raise StoreUnavailable(f"could not open job database: {exc.__class__.__name__}") from exc
    try:
        yield connection
    except sqlite3.Error as exc:
        raise StoreUnavailable(f"job database operation failed: {exc.__class__.__name__}") from exc
    finally:
        if connection is not None:
            connection.close()
        if database_fd >= 0:
            os.close(database_fd)
        if lock_fd >= 0:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def initialize(path: str | Path | None = None) -> Path:
    """Create the schema and return the effective database path."""
    db_path = database_path(path)
    with connect(db_path) as connection:
        try:
            connection.executescript(SCHEMA)
            _migrate_schema(connection)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', '2') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        except sqlite3.Error as exc:
            raise StoreUnavailable(f"could not initialize job database: {exc.__class__.__name__}") from exc
    return db_path


def _migrate_schema(connection: sqlite3.Connection) -> None:
    """Apply additive, non-destructive migrations for existing SQLite stores."""
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "run_id" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN run_id TEXT")
    connection.execute(
        "UPDATE jobs SET run_id=? WHERE run_id IS NULL AND status IN ('completed', 'failed', 'cancelled')",
        (LEGACY_FLAT_RUN_ID,),
    )


def _transaction(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise StoreUnavailable(f"could not begin job transaction: {exc.__class__.__name__}") from exc


def _event(connection: sqlite3.Connection, job_id: str, event_type: str, payload: Any = None) -> None:
    connection.execute(
        "INSERT INTO job_events(job_id, event_type, created_at, payload_json) VALUES(?, ?, ?, ?)",
        (job_id, event_type, _now_iso(), _json(payload or {})),
    )


def _require_live_lease(
    connection: sqlite3.Connection,
    job_id: str,
    worker_id: str,
    run_id: str,
    *,
    now: float | None = None,
) -> sqlite3.Row:
    """Revalidate the complete worker identity while a write transaction is held."""
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise InvalidStoreState("worker id is required")
    if not isinstance(run_id, str) or not run_id.strip():
        raise InvalidStoreState("run id is required")
    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    lease_now = _now_epoch() if now is None else float(now)
    if (
        row is None
        or row["status"] != "running"
        or row["lease_owner"] != worker_id
        or row["run_id"] != run_id
        or row["lease_expires_at"] is None
        or row["lease_expires_at"] <= lease_now
    ):
        raise LeaseLost(f"worker lease lost for job {job_id}")
    return row


def _merge_payload(connection: sqlite3.Connection, row: sqlite3.Row, updates: Mapping[str, Any]) -> str:
    payload = _load_json(row["input_json"], label="job input")
    if not isinstance(payload, dict):
        raise InvalidStoreState("stored job input must be an object")
    payload.update(updates)
    return _json(payload)


def _require_attempt_identity(
    connection: sqlite3.Connection,
    job_id: str,
    worker_id: str,
    run_id: str,
) -> sqlite3.Row:
    """Require the attempt identity without requiring an unexpired lease."""
    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if (
        row is None
        or row["status"] != "running"
        or row["lease_owner"] != worker_id
        or row["run_id"] != run_id
    ):
        raise LeaseLost(f"worker lease lost for job {job_id}")
    return row


def _persist_reconciliation_error(
    job_id: str,
    worker_id: str,
    run_id: str,
    *,
    code: str,
    message: str,
    path: str | Path | None,
    publication_completed: bool = False,
) -> None:
    """Persist a fenced reconciliation result, including after lease expiry."""
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        try:
            row = _require_attempt_identity(connection, job_id, worker_id, run_id)
            terminal = bool(publication_completed)
            reconciliation_payload = {
                "reconciliation_error": {"code": code[:128], "message": message[:1000], "run_id": run_id},
            }
            payload = _load_json(row["input_json"], label="job input")
            if not isinstance(payload, dict):
                raise InvalidStoreState("stored job input must be an object")
            if terminal:
                for key in ("final_video_sha256", "final_video_duration_seconds", "final_video_plan_sha256"):
                    payload.pop(key, None)
            payload.update(reconciliation_payload)
            payload_json = _json(payload)
            if terminal:
                result = connection.execute(
                    "UPDATE jobs SET input_json=?, status='failed', error_code=?, error_message=?, updated_at=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, completed_at=?, run_id=? "
                    "WHERE id=? AND status='running' AND lease_owner=? AND run_id=?",
                    (
                        payload_json, code[:128], message[:1000], _now_iso(), _now_iso(), run_id,
                        job_id, worker_id, run_id,
                    ),
                )
            else:
                result = connection.execute(
                    "UPDATE jobs SET input_json=?, error_code=?, error_message=?, updated_at=? "
                    "WHERE id=? AND status='running' AND lease_owner=? AND run_id=?",
                    (payload_json, code[:128], message[:1000], _now_iso(), job_id, worker_id, run_id),
                )
            if result.rowcount != 1:
                raise LeaseLost(f"worker lease lost for job {job_id}")
            _event(connection, job_id, "reconciliation_required", {"error_code": code[:128], "run_id": run_id})
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def publish_final_media(
    job_id: str,
    worker_id: str,
    run_id: str,
    *,
    publication: Callable[[], None],
    rollback_publication: Callable[[], None] | None = None,
    unpublish: Callable[[], None] | None = None,
    evidence: Mapping[str, Any],
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Publish final media and durable evidence under one lease barrier."""
    job_id = _validate_job_id(job_id)
    allowed = {"final_video_sha256", "final_video_duration_seconds", "final_video_plan_sha256"}
    if not isinstance(evidence, Mapping) or any(key not in allowed for key in evidence):
        raise InvalidStoreState("final media evidence contains an unsupported field")
    if rollback_publication is not None and unpublish is not None:
        raise InvalidStoreState("only one final media rollback callback may be supplied")
    rollback = rollback_publication or unpublish
    initialize(path)
    reconciliation: Exception | None = None
    reconciliation_terminal = False
    publication_attempted = False
    publication_completed = False
    with connect(path) as connection:
        _transaction(connection)
        try:
            row = _require_live_lease(connection, job_id, worker_id, run_id)
            publication_attempted = True
            publication()
            publication_completed = True
            payload_json = _merge_payload(connection, row, evidence)
            updated_at = _now_iso()
            # Re-read the clock immediately before the durable write.  A long
            # callback must not commit evidence using an entry-time lease.
            fresh_now = _now_epoch()
            result = connection.execute(
                "UPDATE jobs SET input_json=?, updated_at=? WHERE id=? AND status='running' "
                "AND lease_owner=? AND run_id=? AND lease_expires_at > ?",
                (payload_json, updated_at, job_id, worker_id, run_id, fresh_now),
            )
            if result.rowcount != 1:
                raise LeaseLost(f"worker lease lost for job {job_id}")
            _event(connection, job_id, "final_media_published", {"worker_id": worker_id, "run_id": run_id})
            connection.commit()
        except LeaseLost as exc:
            connection.rollback()
            if publication_completed:
                reconciliation_terminal = True
                if rollback is not None:
                    try:
                        rollback()
                    except Exception as rollback_exc:
                        reconciliation = RuntimeError(f"{exc}; final media rollback failed: {rollback_exc}")
                    else:
                        reconciliation = exc
                else:
                    reconciliation = exc
            else:
                raise
        except Exception as exc:
            connection.rollback()
            compensation_error: Exception | None = None
            if publication_attempted and rollback is not None:
                try:
                    rollback()
                except Exception as rollback_exc:
                    compensation_error = rollback_exc
            reconciliation = exc
            reconciliation_terminal = publication_completed
            if compensation_error is not None:
                reconciliation = RuntimeError(f"{exc}; final media rollback failed: {compensation_error}")
                reconciliation_terminal = True
    if reconciliation is not None:
        try:
            _persist_reconciliation_error(
                job_id,
                worker_id,
                run_id,
                code="publication_reconciliation_required",
                message=f"final media publication did not reach a durable state: {reconciliation}",
                path=path,
                publication_completed=reconciliation_terminal,
            )
        except Exception as reconciliation_exc:
            raise StoreUnavailable("final media publication reconciliation failed") from reconciliation_exc
        raise reconciliation
    result = get_job(job_id, path)
    if result is None:
        raise InvalidStoreState(f"job {job_id} disappeared after media publication")
    return result


def finalize_failure(
    job_id: str,
    worker_id: str,
    run_id: str,
    *,
    publish_manifest: Callable[[dict[str, Any]], Mapping[str, Any]],
    error_code: str,
    error_message: str,
    retryable: bool,
    cleanup_manifest: Callable[[], None] | None = None,
    compensate_manifest: Callable[[], None] | None = None,
    now: float | None = None,
    backoff_seconds: float | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Publish failure evidence and apply retry policy under one lease barrier."""
    job_id = _validate_job_id(job_id)
    entry_now = _now_epoch() if now is None else float(now)
    if cleanup_manifest is not None and compensate_manifest is not None:
        raise InvalidStoreState("only one failure manifest cleanup callback may be supplied")
    cleanup = cleanup_manifest or compensate_manifest
    initialize(path)
    reconciliation: Exception | None = None
    publication_attempted = False
    publication_completed = False
    with connect(path) as connection:
        _transaction(connection)
        try:
            row = _require_live_lease(connection, job_id, worker_id, run_id, now=entry_now)
            current = _row_to_job(connection, row)
            if current is None:
                raise InvalidStoreState(f"job {job_id} does not exist")
            publication_attempted = True
            manifest = publish_manifest(current)
            publication_completed = True
            if not isinstance(manifest, Mapping):
                raise InvalidStoreState("failure manifest callback must return an object")
            evidence_keys = ("package_status", "has_visuals", "has_voiceover", "has_clips", "has_final_video", "verified_clips")
            evidence = {key: manifest[key] for key in evidence_keys if key in manifest}
            can_retry = bool(retryable) and row["retry_count"] < row["max_retries"] and row["cancelled_at"] is None
            new_status = "queued" if can_retry else "failed"
            payload_json = _merge_payload(connection, row, evidence)
            delay = backoff_seconds if backoff_seconds is not None else min(3600.0, 2.0 ** row["retry_count"])
            updated_at = _now_iso()
            # Re-read immediately before the fenced UPDATE.  Both expiry
            # validation and retry scheduling use this fresh value.
            fresh_now = _now_epoch()
            next_at = fresh_now + max(0.0, float(delay)) if can_retry else None
            result = connection.execute(
                "UPDATE jobs SET input_json=?, status=?, retry_count=retry_count+?, next_attempt_at=?, "
                "error_code=?, error_message=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL, "
                "completed_at=?, run_id=? WHERE id=? AND status='running' AND lease_owner=? AND run_id=? "
                "AND lease_expires_at > ?",
                (
                    payload_json, new_status, 1 if can_retry else 0, next_at, str(error_code)[:128],
                    str(error_message)[:1000], updated_at, None if can_retry else updated_at,
                    None if can_retry else row["run_id"], job_id, worker_id, run_id, fresh_now,
                ),
            )
            if result.rowcount != 1:
                raise LeaseLost(f"worker lease lost for job {job_id}")
            _event(connection, job_id, "failure_finalized", {
                "error_code": error_code, "retryable": bool(retryable), "next_attempt_at": next_at, "run_id": run_id,
            })
            connection.commit()
        except LeaseLost as exc:
            connection.rollback()
            if publication_completed:
                reconciliation = exc
            else:
                raise
        except Exception as exc:
            connection.rollback()
            reconciliation = exc
        if reconciliation is not None and publication_attempted and cleanup is not None:
            try:
                cleanup()
            except Exception as cleanup_exc:
                reconciliation = RuntimeError(f"{reconciliation}; failure manifest cleanup failed: {cleanup_exc}")
    if reconciliation is not None:
        try:
            _persist_reconciliation_error(
                job_id,
                worker_id,
                run_id,
                code="failure_finalization_required",
                message=f"failure manifest/state finalization did not complete: {reconciliation}",
                path=path,
                publication_completed=publication_attempted,
            )
        except Exception as reconciliation_exc:
            raise StoreUnavailable("failure finalization reconciliation failed") from reconciliation_exc
        raise reconciliation
    result = get_job(job_id, path)
    if result is None:
        raise InvalidStoreState(f"job {job_id} disappeared after failure finalization")
    return result


def record_worker_heartbeat(
    worker_id: str,
    *,
    status: str = "idle",
    pid: int | None = None,
    hostname: str | None = None,
    path: str | Path | None = None,
) -> None:
    initialize(path)
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO worker_heartbeats(worker_id, pid, hostname, status, last_seen_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(worker_id) DO UPDATE SET pid=excluded.pid, hostname=excluded.hostname, status=excluded.status, last_seen_at=excluded.last_seen_at",
            (worker_id[:128], pid, (hostname or "")[:255], status[:32], _now_epoch()),
        )
        connection.commit()


def queue_snapshot(*, path: str | Path | None = None, worker_ttl: float = 120.0) -> dict[str, Any]:
    initialize(path)
    now = _now_epoch()
    with connect(path) as connection:
        counts = {
            row["status"]: row["count"]
            for row in connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        }
        workers = connection.execute(
            "SELECT worker_id, status, last_seen_at FROM worker_heartbeats WHERE last_seen_at > ? ORDER BY worker_id",
            (now - max(1.0, worker_ttl),),
        ).fetchall()
        stale_leases = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (now,),
        ).fetchone()["count"]
    return {
        "jobs": {key: int(value) for key, value in counts.items()},
        "queue_depth": int(counts.get("queued", 0)),
        "stale_leases": int(stale_leases),
        "live_workers": [
            {"worker_id": row["worker_id"], "status": row["status"], "last_seen_at": row["last_seen_at"]}
            for row in workers
        ],
    }


def _stage_rows(connection: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT stage_name, status, attempt, started_at, completed_at, error_code, "
        "error_message, artifact_json FROM job_stages WHERE job_id=? ORDER BY rowid",
        (job_id,),
    ).fetchall()
    stages = []
    for row in rows:
        item = dict(row)
        item["artifact"] = _load_json(item.pop("artifact_json"), label="stage artifact") if item.get("artifact_json") else None
        stages.append(item)
    return stages


def _row_to_job(connection: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    payload = _load_json(result.pop("input_json"), label="job input")
    if not isinstance(payload, dict):
        raise InvalidStoreState("stored job input must be an object")
    core = dict(result)
    result.update(payload)
    result.update(core)
    result["stage"] = core.get("current_stage", payload.get("stage", "waiting"))
    result["error"] = core.get("error_message") or payload.get("error")
    result["stages"] = _stage_rows(connection, result["id"])
    return result


def get_job(job_id: str, path: str | Path | None = None) -> dict[str, Any] | None:
    job_id = _validate_job_id(job_id)
    initialize(path)
    with connect(path) as connection:
        return _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def get_job_by_idempotency(owner_id: str, idempotency_key: str, path: str | Path | None = None) -> dict[str, Any] | None:
    owner_id = _validate_owner(owner_id)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return None
    initialize(path)
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?",
            (owner_id, idempotency_key.strip()),
        ).fetchone()
        return _row_to_job(connection, row)


def list_jobs(limit: int = 50, owner_id: str | None = None, path: str | Path | None = None) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 500:
        raise InvalidStoreState("job list limit must be an integer between 1 and 500")
    initialize(path)
    with connect(path) as connection:
        if owner_id is None:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        else:
            owner_id = _validate_owner(owner_id)
            rows = connection.execute(
                "SELECT * FROM jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT ?", (owner_id, limit)
            ).fetchall()
        return [_row_to_job(connection, row) for row in rows]


def create_job(
    job_id: str,
    payload: dict[str, Any],
    *,
    owner_id: str = DEFAULT_OWNER_ID,
    idempotency_key: str | None = None,
    output_dir: str | Path | None = None,
    stage_names: tuple[str, ...] | list[str] | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_status: str = "queued",
    path: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a job, returning ``(job, created)`` for idempotent callers."""
    job_id = _validate_job_id(job_id)
    owner_id = _validate_owner(owner_id)
    if not isinstance(payload, dict):
        raise InvalidStoreState("job payload must be an object")
    max_retries = _validate_max_retries(max_retries)
    if initial_status not in {"queued", "initializing"}:
        raise InvalidStoreState("initial_status must be queued or initializing")
    key = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
    stages = _validate_stage_names(stage_names)
    db_path = initialize(path)
    created_at = str(payload.get("created_at") or _now_iso())
    now = _now_iso()
    output = str(output_dir or payload.get("output_dir") or (APP_DIR / "output" / job_id))
    with connect(db_path) as connection:
        _transaction(connection)
        existing = None
        if key is not None:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?", (owner_id, key)
            ).fetchone()
        if existing is not None:
            connection.commit()
            return _row_to_job(connection, existing), False
        try:
            connection.execute(
                "INSERT INTO jobs(id, owner_id, idempotency_key, status, current_stage, progress, "
                "created_at, updated_at, max_retries, next_attempt_at, input_json, output_dir) "
                "VALUES(?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?)",
                (job_id, owner_id, key, initial_status, stages[0], created_at, now, max_retries, None, _json(payload), output),
            )
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if key is not None:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?", (owner_id, key)
                ).fetchone()
                if existing is not None:
                    return _row_to_job(connection, existing), False
            raise DuplicateJob("job id already exists") from exc
        for stage in stages:
            connection.execute(
                "INSERT INTO job_stages(job_id, stage_name, status) VALUES(?, ?, 'pending')",
                (job_id, stage),
            )
        _event(connection, job_id, "created", {"owner_id": owner_id, "idempotency_key": key})
        connection.commit()
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(connection, row), True


def claim_next_job(
    worker_id: str,
    *,
    now: float | None = None,
    lease_seconds: int = 300,
    path: str | Path | None = None,
) -> JobClaim | None:
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise InvalidStoreState("worker id is required")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86400:
        raise InvalidStoreState("lease_seconds must be between 1 and 86400")
    now = _now_epoch() if now is None else float(now)
    db_path = initialize(path)
    with connect(db_path) as connection:
        _transaction(connection)
        expired_rows = connection.execute(
            "SELECT id, retry_count, max_retries, run_id FROM jobs "
            "WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (now,),
        ).fetchall()
        for expired in expired_rows:
            retry_count = int(expired["retry_count"]) + 1
            terminal = retry_count >= int(expired["max_retries"])
            connection.execute(
                "UPDATE jobs SET status=?, retry_count=?, lease_owner=NULL, lease_expires_at=NULL, "
                "run_id=?, error_code='lease_expired', error_message=?, completed_at=?, updated_at=? WHERE id=?",
                (
                    "failed" if terminal else "queued",
                    retry_count,
                    expired["run_id"] if terminal and "run_id" in expired.keys() else None,
                    "worker lease expired; retry limit reached" if terminal else "worker lease expired; queued for retry",
                    _now_iso() if terminal else None,
                    _now_iso(),
                    expired["id"],
                ),
            )
            _event(connection, expired["id"], "lease_expired", {"retry_count": retry_count, "terminal": terminal})
        row = connection.execute(
            "SELECT * FROM jobs WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "AND cancelled_at IS NULL ORDER BY created_at ASC LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        expires = now + lease_seconds
        run_id = str(uuid.uuid4())
        attempt = int(row["attempt"]) + 1
        connection.execute(
            "UPDATE jobs SET status='running', lease_owner=?, lease_expires_at=?, attempt=?, run_id=?, "
            "updated_at=? WHERE id=? AND status='queued'",
            (worker_id, expires, attempt, run_id, _now_iso(), row["id"]),
        )
        _event(
            connection,
            row["id"],
            "claimed",
            {"worker_id": worker_id, "lease_expires_at": expires, "attempt": attempt, "run_id": run_id},
        )
        connection.commit()
        claimed = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return JobClaim(_row_to_job(connection, claimed), worker_id, expires)


def heartbeat(job_id: str, worker_id: str, *, now: float | None = None, lease_seconds: int = 300, path: str | Path | None = None) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    now = _now_epoch() if now is None else float(now)
    expires = now + lease_seconds
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        result = connection.execute(
            "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE id=? AND status='running' "
            "AND lease_owner=? AND lease_expires_at > ?",
            (expires, _now_iso(), job_id, worker_id, now),
        )
        if result.rowcount != 1:
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        _event(connection, job_id, "heartbeat", {"worker_id": worker_id, "lease_expires_at": expires})
        connection.commit()
        return _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def update_stage(
    job_id: str,
    stage_name: str,
    status: str,
    *,
    worker_id: str | None = None,
    artifact: Any = None,
    error_code: str | None = None,
    error_message: str | None = None,
    progress: float | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    if not isinstance(stage_name, str) or not stage_name.strip():
        raise InvalidStoreState("stage name is required")
    if status not in {"pending", "running", "succeeded", "failed", "skipped"}:
        raise InvalidStoreState("unknown stage status")
    lease_now = _now_epoch()
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            connection.rollback()
            raise InvalidStoreState(f"job {job_id} does not exist")
        if worker_id is not None and (job["status"] != "running" or job["lease_owner"] != worker_id or job["lease_expires_at"] is None or job["lease_expires_at"] <= lease_now):
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        stage = connection.execute(
            "SELECT * FROM job_stages WHERE job_id=? AND stage_name=?", (job_id, stage_name)
        ).fetchone()
        if stage is None:
            connection.execute("INSERT INTO job_stages(job_id, stage_name, status) VALUES(?, ?, ?)", (job_id, stage_name, status))
            stage_attempt = 1 if status == "running" else 0
        else:
            stage_attempt = stage["attempt"] + (1 if status == "running" else 0)
        started_at = _now_iso() if status == "running" else (stage["started_at"] if stage else None)
        completed_at = _now_iso() if status in {"succeeded", "failed", "skipped"} else None
        connection.execute(
            "UPDATE job_stages SET status=?, attempt=?, started_at=?, completed_at=?, error_code=?, "
            "error_message=?, artifact_json=? WHERE job_id=? AND stage_name=?",
            (status, stage_attempt, started_at, completed_at, error_code, (error_message or "")[:1000], _json(artifact) if artifact is not None else None, job_id, stage_name),
        )
        updates = ["current_stage=?", "updated_at=?"]
        values: list[Any] = [stage_name, _now_iso()]
        if progress is not None:
            if not 0 <= float(progress) <= 1:
                raise InvalidStoreState("progress must be between 0 and 1")
            updates.append("progress=?")
            values.append(float(progress))
        values.append(job_id)
        connection.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id=?", values)
        _event(connection, job_id, "stage_" + status, {"stage": stage_name, "error_code": error_code})
        connection.commit()
        return _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def complete_job(job_id: str, worker_id: str, *, path: str | Path | None = None) -> dict[str, Any]:
    return _finish_job(job_id, worker_id, status="completed", path=path)


def _finish_job(job_id: str, worker_id: str, *, status: str, path: str | Path | None = None) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    lease_now = _now_epoch()
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        result = connection.execute(
            "UPDATE jobs SET status=?, progress=?, current_stage='done', completed_at=?, updated_at=?, "
            "lease_owner=NULL, lease_expires_at=NULL WHERE id=? AND status='running' AND lease_owner=? AND lease_expires_at > ?",
            (status, 1.0 if status == "completed" else 0.0, _now_iso() if status == "completed" else None, _now_iso(), job_id, worker_id, lease_now),
        )
        if result.rowcount != 1:
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        _event(connection, job_id, status, {"worker_id": worker_id})
        connection.commit()
        return _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def fail_job(
    job_id: str,
    worker_id: str,
    *,
    error_code: str,
    error_message: str,
    retryable: bool,
    now: float | None = None,
    backoff_seconds: float | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    now = _now_epoch() if now is None else float(now)
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["status"] != "running" or row["lease_owner"] != worker_id or row["lease_expires_at"] is None or row["lease_expires_at"] <= now:
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        can_retry = bool(retryable) and row["retry_count"] < row["max_retries"] and row["cancelled_at"] is None
        next_at = None
        if can_retry:
            delay = backoff_seconds if backoff_seconds is not None else min(3600.0, 2.0 ** row["retry_count"])
            next_at = now + max(0.0, float(delay))
        new_status = "queued" if can_retry else "failed"
        connection.execute(
            "UPDATE jobs SET status=?, retry_count=retry_count+?, next_attempt_at=?, error_code=?, "
            "error_message=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL, completed_at=?, run_id=? WHERE id=?",
            (new_status, 1 if can_retry else 0, next_at, str(error_code)[:128], str(error_message)[:1000], _now_iso(), None if can_retry else _now_iso(), None if can_retry else row["run_id"], job_id),
        )
        _event(connection, job_id, "retry_scheduled" if can_retry else "failed", {"error_code": error_code, "retryable": bool(retryable), "next_attempt_at": next_at})
        connection.commit()
        return _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def cancel_job(job_id: str, *, reason: str = "cancelled by user", path: str | Path | None = None) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        result = connection.execute(
            "UPDATE jobs SET status='cancelled', cancelled_at=?, updated_at=?, lease_owner=NULL, "
            "lease_expires_at=NULL, error_code='cancelled', error_message=? "
            "WHERE id=? AND status NOT IN ('completed', 'failed', 'cancelled')",
            (_now_iso(), _now_iso(), str(reason)[:1000], job_id),
        )
        if result.rowcount:
            _event(connection, job_id, "cancelled", {"reason": reason})
        connection.commit()
        job = _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if job is None:
            raise InvalidStoreState(f"job {job_id} does not exist")
        return job


def is_cancelled(job_id: str, path: str | Path | None = None) -> bool:
    job = get_job(job_id, path)
    return bool(job and job.get("status") == "cancelled")


def update_fields(
    job_id: str,
    fields: dict[str, Any],
    *,
    worker_id: str | None = None,
    expected_status: str | None = None,
    require_not_cancelled: bool = False,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Apply a narrow job-field update under one transaction.

    This is intentionally an allowlisted updater rather than a SQL escape hatch.
    Worker mutations can require the current lease, which prevents a stale
    worker from publishing a terminal status after its lease was reclaimed.
    """
    job_id = _validate_job_id(job_id)
    allowed = {
        "status", "current_stage", "progress", "error_code", "error_message",
        "completed_at", "cancelled_at", "next_attempt_at", "retry_count",
    }
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            connection.rollback()
            raise InvalidStoreState(f"job {job_id} does not exist")
        if expected_status is not None and row["status"] != expected_status:
            connection.rollback()
            current = _row_to_job(connection, row)
            if current is None:
                raise InvalidStoreState(f"job {job_id} disappeared during transition")
            return current
        if require_not_cancelled and row["cancelled_at"] is not None:
            connection.rollback()
            current = _row_to_job(connection, row)
            if current is None:
                raise InvalidStoreState(f"job {job_id} disappeared during transition")
            return current
        if worker_id is not None and (row["status"] != "running" or row["lease_owner"] != worker_id or row["lease_expires_at"] is None or row["lease_expires_at"] <= _now_epoch()):
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        normalized = dict(fields)
        if "stage" in normalized and "current_stage" not in normalized:
            normalized["current_stage"] = normalized.pop("stage")
        if "error" in normalized and "error_message" not in normalized:
            normalized["error_message"] = normalized.pop("error")
        updates = {key: value for key, value in normalized.items() if key in allowed}
        payload_updates = {key: value for key, value in normalized.items() if key not in allowed}
        if payload_updates:
            payload = _load_json(row["input_json"], label="job input")
            if not isinstance(payload, dict):
                connection.rollback()
                raise InvalidStoreState("stored job input must be an object")
            payload.update(payload_updates)
            updates["input_json"] = _json(payload)
        updates["updated_at"] = _now_iso()
        if updates.get("status") in {"completed", "failed", "cancelled"}:
            updates.setdefault("completed_at", _now_iso() if updates["status"] != "cancelled" else None)
            updates["lease_owner"] = None
            updates["lease_expires_at"] = None
        assignments = []
        values: list[Any] = []
        for key, value in updates.items():
            if key not in {"updated_at", "lease_owner", "lease_expires_at", "input_json"} and key in {"error_message", "error_code"}:
                value = None if value is None else str(value)[:1000]
            assignments.append(f"{key}=?")
            values.append(value)
        values.append(job_id)
        connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", values)
        _event(connection, job_id, "updated", {"fields": sorted(fields)})
        connection.commit()
        return _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def reconcile_expired_leases(*, now: float | None = None, path: str | Path | None = None) -> int:
    now = _now_epoch() if now is None else float(now)
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        expired_rows = connection.execute(
            "SELECT id, retry_count, max_retries, run_id FROM jobs "
            "WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
            (now,),
        ).fetchall()
        for expired in expired_rows:
            retry_count = int(expired["retry_count"]) + 1
            terminal = retry_count >= int(expired["max_retries"])
            connection.execute(
                "UPDATE jobs SET status=?, retry_count=?, lease_owner=NULL, lease_expires_at=NULL, "
                "run_id=?, error_code='lease_expired', error_message=?, completed_at=?, updated_at=? WHERE id=?",
                (
                    "failed" if terminal else "queued",
                    retry_count,
                    expired["run_id"] if terminal else None,
                    "worker lease expired; retry limit reached" if terminal else "worker lease expired; queued for retry",
                    _now_iso() if terminal else None,
                    _now_iso(),
                    expired["id"],
                ),
            )
            _event(connection, expired["id"], "lease_expired", {"retry_count": retry_count, "terminal": terminal})
        connection.commit()
        return len(expired_rows)


def reconcile_initializing_jobs(
    *,
    now: float | None = None,
    max_age_seconds: float = 900.0,
    path: str | Path | None = None,
) -> int:
    """Terminally fail jobs stranded during API materialization.

    A process can die after inserting an ``initializing`` row but before the
    brief and initial artifacts are published. Such a row must not remain
    invisible to the queue forever. Unknown timestamps are also failed closed
    rather than treated as fresh state.
    """
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)):
        raise InvalidStoreState("initialization age must be numeric")
    max_age = float(max_age_seconds)
    if not 1.0 <= max_age <= 86400.0:
        raise InvalidStoreState("initialization age must be between 1 and 86400 seconds")
    now = _now_epoch() if now is None else float(now)
    if not now == now or now in (float("inf"), float("-inf")):
        raise InvalidStoreState("initialization reconciliation time must be finite")
    cutoff = now - max_age
    initialize(path)
    changed = 0
    with connect(path) as connection:
        _transaction(connection)
        rows = connection.execute(
            "SELECT id, updated_at FROM jobs WHERE status='initializing' AND cancelled_at IS NULL"
        ).fetchall()
        for row in rows:
            stale = False
            try:
                timestamp = datetime.fromisoformat(str(row["updated_at"]))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                stale = timestamp.timestamp() <= cutoff
            except (TypeError, ValueError, OverflowError, OSError):
                stale = True
            if not stale:
                continue
            message = "initialization abandoned before job became claimable"
            result = connection.execute(
                "UPDATE jobs SET status='failed', current_stage='initialization', progress=0.0, "
                "error_code='initialization_abandoned', error_message=?, completed_at=?, updated_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL WHERE id=? AND status='initializing' "
                "AND cancelled_at IS NULL",
                (message, _now_iso(), _now_iso(), row["id"]),
            )
            if result.rowcount == 1:
                _event(connection, row["id"], "initialization_abandoned", {"max_age_seconds": max_age})
                changed += 1
        connection.commit()
    return changed


def _validate_legacy_timestamp(value: Any, label: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise InvalidStoreState(f"legacy {label} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidStoreState(f"legacy {label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise InvalidStoreState(f"legacy {label} timestamp must include a timezone")
    return value.strip()


def _validate_legacy_record(key: Any, payload: Any) -> dict[str, Any]:
    if not isinstance(key, str) or not key:
        raise InvalidStoreState("legacy job mapping keys must be non-empty strings")
    if not isinstance(payload, dict):
        raise InvalidStoreState("legacy job entries must be objects")
    if payload.get("id") != key:
        raise InvalidStoreState("legacy job mapping key must match embedded id")
    job_id = _validate_job_id(key)
    if job_id != key:
        raise InvalidStoreState("legacy job mapping key must be canonical")
    owner_value = payload.get("owner_id", DEFAULT_OWNER_ID)
    owner = _validate_owner(owner_value)
    if owner != owner_value:
        raise InvalidStoreState("legacy owner_id must be canonical")
    idempotency = payload.get("idempotency_key")
    if idempotency is not None and (
        not isinstance(idempotency, str)
        or not idempotency.strip()
        or len(idempotency) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in idempotency)
    ):
        raise InvalidStoreState("legacy idempotency_key is invalid")
    if idempotency is not None and idempotency.strip() != idempotency:
        raise InvalidStoreState("legacy idempotency_key must be canonical")
    stage_value = payload.get("stage_names", DEFAULT_STAGE_NAMES)
    if stage_value is None:
        stage_value = DEFAULT_STAGE_NAMES
    if not isinstance(stage_value, (list, tuple)):
        raise InvalidStoreState("legacy stage_names must be a list of strings")
    stages = _validate_stage_names(stage_value)
    created_at = _validate_legacy_timestamp(payload.get("created_at"), "created_at", default=_now_iso())
    updated_at = _validate_legacy_timestamp(payload.get("updated_at"), "updated_at", default=created_at)
    max_retries = _validate_max_retries(payload.get("max_retries", DEFAULT_MAX_RETRIES))
    raw_status = payload.get("status", "queued")
    allowed_statuses = {"initializing", "queued", "running", "completed", "failed", "cancelled"}
    if not isinstance(raw_status, str) or raw_status not in allowed_statuses:
        raise InvalidStoreState("legacy job status is invalid")
    imported_status = "queued" if raw_status == "running" else raw_status
    raw_progress = payload.get("progress", 1.0 if imported_status == "completed" else 0.0)
    if isinstance(raw_progress, bool) or not isinstance(raw_progress, (int, float)) or not (0.0 <= float(raw_progress) <= 1.0):
        raise InvalidStoreState("legacy job progress is invalid")
    current_stage = payload.get("stage", stages[0])
    if current_stage == "waiting":
        current_stage = stages[0]
    legacy_completed_stage = imported_status == "completed" and current_stage == "done"
    if not isinstance(current_stage, str) or (current_stage not in stages and not legacy_completed_stage):
        raise InvalidStoreState("legacy current stage is invalid")
    completed_at = _validate_legacy_timestamp(payload.get("completed_at"), "completed_at") if payload.get("completed_at") is not None else None
    cancelled_at = _validate_legacy_timestamp(payload.get("cancelled_at"), "cancelled_at") if payload.get("cancelled_at") is not None else None
    if imported_status == "completed" and completed_at is None:
        completed_at = updated_at
    if imported_status == "cancelled" and cancelled_at is None:
        cancelled_at = updated_at
    error_value = payload.get("error")
    if error_value is not None and not isinstance(error_value, str):
        raise InvalidStoreState("legacy error must be a string")
    raw_output = payload.get("output_dir")
    if raw_output is not None and not isinstance(raw_output, str):
        raise InvalidStoreState("legacy output_dir must be null or a string")
    if isinstance(raw_output, str) and not raw_output.strip():
        raise InvalidStoreState("legacy output_dir must be a non-empty string")
    output_dir = Path(raw_output) if raw_output is not None else APP_DIR / "output" / job_id
    expected_output = (APP_DIR / "output" / job_id).resolve()
    if (
        not output_dir.is_absolute()
        or output_dir.is_symlink()
        or output_dir != expected_output
        or output_dir.resolve() != expected_output
    ):
        raise InvalidStoreState("legacy output_dir is outside the job output root")
    safe_input = {key: payload[key] for key in LEGACY_INPUT_KEYS if key in payload}
    return {
        "id": job_id, "owner_id": owner, "idempotency_key": idempotency.strip() if idempotency else None,
        "status": imported_status, "stage": current_stage, "progress": float(raw_progress),
        "created_at": created_at, "updated_at": updated_at, "max_retries": max_retries,
        "error": error_value, "completed_at": completed_at, "cancelled_at": cancelled_at,
        "input_json": _json(safe_input), "output_dir": str(output_dir), "stages": stages,
        "run_id": LEGACY_FLAT_RUN_ID if imported_status in {"completed", "failed", "cancelled"} else None,
    }


def import_jobs_json_once(json_path: str | Path, *, path: str | Path | None = None) -> int:
    """Import legacy jobs only into an empty database, atomically."""
    source = Path(json_path)
    from package_utils import _open_lock_file

    lock_path = source.with_name(f"{source.name}.lock")
    try:
        with _open_lock_file(lock_path) as lock_file:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            db_path = initialize(path)
            with connect(db_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                if count:
                    raise InvalidStoreState("refusing legacy import into a populated job database")
            loaded = _read_legacy_json(source)
            if not isinstance(loaded, dict):
                raise InvalidStoreState("legacy jobs.json must contain an object")
            records = [_validate_legacy_record(key, payload) for key, payload in loaded.items()]
            with connect(db_path) as connection:
                _transaction(connection)
                count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                if count:
                    connection.rollback()
                    raise InvalidStoreState("refusing legacy import into a populated job database")
                for record in records:
                    connection.execute(
                        "INSERT INTO jobs(id, owner_id, idempotency_key, status, current_stage, progress, created_at, updated_at, "
                        "max_retries, next_attempt_at, error_message, completed_at, cancelled_at, input_json, output_dir, run_id) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (record["id"], record["owner_id"], record["idempotency_key"], record["status"], record["stage"], record["progress"], record["created_at"], record["updated_at"], record["max_retries"], None,
                         record["error"], record["completed_at"], record["cancelled_at"], record["input_json"], record["output_dir"], record["run_id"]),
                    )
                    for stage in record["stages"]:
                        connection.execute("INSERT INTO job_stages(job_id, stage_name, status) VALUES(?, ?, ?)", (record["id"], stage, "pending"))
                    _event(connection, record["id"], "legacy_import", {})
                connection.commit()
                return len(loaded)
    except InvalidStoreState:
        raise
    except (OSError, ValueError) as exc:
        raise InvalidStoreState("legacy jobs.json could not be imported safely") from exc
