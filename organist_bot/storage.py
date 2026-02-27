# organist_bot/storage.py

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_seen_gigs(filepath: str = "data/seen_gigs.csv") -> set[str]:
    path = Path(filepath)
    if not path.exists():
        logger.info(
            "Seen-gigs file not found — starting fresh",
            extra={"filepath": str(path.resolve())},
        )
        return set()

    try:
        seen = set()
        with path.open(newline="") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if row:
                    seen.add(row[0])

        logger.info(
            "Loaded seen gigs",
            extra={"count": len(seen), "filepath": str(path.resolve())},
        )
        return seen
    except Exception:
        logger.exception(
            "Failed to load seen gigs — starting fresh",
            extra={"filepath": str(path.resolve())},
        )
        return set()


def save_seen_gigs(seen: set[str], filepath: str = "data/seen_gigs.csv") -> None:
    path = Path(filepath)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            for link in sorted(seen):
                writer.writerow([link])

        logger.info(
            "Saved seen gigs",
            extra={"count": len(seen), "filepath": str(path.resolve())},
        )
    except Exception:
        logger.exception(
            "Failed to save seen gigs",
            extra={"filepath": str(path.resolve()), "count": len(seen)},
        )
        raise
