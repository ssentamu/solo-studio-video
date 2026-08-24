"""Initialize durable runtime state before API and worker startup."""
import os
from pathlib import Path

import job_store


def main() -> None:
    database = Path(os.environ["SOLO_STUDIO_DATABASE_FILE"])
    legacy_value = os.environ.get("SOLO_STUDIO_JOBS_FILE")
    job_store.initialize(database)
    if not legacy_value:
        return
    legacy = Path(legacy_value)
    if legacy.is_symlink():
        raise RuntimeError("configured legacy jobs file must not be a symlink")
    if not legacy.exists():
        return
    try:
        job_store.import_jobs_json_once(legacy, path=database)
    except job_store.InvalidStoreState as exc:
        if "populated" not in str(exc):
            raise


if __name__ == "__main__":
    main()
