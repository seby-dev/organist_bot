# organist_bot/storage.py

import csv
import io
import logging
from pathlib import Path

from organist_bot import atomic_store

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
        buf = io.StringIO()
        writer = csv.writer(buf)
        for link in sorted(seen):
            writer.writerow([link])
        atomic_store.write_text_atomic(path, buf.getvalue())

        logger.info(
            "Saved seen gigs",
            extra={"count": len(seen), "gigs_saved": seen, "filepath": str(path.resolve())},
        )
    except Exception:
        logger.exception(
            "Failed to save seen gigs",
            extra={"filepath": str(path.resolve()), "count": len(seen)},
        )
        raise


def load_listings_hash(filepath: str = "data/listings_hash.txt") -> str | None:
    path = Path(filepath)
    if not path.exists():
        return None
    try:
        return path.read_text().strip() or None
    except Exception:
        logger.exception("Failed to load listings hash", extra={"filepath": str(path)})
        return None


def save_listings_hash(hash_str: str, filepath: str = "data/listings_hash.txt") -> None:
    path = Path(filepath)
    try:
        atomic_store.write_text_atomic(path, hash_str)
    except Exception:
        logger.exception("Failed to save listings hash", extra={"filepath": str(path)})
        raise
