"""Durable SQLite job store for Solo Studio.

The API and worker share this module instead of performing independent
read-modify-write operations on jobs.json.  The store is deliberately stdlib
only so the standalone deployment keeps the same small runtime footprint.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from package_utils import (
    LEGACY_FLAT_RUN_ID,
    MAX_JSON_BYTES,
    _open_directory_no_follow,
    _open_regular_descriptor,
    _parse_strict_json,
    read_text_artifact,
)


APP_DIR = Path(__file__).resolve().parent
DEFAULT_OWNER_ID = "operator"
DEFAULT_MAX_RETRIES = 3


def _bounded_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(value):
        return default
    return max(minimum, min(maximum, value))


def _finite_float(value: Any, label: str, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise InvalidStoreState(f"{label} must be numeric, not boolean")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidStoreState(f"{label} is invalid") from exc
    if not math.isfinite(parsed):
        raise InvalidStoreState(f"{label} must be finite")
    if minimum is not None and parsed < minimum:
        raise InvalidStoreState(f"{label} is below the allowed minimum")
    if maximum is not None and parsed > maximum:
        raise InvalidStoreState(f"{label} exceeds the allowed maximum")
    return parsed


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InvalidStoreState(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _payload_float(value: Any, label: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidStoreState(f"{label} must be a numeric value")
    return _finite_float(value, label, minimum, maximum)


def _validate_reference_urls(payload: Mapping[str, Any]) -> None:
    if "reference_urls" not in payload:
        return
    urls = payload["reference_urls"]
    if not isinstance(urls, list) or len(urls) > 3:
        raise InvalidStoreState("reference_urls must be a list of at most 3 URLs")
    from engines import source_ingest_agent

    for url in urls:
        try:
            source_ingest_agent.validate_url_syntax(url)
        except (source_ingest_agent.SourceIngestError, TypeError, ValueError) as exc:
            del exc
            raise InvalidStoreState("reference_urls contains an invalid URL")


def _validate_payload_numbers(payload: Mapping[str, Any]) -> None:
    _validate_reference_urls(payload)
    for field in ("duration_seconds", "final_video_duration_seconds"):
        if field in payload:
            _payload_float(payload[field], field, 0.0, 86400.0)
    for field in ("chapters", "scenes"):
        if field in payload:
            _bounded_int(payload[field], field, 0, 100000)
    if "progress" in payload:
        _payload_float(payload["progress"], "progress", 0.0, 1.0)


def _validate_final_video_payload(payload: Mapping[str, Any]) -> None:
    _validate_payload_numbers(payload)
    if payload.get("package_status") != "final_video_ready":
        raise InvalidStoreState("completed job requires final_video_ready package status")
    for field in ("final_video_sha256", "final_video_plan_sha256"):
        if not isinstance(payload.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", payload[field]):
            raise InvalidStoreState("completed job requires verified final media evidence")
    if "final_video_duration_seconds" not in payload or payload["final_video_duration_seconds"] <= 0.0:
        raise InvalidStoreState("completed job requires a positive final media duration")


DEFAULT_STAGE_NAMES = (
    "research",
    "script",
    "visuals",
    "voiceover",
    "video_prompts",
    "video_generation",
    "music",
    "editing",
    "captions",
    "editor_export",
    "assembly",
)

RESERVED_STAGE_NAMES = frozenset({"done", "waiting", "initialization", "reconciliation"})

LEGACY_INPUT_KEYS = frozenset({
    "id", "topic", "target_audience", "duration_seconds", "platform", "tone",
    "key_messages", "visual_style", "call_to_action", "reference_urls", "format", "chapters", "scenes",
    "output_profile", "aspect_ratio", "created_at", "completed_at", "error", "owner_id", "idempotency_key",
    "package_status", "final_video_sha256", "final_video_duration_seconds", "final_video_plan_sha256",
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


def _output_root_for_database(db_path: str | Path) -> Path:
    resolved = Path(db_path).resolve()
    app_root = resolved.parent.parent if resolved.parent.name == "state" else resolved.parent
    return app_root / "output"


def _output_roots_for_database(db_path: str | Path) -> tuple[Path, ...]:
    return (_output_root_for_database(db_path),)


def _validate_output_dir(
    output_dir: str | Path,
    db_path: str | Path,
    *,
    job_id: str | None = None,
) -> str:
    if not isinstance(output_dir, (str, Path)):
        raise InvalidStoreState("output_dir must be a path")
    raw = str(output_dir)
    if not raw.strip():
        raise InvalidStoreState("output_dir must be a non-empty path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise InvalidStoreState("output_dir must be absolute")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise InvalidStoreState("output_dir cannot be resolved") from exc
    roots = _output_roots_for_database(db_path)
    for root in roots:
        try:
            parent_fd = _open_directory_no_follow(root.parent, create=False)
        except (OSError, ValueError) as exc:
            raise InvalidStoreState("output root parent is missing or unsafe") from exc
        try:
            try:
                root_stat = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise InvalidStoreState("output root must be a regular directory")
        finally:
            os.close(parent_fd)
    if job_id is not None:
        expected_paths = tuple(root / _validate_job_id(job_id) for root in roots)
        if candidate not in expected_paths or resolved not in expected_paths:
            raise InvalidStoreState("output_dir must be the canonical job output path")
        try:
            candidate_stat = os.lstat(candidate)
        except FileNotFoundError:
            candidate_stat = None
        if candidate_stat is not None and (stat.S_ISLNK(candidate_stat.st_mode) or not stat.S_ISDIR(candidate_stat.st_mode)):
            raise InvalidStoreState("job output directory must be a regular directory")
        return str(candidate)
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise InvalidStoreState("output_dir is outside the job output root")
    return str(candidate)


def _validate_connection_output_dir(
    connection: sqlite3.Connection,
    output_dir: Any,
    *,
    job_id: str | None = None,
) -> None:
    database = connection.execute("PRAGMA database_list").fetchone()
    database_file = database["file"] if database is not None else ""
    if isinstance(database_file, str) and database_file.startswith("/proc/self/fd/"):
        try:
            database_file = os.readlink(database_file)
        except OSError as exc:
            raise InvalidStoreState("SQLite database descriptor cannot be resolved") from exc
    if database_file:
        _validate_output_dir(output_dir, database_file, job_id=job_id)


def _now_epoch() -> float:
    return time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_JSON_BYTES:
            raise InvalidStoreState(f"job payload exceeds the {MAX_JSON_BYTES}-byte limit")
        return payload
    except (TypeError, ValueError) as exc:
        raise InvalidStoreState(f"job payload is not JSON serializable: {exc}") from exc


def _load_json(value: str, *, label: str) -> Any:
    try:
        if len(value.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError(f"stored {label} JSON exceeds the {MAX_JSON_BYTES}-byte limit")
        return _parse_strict_json(value)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise InvalidStoreState(f"stored {label} JSON is invalid") from exc


def _read_legacy_json(path: Path) -> Any:
    """Read legacy JSON from a stable regular-file descriptor."""
    try:
        return _parse_strict_json(read_text_artifact(path))
    except json.JSONDecodeError as exc:
        raise InvalidStoreState("legacy jobs.json is malformed") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InvalidStoreState("legacy jobs.json is missing or unreadable") from exc


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
    if stage_names is None:
        values = DEFAULT_STAGE_NAMES
    elif isinstance(stage_names, (list, tuple)):
        values = tuple(stage_names)
    else:
        raise InvalidStoreState("stage names must be a list or tuple of strings")
    if not values or any(not isinstance(name, str) or not name.strip() for name in values):
        raise InvalidStoreState("stage names must be a non-empty sequence of strings")
    values = tuple(_validate_current_stage(name) for name in values)
    if any(name in RESERVED_STAGE_NAMES for name in values):
        raise InvalidStoreState("stage graph contains a reserved lifecycle stage name")
    if len(set(values)) != len(values):
        raise InvalidStoreState("stage names must be unique")
    return values


def _validate_current_stage(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise InvalidStoreState("current stage must be a canonical non-empty string without control characters")
    return value


def _validate_max_retries(max_retries: Any) -> int:
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 20:
        raise InvalidStoreState("max_retries must be an integer between 0 and 20")
    return max_retries


def _validate_persisted_lifecycle_row(row: sqlite3.Row | None) -> None:
    if row is None:
        return
    if row["status"] not in {"initializing", "queued", "running", "completed", "editor_package", "failed", "cancelled"}:
        raise InvalidStoreState("persisted job status is invalid")
    _validate_current_stage(row["current_stage"])
    _finite_float(row["progress"], "persisted progress", 0.0, 1.0)
    max_retries = _validate_max_retries(row["max_retries"])
    retry_limit = max_retries + 1 if row["status"] == "failed" and row["error_code"] == "lease_expired" else max_retries
    _bounded_int(row["retry_count"], "persisted retry_count", 0, min(21, retry_limit))
    _bounded_int(row["attempt"], "persisted attempt", 0, 100000)
    for field in ("lease_expires_at", "next_attempt_at"):
        value = row[field]
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidStoreState(f"persisted {field} must be numeric")
            _finite_float(value, f"persisted {field}", 0.0)
    if row["status"] == "running":
        if not isinstance(row["lease_owner"], str) or not row["lease_owner"].strip():
            raise InvalidStoreState("running job lease_owner is invalid")
        _validate_owner(row["lease_owner"])
        if not isinstance(row["run_id"], str) or not row["run_id"].strip():
            raise InvalidStoreState("running job run_id is invalid")
        _validate_job_id(row["run_id"])
        if row["lease_expires_at"] is None:
            raise InvalidStoreState("running job lease expiry is missing")
    else:
        if row["run_id"] is not None:
            _validate_job_id(row["run_id"])
        if row["lease_owner"] is not None or row["lease_expires_at"] is not None:
            raise InvalidStoreState("non-running job cannot retain a lease")
    status = row["status"]
    if status in {"completed", "editor_package"}:
        if row["completed_at"] is None or row["current_stage"] != "done" or row["progress"] != 1.0:
            raise InvalidStoreState("terminal package job must have done stage, full progress, and completion timestamp")
        if row["cancelled_at"] is not None:
            raise InvalidStoreState("terminal package job cannot have cancelled_at")
        if row["lease_owner"] is not None or row["lease_expires_at"] is not None:
            raise InvalidStoreState("terminal package job cannot retain a lease")
    elif status == "cancelled":
        if row["cancelled_at"] is None or row["completed_at"] is not None:
            raise InvalidStoreState("cancelled job requires only a cancellation timestamp")
    elif status == "failed":
        if row["cancelled_at"] is not None:
            raise InvalidStoreState("failed job cannot have cancelled_at")
    elif status in {"initializing", "queued", "running"}:
        if row["completed_at"] is not None or row["cancelled_at"] is not None:
            raise InvalidStoreState("active job cannot have a terminal timestamp")
    for field in ("created_at", "updated_at", "completed_at", "cancelled_at"):
        value = row[field]
        if value is not None:
            _validate_legacy_timestamp(value, f"persisted {field}")
    if _timestamp_epoch(row["created_at"], "persisted created_at") > _timestamp_epoch(row["updated_at"], "persisted updated_at"):
        raise InvalidStoreState("persisted created_at cannot be later than updated_at")
    updated_epoch = _timestamp_epoch(row["updated_at"], "persisted updated_at")
    created_epoch = _timestamp_epoch(row["created_at"], "persisted created_at")
    for field in ("completed_at", "cancelled_at"):
        value = row[field]
        if value is not None:
            terminal_epoch = _timestamp_epoch(value, f"persisted {field}")
            if terminal_epoch < created_epoch:
                raise InvalidStoreState(f"persisted {field} cannot precede created_at")
            if terminal_epoch > updated_epoch:
                raise InvalidStoreState(f"persisted {field} cannot be later than updated_at")


def _validate_persisted_stage_row(row: sqlite3.Row) -> None:
    _validate_current_stage(row["stage_name"])
    if row["status"] not in {"pending", "running", "succeeded", "failed", "skipped"}:
        raise InvalidStoreState("persisted stage status is invalid")
    _bounded_int(row["attempt"], "persisted stage attempt", 0, 100000)
    for field in ("started_at", "completed_at"):
        value = row[field]
        if value is not None:
            _validate_legacy_timestamp(value, f"persisted stage {field}")
    if row["artifact_json"] is not None:
        artifact = _load_json(row["artifact_json"], label="stage artifact")
        if not isinstance(artifact, dict):
            raise InvalidStoreState("persisted stage artifact must be an object")
    status = row["status"]
    if status == "pending" and (row["started_at"] is not None or row["completed_at"] is not None):
        raise InvalidStoreState("pending stage cannot have execution timestamps")
    if status == "running" and (row["started_at"] is None or row["completed_at"] is not None):
        raise InvalidStoreState("running stage requires started_at and no completed_at")
    if status in {"succeeded", "failed", "skipped"} and (
        row["started_at"] is None or row["completed_at"] is None
    ):
        raise InvalidStoreState("terminal stage requires started_at and completed_at")
    if row["started_at"] is not None and row["completed_at"] is not None:
        if _timestamp_epoch(row["completed_at"], "persisted stage completed_at") < _timestamp_epoch(row["started_at"], "persisted stage started_at"):
            raise InvalidStoreState("persisted stage completed_at cannot precede started_at")


def _terminalize_invalid_running_row(connection: sqlite3.Connection, row: sqlite3.Row) -> None:
    now = _now_iso()
    connection.execute(
        "UPDATE jobs SET status='failed', current_stage='reconciliation', progress=0.0, "
        "retry_count=0, max_retries=0, attempt=0, lease_owner=NULL, lease_expires_at=NULL, "
        "run_id=NULL, error_code='invalid_persisted_state', "
        "error_message='persisted lifecycle state invalid; repair required', "
        "created_at=?, updated_at=?, completed_at=NULL, cancelled_at=NULL WHERE id=?",
        (now, now, row["id"]),
    )
    _event(connection, row["id"], "invalid_persisted_state", {"terminal": True})


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
    run_id TEXT,
    stage_names_json TEXT
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
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
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
            timeout=_bounded_float_env("SOLO_STUDIO_DATABASE_TIMEOUT", 10.0, 0.1, 120.0),
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
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in SCHEMA.split(";"):
                    statement = statement.strip()
                    if statement:
                        connection.execute(statement)
                _migrate_schema(connection)
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', '2') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
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
    if "stage_names_json" not in columns:
        connection.execute("ALTER TABLE jobs ADD COLUMN stage_names_json TEXT")
    jobs = connection.execute("SELECT id, status, current_stage, stage_names_json FROM jobs ORDER BY rowid").fetchall()
    for job in jobs:
        persisted_graph = job["stage_names_json"]
        if persisted_graph is None:
            configured = DEFAULT_STAGE_NAMES
        else:
            configured_payload = _load_json(persisted_graph, label="persisted stage graph")
            configured = _validate_stage_names(configured_payload)
        existing = connection.execute(
            "SELECT * FROM job_stages WHERE job_id=? ORDER BY rowid", (job["id"],)
        ).fetchall()
        existing_by_name: dict[str, sqlite3.Row] = {}
        existing_names: list[str] = []
        for stage in existing:
            _validate_persisted_stage_row(stage)
            name = stage["stage_name"]
            if name in existing_by_name or name not in configured:
                raise InvalidStoreState("persisted job stage rows cannot be reconciled to the configured graph")
            existing_by_name[name] = stage
            existing_names.append(name)
        positions = [configured.index(name) for name in existing_names]
        if positions != sorted(positions):
            raise InvalidStoreState("persisted job stage rows are out of order")
        rebuild = tuple(existing_names) != configured
        if rebuild:
            connection.execute("DELETE FROM job_stages WHERE job_id=?", (job["id"],))
        for name in configured:
            stage = existing_by_name.get(name)
            if stage is None:
                connection.execute(
                    "INSERT INTO job_stages(job_id, stage_name, status) VALUES(?, ?, 'pending')",
                    (job["id"], name),
                )
            elif rebuild:
                connection.execute(
                    "INSERT INTO job_stages(job_id, stage_name, status, attempt, started_at, completed_at, "
                    "error_code, error_message, artifact_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job["id"], name, stage["status"], stage["attempt"], stage["started_at"],
                     stage["completed_at"], stage["error_code"], stage["error_message"], stage["artifact_json"]),
                )
        if job["status"] in {"initializing", "queued", "running"}:
            statuses = [
                (existing_by_name[name]["status"] if name in existing_by_name else "pending")
                for name in configured
            ]
            terminal_statuses = {"succeeded", "skipped"}
            first_nonterminal = next(
                (index for index, status in enumerate(statuses) if status not in terminal_statuses),
                len(statuses),
            )
            if not any(status in terminal_statuses for status in statuses[first_nonterminal:]):
                current_stage = job["current_stage"]
                if first_nonterminal < len(configured):
                    desired_stage = configured[first_nonterminal]
                    legacy_active_marker = current_stage in {"waiting", "initialization", "reconciliation"}
                    if legacy_active_marker or (current_stage in configured and desired_stage != current_stage):
                        connection.execute(
                            "UPDATE jobs SET current_stage=? WHERE id=?",
                            (desired_stage, job["id"]),
                        )
        if persisted_graph is None:
            connection.execute("UPDATE jobs SET stage_names_json=? WHERE id=?", (_json(configured), job["id"]))
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
    _validate_persisted_lifecycle_row(row)
    lease_now = _now_epoch() if now is None else _finite_float(now, "lease time")
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
    _validate_payload_numbers(payload)
    payload.update(updates)
    _validate_payload_numbers(payload)
    return _json(payload)


def _require_attempt_identity(
    connection: sqlite3.Connection,
    job_id: str,
    worker_id: str,
    run_id: str,
) -> sqlite3.Row:
    """Require the attempt identity without requiring an unexpired lease."""
    row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    _validate_persisted_lifecycle_row(row)
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
            _validate_payload_numbers(payload)
            if terminal:
                for key in ("final_video_sha256", "final_video_duration_seconds", "final_video_plan_sha256"):
                    payload.pop(key, None)
            payload.update(reconciliation_payload)
            payload_json = _json(payload)
            if terminal:
                mutation_at = _now_iso()
                result = connection.execute(
                    "UPDATE jobs SET input_json=?, status='failed', error_code=?, error_message=?, updated_at=?, "
                    "lease_owner=NULL, lease_expires_at=NULL, completed_at=?, run_id=? "
                    "WHERE id=? AND status='running' AND lease_owner=? AND run_id=?",
                    (
                        payload_json, code[:128], message[:1000], mutation_at, mutation_at, run_id,
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
    if not isinstance(evidence, Mapping) or set(evidence) != allowed:
        raise InvalidStoreState("final media evidence must contain exactly the verified hash, duration, and plan hash")
    evidence = dict(evidence)
    for key in ("final_video_sha256", "final_video_plan_sha256"):
        if not isinstance(evidence[key], str) or not re.fullmatch(r"[0-9a-f]{64}", evidence[key]):
            raise InvalidStoreState(f"{key} must be a lowercase SHA-256 digest")
    if "final_video_duration_seconds" in evidence:
        evidence["final_video_duration_seconds"] = _finite_float(
            evidence["final_video_duration_seconds"],
            "final_video_duration_seconds",
            1e-09,
            86400.0,
        )
    if rollback_publication is not None and unpublish is not None:
        raise InvalidStoreState("only one final media rollback callback may be supplied")
    rollback = rollback_publication or unpublish
    if rollback is None:
        raise InvalidStoreState("final media publication requires a rollback callback")
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
                message="final media publication reconciliation required",
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
    if now is not None:
        _finite_float(now, "now")
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
            row = _require_live_lease(connection, job_id, worker_id, run_id)
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
            delay = _finite_float(delay, "backoff_seconds", 0.0, 3600.0)
            updated_at = _now_iso()
            # Re-read immediately before the fenced UPDATE.  Both expiry
            # validation and retry scheduling use this fresh value.
            fresh_now = _finite_float(_now_epoch(), "current time")
            next_at = fresh_now + delay if can_retry else None
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
        cleanup_succeeded = False
        if reconciliation is not None and publication_attempted and cleanup is not None:
            try:
                cleanup()
                cleanup_succeeded = True
            except Exception as cleanup_exc:
                reconciliation = RuntimeError(f"{reconciliation}; failure manifest cleanup failed: {cleanup_exc}")
    if reconciliation is not None:
        try:
            _persist_reconciliation_error(
                job_id,
                worker_id,
                run_id,
                code="failure_finalization_required",
                message="failure manifest/state finalization reconciliation required",
                path=path,
                publication_completed=publication_completed or cleanup_succeeded,
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
        counts: dict[str, int] = {}
        for row in connection.execute("SELECT * FROM jobs").fetchall():
            _validate_persisted_lifecycle_row(row)
            _validate_connection_output_dir(connection, row["output_dir"], job_id=row["id"])
            _validate_terminal_stage_consistency(connection, row)
            payload = _load_json(row["input_json"], label="job input")
            if not isinstance(payload, dict):
                raise InvalidStoreState("stored job input must be an object")
            _validate_payload_numbers(payload)
            _stage_rows(connection, row["id"])
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        workers = connection.execute(
            "SELECT worker_id, status, last_seen_at FROM worker_heartbeats WHERE last_seen_at > ? ORDER BY worker_id",
            (now - max(1.0, worker_ttl),),
        ).fetchall()
        stale_leases = sum(
            1
            for row in connection.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
            if row["lease_expires_at"] <= now
        )
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
        _validate_persisted_stage_row(row)
        item = dict(row)
        item["artifact"] = _load_json(item.pop("artifact_json"), label="stage artifact") if item.get("artifact_json") else None
        stages.append(item)
    return stages


def _validate_terminal_stage_consistency(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    allow_active_all_terminal: bool = False,
) -> None:
    stage_rows = connection.execute(
        "SELECT * FROM job_stages WHERE job_id=? ORDER BY rowid", (row["id"],)
    ).fetchall()
    try:
        configured = _validate_stage_names(_load_json(row["stage_names_json"], label="stage graph"))
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidStoreState("persisted stage graph is invalid") from exc
    actual = tuple(stage["stage_name"] for stage in stage_rows)
    if actual != configured:
        raise InvalidStoreState("persisted stage graph is missing, duplicated, or out of order")
    last_started_timestamp: float | None = None
    last_completed_timestamp: float | None = None
    current_timestamp = _now_epoch()
    created_timestamp = _timestamp_epoch(row["created_at"], "persisted created_at")
    updated_timestamp = _timestamp_epoch(row["updated_at"], "persisted updated_at")
    for stage in stage_rows:
        _validate_persisted_stage_row(stage)
        if stage["started_at"] is not None:
            started_timestamp = _timestamp_epoch(stage["started_at"], "persisted stage started_at")
            if started_timestamp < created_timestamp or started_timestamp > updated_timestamp:
                raise InvalidStoreState("persisted stage started_at is outside the job lifetime")
            if started_timestamp > current_timestamp:
                raise InvalidStoreState("persisted stage started_at cannot be in the future")
            if last_completed_timestamp is not None and started_timestamp < last_completed_timestamp:
                raise InvalidStoreState("persisted stage execution intervals overlap")
            if last_started_timestamp is not None and started_timestamp < last_started_timestamp:
                raise InvalidStoreState("persisted stage start timestamps are out of chronological order")
            last_started_timestamp = started_timestamp
        if stage["completed_at"] is not None:
            completed_timestamp = _timestamp_epoch(stage["completed_at"], "persisted stage completed_at")
            if completed_timestamp < created_timestamp or completed_timestamp > updated_timestamp:
                raise InvalidStoreState("persisted stage completed_at is outside the job lifetime")
            if completed_timestamp > current_timestamp:
                raise InvalidStoreState("persisted stage completed_at cannot be in the future")
            if last_completed_timestamp is not None and completed_timestamp < last_completed_timestamp:
                raise InvalidStoreState("persisted stage completion timestamps are out of chronological order")
            last_completed_timestamp = completed_timestamp
    terminal_stage_statuses = {"succeeded", "skipped"}
    first_nonterminal_index = next(
        (index for index, stage in enumerate(stage_rows) if stage["status"] not in terminal_stage_statuses),
        len(stage_rows),
    )
    if any(stage["status"] in terminal_stage_statuses for stage in stage_rows[first_nonterminal_index:]):
        raise InvalidStoreState("persisted stage statuses are out of configured order")
    if first_nonterminal_index < len(stage_rows):
        first_nonterminal = stage_rows[first_nonterminal_index]["status"]
        if first_nonterminal not in {"pending", "running", "failed"}:
            raise InvalidStoreState("persisted stage has an invalid incomplete status")
        if any(stage["status"] != "pending" for stage in stage_rows[first_nonterminal_index + 1:]):
            raise InvalidStoreState("persisted stage execution is not sequential")
    if row["status"] in {"initializing", "queued", "running"}:
        if row["current_stage"] not in configured:
            raise InvalidStoreState("active job current_stage is not in the configured stage graph")
        if first_nonterminal_index == len(stage_rows):
            if not allow_active_all_terminal:
                raise InvalidStoreState("active job has no incomplete stage")
        elif configured[first_nonterminal_index] != row["current_stage"]:
            raise InvalidStoreState("active job current_stage does not match the first incomplete stage")
    if row["status"] not in {"completed", "editor_package"}:
        return
    if any(stage["status"] not in {"succeeded", "skipped"} for stage in stage_rows):
        raise InvalidStoreState("terminal package job requires terminal stage evidence")
    for stage in stage_rows:
        _validate_persisted_stage_row(stage)


def _row_to_job(
    connection: sqlite3.Connection,
    row: sqlite3.Row | None,
    *,
    allow_active_all_terminal: bool = False,
) -> dict[str, Any] | None:
    if row is None:
        return None
    _validate_persisted_lifecycle_row(row)
    _validate_connection_output_dir(connection, row["output_dir"], job_id=row["id"])
    _validate_terminal_stage_consistency(
        connection,
        row,
        allow_active_all_terminal=allow_active_all_terminal,
    )
    result = dict(row)
    payload = _load_json(result.pop("input_json"), label="job input")
    if not isinstance(payload, dict):
        raise InvalidStoreState("stored job input must be an object")
    payload.setdefault("topic", "")
    _validate_payload_numbers(payload)
    core = dict(result)
    result.update(payload)
    result.update(core)
    if row["status"] == "completed":
        _validate_final_video_payload(payload)
    elif row["status"] == "editor_package" and payload.get("package_status") != "editor_package":
        raise InvalidStoreState("editor_package job requires editor package evidence")
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
    payload = dict(payload)
    payload.setdefault("topic", "")
    if (
        any(key in payload for key in {
            "final_video_sha256", "final_video_duration_seconds", "final_video_plan_sha256",
        })
        or payload.get("package_status") in {"final_video_ready", "editor_package", "completed"}
        or payload.get("has_final_video") is True
    ):
        raise InvalidStoreState("publication-owned media fields cannot be supplied at job creation")
    max_retries = _validate_max_retries(max_retries)
    if initial_status not in {"queued", "initializing"}:
        raise InvalidStoreState("initial_status must be queued or initializing")
    key = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
    stages = _validate_stage_names(stage_names)
    if stages != DEFAULT_STAGE_NAMES:
        raise InvalidStoreState("custom stage graphs are not supported by the worker")
    db_path = initialize(path)
    _validate_payload_numbers(payload)
    created_at = _validate_legacy_timestamp(payload.get("created_at"), "created_at", default=_now_iso())
    now = _now_iso()
    if output_dir is not None:
        output = output_dir
    elif "output_dir" in payload:
        output = payload["output_dir"]
    else:
        output = _output_root_for_database(db_path) / job_id
    output = _validate_output_dir(output, db_path, job_id=job_id)
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
                "created_at, updated_at, max_retries, next_attempt_at, input_json, output_dir, stage_names_json) "
                "VALUES(?, ?, ?, ?, ?, 0.0, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, owner_id, key, initial_status, stages[0], created_at, now, max_retries, None, _json(payload), output, _json(stages)),
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
    requested_now = None if now is None else _finite_float(now, "claim time")
    db_path = initialize(path)
    with connect(db_path) as connection:
        _transaction(connection)
        authoritative_now = _now_epoch()
        if requested_now is not None and requested_now > authoritative_now + 86400.0:
            connection.rollback()
            raise InvalidStoreState("claim time is too far ahead of the authoritative clock")
        now = authoritative_now
        expired_rows = connection.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        for expired in expired_rows:
            try:
                _validate_persisted_lifecycle_row(expired)
                _validate_connection_output_dir(connection, expired["output_dir"], job_id=expired["id"])
                _validate_terminal_stage_consistency(connection, expired)
                _stage_rows(connection, expired["id"])
            except InvalidStoreState:
                _terminalize_invalid_running_row(connection, expired)
                continue
            if expired["lease_expires_at"] > now:
                continue
            retry_count = expired["retry_count"] + 1
            terminal = retry_count > expired["max_retries"]
            mutation_at = _now_iso()
            connection.execute(
                "UPDATE jobs SET status=?, retry_count=?, lease_owner=NULL, lease_expires_at=NULL, "
                "run_id=?, error_code='lease_expired', error_message=?, completed_at=?, updated_at=? WHERE id=?",
                (
                    "failed" if terminal else "queued",
                    retry_count,
                    expired["run_id"] if terminal and "run_id" in expired.keys() else None,
                    "worker lease expired; retry limit reached" if terminal else "worker lease expired; queued for retry",
                    mutation_at if terminal else None,
                    mutation_at,
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
        _validate_persisted_lifecycle_row(row)
        _validate_connection_output_dir(connection, row["output_dir"], job_id=row["id"])
        _validate_terminal_stage_consistency(connection, row)
        _stage_rows(connection, row["id"])
        if row["retry_count"] > 0:
            retry_stages = connection.execute(
                "SELECT * FROM job_stages WHERE job_id=? ORDER BY rowid", (row["id"],)
            ).fetchall()
            if not retry_stages:
                connection.rollback()
                raise InvalidStoreState(f"job {row['id']} has no configured stages")
            for retry_stage in retry_stages:
                _validate_persisted_stage_row(retry_stage)
            connection.execute(
                "UPDATE job_stages SET status='pending', started_at=NULL, completed_at=NULL, "
                "error_code=NULL, error_message=NULL, artifact_json=NULL WHERE job_id=?",
                (row["id"],),
            )
            connection.execute(
                "UPDATE jobs SET current_stage=?, progress=0.0, next_attempt_at=NULL, "
                "error_code=NULL, error_message=NULL, updated_at=? WHERE id=?",
                (retry_stages[0]["stage_name"], _now_iso(), row["id"]),
            )
            _event(connection, row["id"], "retry_attempt_reset", {"retry_count": row["retry_count"]})
        fresh_now = _now_epoch()
        expires = fresh_now + lease_seconds
        run_id = str(uuid.uuid4())
        attempt = row["attempt"] + 1
        connection.execute(
            "UPDATE jobs SET status='running', lease_owner=?, lease_expires_at=?, attempt=?, run_id=?, "
            "updated_at=? WHERE id=? AND status='queued'",
            (worker_id, expires, attempt, run_id, _now_iso(), row["id"]),
        )
        claimed = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        claimed_job = _row_to_job(connection, claimed)
        if claimed_job is None:
            connection.rollback()
            raise InvalidStoreState(f"job {row['id']} disappeared during claim")
        _event(
            connection,
            row["id"],
            "claimed",
            {"worker_id": worker_id, "lease_expires_at": expires, "attempt": attempt, "run_id": run_id},
        )
        connection.commit()
        return JobClaim(claimed_job, worker_id, expires)


def heartbeat(job_id: str, worker_id: str, *, run_id: str | None = None, now: float | None = None, lease_seconds: int = 300, path: str | Path | None = None) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise InvalidStoreState("worker id is required")
    if not isinstance(run_id, str) or not run_id.strip():
        raise InvalidStoreState("run id is required")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 86400:
        raise InvalidStoreState("lease_seconds must be between 1 and 86400")
    if now is not None:
        _finite_float(now, "heartbeat time")
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        lease_now = _now_epoch()
        _require_live_lease(connection, job_id, worker_id, run_id, now=lease_now)
        fresh_now = _now_epoch()
        expires = fresh_now + lease_seconds
        result = connection.execute(
            "UPDATE jobs SET lease_expires_at=?, updated_at=? WHERE id=? AND status='running' "
            "AND lease_owner=? AND run_id=? AND lease_expires_at > ?",
            (expires, _now_iso(), job_id, worker_id, run_id, fresh_now),
        )
        if result.rowcount != 1:
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        _event(connection, job_id, "heartbeat", {"worker_id": worker_id, "lease_expires_at": expires})
        connection.commit()
        updated = _row_to_job(
            connection,
            connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(),
            allow_active_all_terminal=True,
        )
        if updated is None:
            raise InvalidStoreState(f"job {job_id} disappeared after heartbeat")
        return updated


def update_stage(
    job_id: str,
    stage_name: str,
    status: str,
    *,
    worker_id: str,
    run_id: str,
    artifact: Any = None,
    error_code: str | None = None,
    error_message: str | None = None,
    progress: float | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    stage_name = _validate_current_stage(stage_name)
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise InvalidStoreState("worker id is required")
    if not isinstance(run_id, str) or not run_id.strip():
        raise InvalidStoreState("run id is required")
    if status not in {"pending", "running", "succeeded", "failed", "skipped"}:
        raise InvalidStoreState("unknown stage status")
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        lease_now = _now_epoch()
        job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            connection.rollback()
            raise InvalidStoreState(f"job {job_id} does not exist")
        _validate_persisted_lifecycle_row(job)
        _validate_terminal_stage_consistency(connection, job)
        _stage_rows(connection, job_id)
        if (
            job["status"] != "running"
            or job["lease_owner"] != worker_id
            or job["run_id"] != run_id
            or job["lease_expires_at"] is None
            or job["lease_expires_at"] <= lease_now
        ):
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        stage = connection.execute(
            "SELECT * FROM job_stages WHERE job_id=? AND stage_name=?", (job_id, stage_name)
        ).fetchone()
        if stage is None:
            connection.rollback()
            raise InvalidStoreState(f"stage {stage_name} is not configured for job {job_id}")
        _validate_persisted_stage_row(stage)
        ordered_stages = connection.execute(
            "SELECT stage_name, status FROM job_stages WHERE job_id=? ORDER BY rowid", (job_id,)
        ).fetchall()
        stage_index = next(index for index, item in enumerate(ordered_stages) if item["stage_name"] == stage_name)
        prior_statuses = [item["status"] for item in ordered_stages[:stage_index]]
        current_index = next(
            (index for index, item in enumerate(ordered_stages) if item["stage_name"] == job["current_stage"]),
            None,
        )
        if status == "pending" and stage_index != current_index:
            connection.rollback()
            raise InvalidStoreState(f"stage {stage_name} cannot become pending outside the current stage")
        if status == "running" and any(item not in {"succeeded", "skipped"} for item in prior_statuses):
            connection.rollback()
            raise InvalidStoreState(f"stage {stage_name} cannot run before prior stages complete")
        if status in {"succeeded", "skipped"} and any(item not in {"succeeded", "skipped"} for item in prior_statuses):
            connection.rollback()
            raise InvalidStoreState(f"stage {stage_name} cannot complete before prior stages complete")
        allowed_transitions = {
            "pending": {"pending", "running", "succeeded", "skipped"},
            "running": {"running", "succeeded", "failed", "skipped"},
            "failed": {"running"},
            "succeeded": set(),
            "skipped": set(),
        }
        if status not in allowed_transitions[stage["status"]]:
            connection.rollback()
            raise InvalidStoreState(f"invalid stage transition {stage['status']} -> {status}")
        if progress is not None:
            progress = _payload_float(progress, "progress", 0.0, 1.0)
            if progress < job["progress"]:
                connection.rollback()
                raise InvalidStoreState("job progress cannot decrease")
        stage_attempt = stage["attempt"] + (1 if status == "running" else 0)
        stage_timestamp = _now_iso() if status in {"running", "succeeded", "failed", "skipped"} else None
        started_at = stage_timestamp if status == "running" else (
            stage["started_at"] if stage["started_at"] is not None else stage_timestamp
        )
        completed_at = stage_timestamp if status in {"succeeded", "failed", "skipped"} else None
        connection.execute(
            "UPDATE job_stages SET status=?, attempt=?, started_at=?, completed_at=?, error_code=?, "
            "error_message=?, artifact_json=? WHERE job_id=? AND stage_name=?",
            (status, stage_attempt, started_at, completed_at, error_code, (error_message or "")[:1000], _json(artifact) if artifact is not None else None, job_id, stage_name),
        )
        next_stage_name = stage_name
        if status in {"succeeded", "skipped"} and stage_index + 1 < len(ordered_stages):
            next_stage_name = ordered_stages[stage_index + 1]["stage_name"]
        updates = ["current_stage=?", "updated_at=?"]
        values: list[Any] = [next_stage_name, _now_iso()]
        if progress is not None:
            updates.append("progress=?")
            values.append(float(progress))
        fresh_lease_now = _now_epoch()
        values.append(job_id)
        values.append(worker_id)
        values.append(run_id)
        values.append(fresh_lease_now)
        result = connection.execute(
            f"UPDATE jobs SET {', '.join(updates)} WHERE id=? AND status='running' "
            "AND lease_owner=? AND run_id=? AND lease_expires_at > ?",
            values,
        )
        if result.rowcount != 1:
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        _event(connection, job_id, "stage_" + status, {"stage": stage_name, "error_code": error_code})
        connection.commit()
        updated = _row_to_job(
            connection,
            connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(),
            allow_active_all_terminal=True,
        )
        if updated is None:
            raise InvalidStoreState(f"job {job_id} disappeared after stage update")
        return updated


def complete_job(job_id: str, worker_id: str, *, run_id: str | None = None, path: str | Path | None = None) -> dict[str, Any]:
    raise InvalidStoreState("completed jobs require descriptor-bound final media publication")


def _finish_job(job_id: str, worker_id: str, *, run_id: str | None = None, status: str, path: str | Path | None = None) -> dict[str, Any]:
    if status != "failed":
        raise InvalidStoreState("_finish_job only supports the failed terminal transition")
    job_id = _validate_job_id(job_id)
    if not isinstance(run_id, str) or not run_id.strip():
        raise InvalidStoreState("run id is required")
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        lease_now = _now_epoch()
        _require_live_lease(connection, job_id, worker_id, run_id, now=lease_now)
        fresh_lease_now = _now_epoch()
        mutation_at = _now_iso()
        result = connection.execute(
            "UPDATE jobs SET status=?, progress=?, current_stage='done', completed_at=?, updated_at=?, "
            "lease_owner=NULL, lease_expires_at=NULL WHERE id=? AND status='running' AND lease_owner=? AND run_id=? AND lease_expires_at > ?",
            (status, 1.0 if status == "completed" else 0.0, mutation_at if status == "completed" else None, mutation_at, job_id, worker_id, run_id, fresh_lease_now),
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
    run_id: str | None = None,
    now: float | None = None,
    backoff_seconds: float | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    if not isinstance(run_id, str) or not run_id.strip():
        raise InvalidStoreState("run id is required")
    if now is not None:
        _finite_float(now, "now")
    validated_delay = None if backoff_seconds is None else _finite_float(backoff_seconds, "backoff_seconds", 0.0, 3600.0)
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        fresh_now = _finite_float(_now_epoch(), "current time")
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        _validate_persisted_lifecycle_row(row)
        if row is None or row["status"] != "running" or row["lease_owner"] != worker_id or row["run_id"] != run_id or row["lease_expires_at"] is None or row["lease_expires_at"] <= fresh_now:
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        can_retry = bool(retryable) and row["retry_count"] < row["max_retries"] and row["cancelled_at"] is None
        next_at = None
        if can_retry:
            delay = validated_delay if validated_delay is not None else min(3600.0, 2.0 ** row["retry_count"])
            next_at = fresh_now + delay
        new_status = "queued" if can_retry else "failed"
        final_lease_now = _finite_float(_now_epoch(), "current time")
        mutation_at = _now_iso()
        result = connection.execute(
            "UPDATE jobs SET status=?, retry_count=retry_count+?, next_attempt_at=?, error_code=?, "
            "error_message=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL, completed_at=?, run_id=? "
            "WHERE id=? AND status='running' AND lease_owner=? AND run_id=? AND lease_expires_at > ?",
            (new_status, 1 if can_retry else 0, next_at, str(error_code)[:128], str(error_message)[:1000], mutation_at, None if can_retry else mutation_at, None if can_retry else row["run_id"], job_id, worker_id, run_id, final_lease_now),
        )
        if result.rowcount != 1:
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        _event(connection, job_id, "retry_scheduled" if can_retry else "failed", {"error_code": error_code, "retryable": bool(retryable), "next_attempt_at": next_at})
        connection.commit()
        return _row_to_job(connection, connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def cancel_job(job_id: str, *, owner_id: str, reason: str = "cancelled by user", path: str | Path | None = None) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    owner_id = _validate_owner(owner_id)
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        existing = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if existing is None:
            connection.rollback()
            raise InvalidStoreState(f"job {job_id} does not exist")
        _validate_persisted_lifecycle_row(existing)
        if existing["owner_id"] != owner_id:
            connection.rollback()
            raise LeaseLost(f"job owner mismatch for {job_id}")
        mutation_at = _now_iso()
        result = connection.execute(
            "UPDATE jobs SET status='cancelled', cancelled_at=?, updated_at=?, lease_owner=NULL, "
            "lease_expires_at=NULL, error_code='cancelled', error_message=? "
            "WHERE id=? AND owner_id=? AND status NOT IN ('completed', 'editor_package', 'failed', 'cancelled')",
            (mutation_at, mutation_at, str(reason)[:1000], job_id, owner_id),
        )
        if result.rowcount:
            _event(connection, job_id, "cancelled", {"reason": reason})
        candidate = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        _row_to_job(connection, candidate)
        connection.commit()
        job = _row_to_job(connection, candidate)
        if job is None:
            raise InvalidStoreState(f"job {job_id} does not exist")
        return job


def is_cancelled(job_id: str, path: str | Path | None = None) -> bool:
    job_id = _validate_job_id(job_id)
    initialize(path)
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return False
        _validate_persisted_lifecycle_row(row)
        return row["status"] == "cancelled"


def update_fields(
    job_id: str,
    fields: dict[str, Any],
    *,
    owner_id: str | None = None,
    worker_id: str | None = None,
    run_id: str | None = None,
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
    if owner_id is not None:
        owner_id = _validate_owner(owner_id)
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
        _validate_persisted_lifecycle_row(row)
        if owner_id is not None and row["owner_id"] != owner_id:
            connection.rollback()
            raise LeaseLost(f"job owner mismatch for {job_id}")
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
        if worker_id is None and owner_id is None and row["status"] in {"initializing", "queued", "running"}:
            connection.rollback()
            raise InvalidStoreState(f"active job mutation requires owner or worker identity for {job_id}")
        if row["status"] in {"completed", "editor_package", "failed", "cancelled"}:
            connection.rollback()
            raise InvalidStoreState("terminal jobs are immutable")
        if (
            "status" in fields
            and fields["status"] in {"completed", "editor_package", "failed", "cancelled"}
            and row["status"] == "running"
            and worker_id is None
        ):
            connection.rollback()
            raise LeaseLost("terminal transition from a running job requires an active worker lease")
        if worker_id is not None and (
            not isinstance(run_id, str)
            or not run_id.strip()
            or row["status"] != "running"
            or row["lease_owner"] != worker_id
            or row["run_id"] != run_id
            or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= _finite_float(_now_epoch(), "current time")
        ):
            connection.rollback()
            raise LeaseLost(f"worker lease lost for job {job_id}")
        normalized = dict(fields)
        if "stage" in normalized and "current_stage" not in normalized:
            normalized["current_stage"] = normalized.pop("stage")
        if "error" in normalized and "error_message" not in normalized:
            normalized["error_message"] = normalized.pop("error")
        if "status" in normalized and normalized["status"] not in {
            "initializing", "queued", "running", "completed", "editor_package", "failed", "cancelled",
        }:
            raise InvalidStoreState("unsupported job status")
        if "progress" in normalized:
            normalized["progress"] = _payload_float(normalized["progress"], "progress", 0.0, 1.0)
            if normalized["progress"] < row["progress"]:
                raise InvalidStoreState("job progress cannot regress within an attempt")
        if "next_attempt_at" in normalized and normalized["next_attempt_at"] is not None:
            normalized["next_attempt_at"] = _payload_float(normalized["next_attempt_at"], "next_attempt_at", 0.0)
        if "retry_count" in normalized:
            retry_count = normalized["retry_count"]
            if isinstance(retry_count, bool) or not isinstance(retry_count, int) or not 0 <= retry_count <= 20:
                raise InvalidStoreState("retry_count must be an integer between 0 and 20")
        for timestamp_field in ("completed_at", "cancelled_at"):
            if timestamp_field in normalized and normalized[timestamp_field] is not None:
                normalized[timestamp_field] = _validate_legacy_timestamp(
                    normalized[timestamp_field], timestamp_field,
                )
        if "current_stage" in normalized:
            normalized["current_stage"] = _validate_current_stage(normalized["current_stage"])
        updates = {key: value for key, value in normalized.items() if key in allowed}
        payload_updates = {key: value for key, value in normalized.items() if key not in allowed}
        if "current_stage" in updates:
            requested_stage = updates["current_stage"]
            if requested_stage == "done":
                if updates.get("status") not in {"completed", "editor_package"}:
                    raise InvalidStoreState("done stage is only valid with a terminal package status")
            else:
                ordered_stages = connection.execute(
                    "SELECT stage_name, status FROM job_stages WHERE job_id=? ORDER BY rowid", (job_id,)
                ).fetchall()
                stage_names = [stage["stage_name"] for stage in ordered_stages]
                if requested_stage not in stage_names:
                    raise InvalidStoreState(f"stage {requested_stage} is not configured for job {job_id}")
                current_stage = row["current_stage"]
                if requested_stage != current_stage:
                    if current_stage not in stage_names:
                        raise InvalidStoreState(f"current stage {current_stage} is not configured for job {job_id}")
                    current_index = stage_names.index(current_stage)
                    requested_index = stage_names.index(requested_stage)
                    if requested_index != current_index + 1:
                        raise InvalidStoreState("job stages must advance one configured stage at a time")
                    if ordered_stages[current_index]["status"] not in {"succeeded", "skipped"}:
                        raise InvalidStoreState("next job stage requires the current stage to be terminal")
                    if any(
                        stage["status"] not in {"succeeded", "skipped"}
                        for stage in ordered_stages[:requested_index]
                    ):
                        raise InvalidStoreState("next job stage requires all prior stages to be terminal")
        payload = _load_json(row["input_json"], label="job input")
        if not isinstance(payload, dict):
            connection.rollback()
            raise InvalidStoreState("stored job input must be an object")
        _validate_payload_numbers(payload)
        protected_evidence = {
            "final_video_sha256", "final_video_duration_seconds", "final_video_plan_sha256",
        }
        if any(key in protected_evidence for key in payload_updates):
            raise InvalidStoreState("final media evidence may only be written by publish_final_media")
        payload.update(payload_updates)
        _validate_payload_numbers(payload)
        if updates.get("status") in {"completed", "editor_package"}:
            if (
                row["status"] != "running"
                or not isinstance(updates.get("completed_at"), str)
                or updates.get("current_stage") != "done"
                or updates.get("progress") != 1.0
            ):
                raise InvalidStoreState("terminal package status requires a running job, completion timestamp, and full progress")
            if updates["status"] == "completed":
                _validate_final_video_payload(payload)
            elif payload.get("package_status") != "editor_package":
                raise InvalidStoreState("editor_package status requires editor package evidence")
            else:
                from package_utils import (
                    STATUS_EDITOR_PACKAGE,
                    _safe_regular_file,
                    compute_package_status,
                    read_json_object,
                    resolve_current_attempt_output_dir,
                )

                output_dir = Path(row["output_dir"])
                _validate_connection_output_dir(connection, output_dir, job_id=job_id)
                attempt_job = dict(payload)
                attempt_job.update({
                    "id": job_id,
                    "output_dir": str(output_dir),
                    "run_id": row["run_id"],
                    "status": "editor_package",
                })
                attempt_dir = resolve_current_attempt_output_dir(output_dir.parent, attempt_job)
                if attempt_dir is None:
                    raise InvalidStoreState("editor_package status requires a current attempt")
                manifest_path = attempt_dir / "package_manifest.json"
                if not _safe_regular_file(attempt_dir, manifest_path):
                    raise InvalidStoreState("editor_package status requires a regular package manifest")
                try:
                    manifest = read_json_object(manifest_path, lock=False)
                    package = compute_package_status(attempt_dir, job_status="editor_package")
                except (OSError, TypeError, ValueError, KeyError) as exc:
                    raise InvalidStoreState("editor package evidence could not be verified") from exc
                if (
                    manifest.get("package_status") != STATUS_EDITOR_PACKAGE
                    or package.get("package_status") != STATUS_EDITOR_PACKAGE
                ):
                    raise InvalidStoreState("editor package artifacts are incomplete or inconsistent")
        if updates.get("status") == "cancelled":
            if not isinstance(updates.get("cancelled_at"), str) or updates.get("completed_at") is not None:
                raise InvalidStoreState("cancelled status requires only a cancellation timestamp")
        if updates.get("status") == "completed" and updates.get("cancelled_at") is not None:
            raise InvalidStoreState("completed status cannot have cancelled_at")
        if payload_updates:
            updates["input_json"] = _json(payload)
        mutation_now = _now_iso()
        updates["updated_at"] = mutation_now
        if updates.get("status") in {"completed", "editor_package", "failed", "cancelled"}:
            updates.setdefault("completed_at", mutation_now if updates["status"] != "cancelled" else None)
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
        where = "id=?"
        if worker_id is not None:
            final_lease_now = _finite_float(_now_epoch(), "current time")
            values.extend((worker_id, run_id, final_lease_now))
            where += " AND status='running' AND lease_owner=? AND run_id=? AND lease_expires_at > ?"
        result = connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE {where}", values)
        if result.rowcount != 1:
            connection.rollback()
            if worker_id is not None:
                raise LeaseLost(f"worker lease lost for job {job_id}")
            raise InvalidStoreState(f"job {job_id} disappeared during transition")
        candidate = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        _row_to_job(connection, candidate)
        _event(connection, job_id, "updated", {"fields": sorted(fields)})
        connection.commit()
        updated = _row_to_job(connection, candidate)
        if updated is None:
            raise InvalidStoreState(f"job {job_id} disappeared during transition")
        return updated


def reconcile_expired_leases(*, now: float | None = None, path: str | Path | None = None) -> int:
    requested_now = None if now is None else _finite_float(now, "lease reconciliation time")
    initialize(path)
    with connect(path) as connection:
        _transaction(connection)
        now = _now_epoch() if requested_now is None else requested_now
        expired_rows = connection.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
        changed = 0
        for expired in expired_rows:
            try:
                _validate_persisted_lifecycle_row(expired)
                _validate_connection_output_dir(connection, expired["output_dir"], job_id=expired["id"])
                _validate_terminal_stage_consistency(connection, expired)
                _stage_rows(connection, expired["id"])
            except InvalidStoreState:
                _terminalize_invalid_running_row(connection, expired)
                changed += 1
                continue
            if expired["lease_expires_at"] > now:
                continue
            retry_count = expired["retry_count"] + 1
            terminal = retry_count > expired["max_retries"]
            mutation_at = _now_iso()
            connection.execute(
                "UPDATE jobs SET status=?, retry_count=?, lease_owner=NULL, lease_expires_at=NULL, "
                "run_id=?, error_code='lease_expired', error_message=?, completed_at=?, updated_at=? WHERE id=?",
                (
                    "failed" if terminal else "queued",
                    retry_count,
                    expired["run_id"] if terminal else None,
                    "worker lease expired; retry limit reached" if terminal else "worker lease expired; queued for retry",
                    mutation_at if terminal else None,
                    mutation_at,
                    expired["id"],
                ),
            )
            _event(connection, expired["id"], "lease_expired", {"retry_count": retry_count, "terminal": terminal})
            changed += 1
        connection.commit()
        return changed


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
    max_age = _finite_float(max_age_seconds, "initialization age", 1.0, 86400.0)
    requested_now = None if now is None else _finite_float(now, "initialization reconciliation time")
    initialize(path)
    changed = 0
    with connect(path) as connection:
        _transaction(connection)
        now = _now_epoch() if requested_now is None else requested_now
        cutoff = now - max_age
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status='initializing' AND cancelled_at IS NULL"
        ).fetchall()
        for row in rows:
            stale = False
            invalid_state = False
            try:
                _validate_persisted_lifecycle_row(row)
                _validate_connection_output_dir(connection, row["output_dir"], job_id=row["id"])
                timestamp = datetime.fromisoformat(str(row["updated_at"]))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                stale = timestamp.timestamp() <= cutoff
            except (InvalidStoreState, TypeError, ValueError, OverflowError, OSError):
                stale = True
                invalid_state = True
            if not stale:
                continue
            message = (
                "persisted initialization state invalid; repair required"
                if invalid_state
                else "initialization abandoned before job became claimable"
            )
            mutation_at = _now_iso()
            result = connection.execute(
                "UPDATE jobs SET status='failed', current_stage='initialization', progress=0.0, "
                "error_code='initialization_abandoned', error_message=?, completed_at=?, updated_at=?, "
                "lease_owner=NULL, lease_expires_at=NULL WHERE id=? AND status='initializing' "
                "AND cancelled_at IS NULL",
                (message, mutation_at, mutation_at, row["id"]),
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
    return parsed.astimezone(timezone.utc).isoformat()


def _timestamp_epoch(value: str, label: str) -> float:
    normalized = _validate_legacy_timestamp(value, label)
    return datetime.fromisoformat(normalized).timestamp()


def _validate_legacy_record(
    key: Any,
    payload: Any,
    *,
    output_root: str | Path | None = None,
    output_roots: tuple[str | Path, ...] | None = None,
) -> dict[str, Any]:
    if not isinstance(key, str) or not key:
        raise InvalidStoreState("legacy job mapping keys must be non-empty strings")
    if not isinstance(payload, dict):
        raise InvalidStoreState("legacy job entries must be objects")
    if payload.get("id") != key:
        raise InvalidStoreState("legacy job mapping key must match embedded id")
    job_id = _validate_job_id(key)
    if job_id != key:
        raise InvalidStoreState("legacy job mapping key must be canonical")
    if "topic" in payload and not isinstance(payload["topic"], str):
        raise InvalidStoreState("legacy topic must be a string")
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
    _validate_payload_numbers(payload)
    created_at = _validate_legacy_timestamp(payload.get("created_at"), "created_at", default=_now_iso())
    updated_at = _validate_legacy_timestamp(payload.get("updated_at"), "updated_at", default=created_at)
    if _timestamp_epoch(created_at, "created_at") > _timestamp_epoch(updated_at, "updated_at"):
        raise InvalidStoreState("legacy created_at cannot be later than updated_at")
    max_retries = _validate_max_retries(payload.get("max_retries", DEFAULT_MAX_RETRIES))
    raw_status = payload.get("status", "queued")
    allowed_statuses = {"initializing", "queued", "running", "completed", "editor_package", "failed", "cancelled"}
    if not isinstance(raw_status, str) or raw_status not in allowed_statuses:
        raise InvalidStoreState("legacy job status is invalid")
    imported_status = "queued" if raw_status == "running" else raw_status
    if imported_status == "completed":
        try:
            _validate_final_video_payload(payload)
        except InvalidStoreState:
            imported_status = "editor_package"
    raw_progress = payload.get("progress", 1.0 if imported_status in {"completed", "editor_package"} else 0.0)
    if isinstance(raw_progress, bool) or not isinstance(raw_progress, (int, float)) or not (0.0 <= float(raw_progress) <= 1.0):
        raise InvalidStoreState("legacy job progress is invalid")
    current_stage = payload.get("stage", "done" if imported_status in {"completed", "editor_package"} else stages[0])
    if current_stage == "waiting":
        current_stage = "done" if imported_status in {"completed", "editor_package"} else stages[0]
    legacy_completed_stage = imported_status in {"completed", "editor_package"} and current_stage == "done"
    if not isinstance(current_stage, str) or (current_stage not in stages and not legacy_completed_stage):
        raise InvalidStoreState("legacy current stage is invalid")
    completed_at = _validate_legacy_timestamp(payload.get("completed_at"), "completed_at") if payload.get("completed_at") is not None else None
    cancelled_at = _validate_legacy_timestamp(payload.get("cancelled_at"), "cancelled_at") if payload.get("cancelled_at") is not None else None
    if imported_status in {"completed", "editor_package"} and completed_at is None:
        completed_at = updated_at
    if imported_status == "cancelled" and cancelled_at is None:
        cancelled_at = updated_at
    if imported_status in {"completed", "editor_package"} and cancelled_at is not None:
        raise InvalidStoreState("completed legacy job cannot have cancelled_at")
    if imported_status == "cancelled" and completed_at is not None:
        raise InvalidStoreState("cancelled legacy job cannot have completed_at")
    if imported_status in {"initializing", "queued"} and (completed_at is not None or cancelled_at is not None):
        raise InvalidStoreState("active legacy job cannot have a terminal timestamp")
    if imported_status == "failed" and cancelled_at is not None:
        raise InvalidStoreState("failed legacy job cannot have cancelled_at")
    if imported_status in {"completed", "editor_package"} and float(raw_progress) != 1.0:
        raise InvalidStoreState("terminal legacy job must have full progress")
    error_value = payload.get("error")
    if error_value is not None and not isinstance(error_value, str):
        raise InvalidStoreState("legacy error must be a string")
    raw_output = payload.get("output_dir")
    if raw_output is not None and not isinstance(raw_output, str):
        raise InvalidStoreState("legacy output_dir must be null or a string")
    if isinstance(raw_output, str) and not raw_output.strip():
        raise InvalidStoreState("legacy output_dir must be a non-empty string")
    if output_roots is not None:
        configured_output_roots = tuple(Path(root) for root in output_roots)
    else:
        configured_output_roots = (
            (Path(output_root),) if output_root is not None else (APP_DIR / "output",)
        )
    if not configured_output_roots:
        raise InvalidStoreState("legacy output roots must not be empty")
    output_dir = Path(raw_output) if raw_output is not None else configured_output_roots[0] / job_id
    expected_outputs = tuple(root.resolve() / job_id for root in configured_output_roots)
    if (
        not output_dir.is_absolute()
        or output_dir.is_symlink()
        or output_dir not in expected_outputs
        or output_dir.resolve() not in expected_outputs
    ):
        raise InvalidStoreState("legacy output_dir is outside the job output root")
    if imported_status == "completed":
        try:
            from package_utils import STATUS_EDITOR_PACKAGE, STATUS_FINAL_VIDEO_READY, compute_package_status

            package = compute_package_status(
                output_dir,
                job_status="completed",
                expected_final_sha256=payload["final_video_sha256"],
                expected_plan_sha256=payload["final_video_plan_sha256"],
                expected_final_duration_seconds=payload["final_video_duration_seconds"],
                expected_run_id=LEGACY_FLAT_RUN_ID,
            )
        except (OSError, TypeError, ValueError, InvalidStoreState, KeyError) as exc:
            raise InvalidStoreState("legacy completed video could not be verified") from exc
        if package.get("package_status") == STATUS_EDITOR_PACKAGE:
            imported_status = "editor_package"
        elif package.get("package_status") != STATUS_FINAL_VIDEO_READY:
            raise InvalidStoreState("legacy completed video artifacts are incomplete")
    if imported_status == "editor_package":
        if raw_status == "editor_package" and payload.get("package_status") != "editor_package":
            raise InvalidStoreState("legacy editor package status lacks package evidence")
        try:
            from package_utils import compute_package_status

            package = compute_package_status(
                output_dir,
                job_status="editor_package",
                expected_run_id=LEGACY_FLAT_RUN_ID,
            )
        except (OSError, TypeError, ValueError, InvalidStoreState) as exc:
            raise InvalidStoreState("legacy editor package could not be verified") from exc
        if package.get("package_status") != "editor_package":
            raise InvalidStoreState("legacy editor package artifacts are incomplete")
    safe_input = {key: payload[key] for key in LEGACY_INPUT_KEYS if key in payload}
    safe_input.setdefault("topic", "")
    if imported_status == "editor_package":
        safe_input["package_status"] = "editor_package"
    return {
        "id": job_id, "owner_id": owner, "idempotency_key": idempotency.strip() if idempotency else None,
        "status": imported_status, "stage": current_stage, "progress": float(raw_progress),
        "created_at": created_at, "updated_at": updated_at, "max_retries": max_retries,
        "error": error_value, "completed_at": completed_at, "cancelled_at": cancelled_at,
        "input_json": _json(safe_input), "output_dir": str(output_dir), "stages": stages,
        "run_id": LEGACY_FLAT_RUN_ID if imported_status in {"completed", "editor_package", "failed", "cancelled"} else None,
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
            records = [
                _validate_legacy_record(
                    key,
                    payload,
                    output_root=_output_root_for_database(db_path),
                    output_roots=(_output_root_for_database(db_path),),
                )
                for key, payload in loaded.items()
            ]
            with connect(db_path) as connection:
                _transaction(connection)
                count = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                if count:
                    connection.rollback()
                    raise InvalidStoreState("refusing legacy import into a populated job database")
                for record in records:
                    connection.execute(
                        "INSERT INTO jobs(id, owner_id, idempotency_key, status, current_stage, progress, created_at, updated_at, "
                        "max_retries, next_attempt_at, error_message, completed_at, cancelled_at, input_json, output_dir, run_id, stage_names_json) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (record["id"], record["owner_id"], record["idempotency_key"], record["status"], record["stage"], record["progress"], record["created_at"], record["updated_at"], record["max_retries"], None,
                         record["error"], record["completed_at"], record["cancelled_at"], record["input_json"], record["output_dir"], record["run_id"], _json(record["stages"])),
                    )
                    if record["status"] in {"completed", "editor_package"}:
                        imported_stage_status = "succeeded"
                        imported_stage_attempt = 1
                        imported_started_at = record["created_at"]
                        imported_completed_at = record["updated_at"]
                    elif record["status"] in {"initializing", "queued"}:
                        imported_stage_status = "pending"
                        imported_stage_attempt = 0
                        imported_started_at = None
                        imported_completed_at = None
                    else:
                        imported_stage_status = "skipped"
                        imported_stage_attempt = 1
                        imported_started_at = record["created_at"]
                        imported_completed_at = record["created_at"]
                    for stage in record["stages"]:
                        connection.execute(
                            "INSERT INTO job_stages(job_id, stage_name, status, attempt, started_at, completed_at) "
                            "VALUES(?, ?, ?, ?, ?, ?)",
                            (record["id"], stage, imported_stage_status, imported_stage_attempt, imported_started_at, imported_completed_at),
                        )
                    _event(connection, record["id"], "legacy_import", {})
                connection.commit()
                return len(loaded)
    except InvalidStoreState:
        raise
    except (OSError, ValueError) as exc:
        raise InvalidStoreState("legacy jobs.json could not be imported safely") from exc
