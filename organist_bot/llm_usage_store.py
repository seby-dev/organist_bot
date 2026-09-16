"""organist_bot/llm_usage_store.py
──────────────────────────────────────────────────
Per-call LLM usage tracking. Backed by data/llm_usage.json — a flat JSON
array, one record per completed litellm.acompletion call — so a spike in
usage, or an unexpected split across providers (e.g. after a failover), is
visible from chat rather than only inferable from each provider's own
billing dashboard days later.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from organist_bot import atomic_store

_PATH = Path("data/llm_usage.json")

_EMPTY_BUCKET = {
    "call_count": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "cached_tokens": 0,
    "cache_write_tokens": 0,
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read() -> list[dict]:
    return atomic_store.read_json(_PATH, [])


def record_call(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """Append one usage record. Raises on a write failure — callers on the hot
    path (process_message) wrap this in try/except so a tracking failure
    never breaks the chat turn that triggered it.

    `cached_tokens`/`cache_write_tokens` are prompt-cache read/write token
    counts (Anthropic cache_control hits, or a provider's own automatic
    caching) — 0 for a call that reported none, so caching visibility is
    additive and doesn't change behavior for callers that don't pass them."""
    with atomic_store.file_lock(_PATH):
        records = _read()
        records.append(
            {
                "timestamp": _now_iso(),
                "provider": provider,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
            }
        )
        atomic_store.write_json(_PATH, records, lock=False)


def summary(*, since: datetime.datetime | None = None) -> dict[str, dict[str, int]]:
    """Return per-provider totals (call_count, prompt_tokens, completion_tokens,
    total_tokens, cached_tokens, cache_write_tokens), optionally restricted to
    records at/after `since` (a timezone-aware UTC datetime). A record with a
    missing/unparseable timestamp is excluded from a `since`-restricted
    summary (it can't be placed in time) but always counted in the
    unrestricted one."""
    result: dict[str, dict[str, int]] = {}
    for r in _read():
        if since is not None:
            try:
                ts = datetime.datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.UTC
                )
            except (KeyError, ValueError):
                continue
            if ts < since:
                continue
        bucket = result.setdefault(r.get("provider", "unknown"), dict(_EMPTY_BUCKET))
        bucket["call_count"] += 1
        bucket["prompt_tokens"] += r.get("prompt_tokens", 0)
        bucket["completion_tokens"] += r.get("completion_tokens", 0)
        bucket["total_tokens"] += r.get("total_tokens", 0)
        bucket["cached_tokens"] += r.get("cached_tokens", 0)
        bucket["cache_write_tokens"] += r.get("cache_write_tokens", 0)
    return result
