# NEG-Gig Draft & Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Instead of silently rejecting gigs whose fee is `"NEG"`/`"Negotiable"`, draft a negotiation email proposing a configurable fee (default £120) and send it to Telegram for approval before any email is sent to the recipient.

**Architecture:** Detection (`is_negotiable`) lives in `filters.py`. When `ENABLE_NEG_DRAFTS=true`, `FeeFilter` is removed from both `pre_filter` and `filter_chain` in `main.py`; surviving gigs are explicitly partitioned into normal / NEG / drop based on `parse_min_fee` + `is_negotiable`. NEG gigs get a rendered `negotiation.html.j2` draft persisted to `applications.json` as a new `neg_pending` status, plus a one-way Telegram alert. The unified Telegram agent gains four tools (`list_neg_pending`, `approve_neg_application`, `edit_neg_application`, `reject_neg_application`) for the user to act on the draft via chat. `expire_past_applied` is extended to flip past-date `neg_pending` rows to `expired`.

**Tech Stack:** Python 3.13, pydantic-settings, Jinja2, pytest, anthropic SDK (existing unified_agent loop).

**Spec:** [docs/superpowers/specs/2026-06-09-neg-gig-draft-approval-design.md](../specs/2026-06-09-neg-gig-draft-approval-design.md)

---

## Task 1 — `is_negotiable` helper in `filters.py`

**Files:**
- Modify: `organist_bot/filters.py` (add module-level constant + function near `parse_min_fee` at line 21)
- Test: `tests/test_filters.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_filters.py`:

```python
from organist_bot.filters import is_negotiable


def test_is_negotiable_detects_NEG():
    assert is_negotiable("NEG") is True


def test_is_negotiable_detects_Negotiable_case_insensitive():
    assert is_negotiable("Negotiable") is True
    assert is_negotiable("negotiable") is True
    assert is_negotiable("NEGOTIABLE") is True


def test_is_negotiable_detects_NEG_with_surrounding_text():
    assert is_negotiable("Fee: NEG") is True
    assert is_negotiable("(negotiable)") is True


def test_is_negotiable_false_for_numeric_fee():
    assert is_negotiable("£120") is False
    assert is_negotiable("£80 - £120") is False
    assert is_negotiable("From £90") is False


def test_is_negotiable_false_for_expenses_only():
    assert is_negotiable("Expenses only") is False
    assert is_negotiable("Expenses") is False


def test_is_negotiable_false_for_blank():
    assert is_negotiable("") is False
    assert is_negotiable(None) is False
    assert is_negotiable("   ") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filters.py -k is_negotiable -v`

Expected: FAIL with `ImportError: cannot import name 'is_negotiable' from 'organist_bot.filters'`

- [ ] **Step 3: Add the constant and helper**

In `organist_bot/filters.py`, just after the imports (around line 14) add:

```python
# ── Negotiable-fee detection ──────────────────────────────────────────
# Shared with parse_min_fee's "neg|negotiable|expenses" regex, but narrower:
# only matches NEG / Negotiable so the pipeline can distinguish negotiable
# (worth drafting) from expenses-only (no money on offer).
_NEG_REGEX = re.compile(r"\b(neg|negotiable)\b", re.IGNORECASE)


def is_negotiable(fee_str: str | None) -> bool:
    """Return True if the fee string indicates a negotiable fee.

    Matches literal "NEG" or "Negotiable" (case-insensitive, whole word).
    Does NOT match "expenses only" / blank / numeric — those should still
    be rejected by FeeFilter.
    """
    if not fee_str:
        return False
    return bool(_NEG_REGEX.search(fee_str))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_filters.py -k is_negotiable -v`

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/filters.py tests/test_filters.py
git commit -m "feat(filters): add is_negotiable helper for NEG-fee detection"
```

---

## Task 2 — Settings fields + `manage_config` whitelist

**Files:**
- Modify: `organist_bot/config.py` (add 2 fields next to `min_fee`)
- Modify: `organist_bot/integrations/unified_agent.py` (extend `manage_config` ranges/defaults/schema enum at lines 466, 1641-1650, plus description at 449)
- Test: `tests/test_unified_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_unified_agent.py`:

```python
import json
import pytest

import organist_bot.runtime_config_store as rcs
from organist_bot.integrations.unified_agent import _HANDLERS


@pytest.mark.asyncio
async def test_manage_config_accepts_negotiable_fee_set(tmp_path, monkeypatch):
    monkeypatch.setattr(rcs, "_PATH", tmp_path / "runtime_config.json")
    handler = _HANDLERS["manage_config"]
    out = json.loads(await handler({"action": "set", "key": "negotiable_fee", "value": 150}, 1))
    assert "negotiable_fee set to 150" in out["result"]


@pytest.mark.asyncio
async def test_manage_config_get_shows_negotiable_fee(tmp_path, monkeypatch):
    monkeypatch.setattr(rcs, "_PATH", tmp_path / "runtime_config.json")
    handler = _HANDLERS["manage_config"]
    out = json.loads(await handler({"action": "get"}, 1))
    assert "negotiable_fee" in out["result"]


@pytest.mark.asyncio
async def test_manage_config_rejects_negotiable_fee_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(rcs, "_PATH", tmp_path / "runtime_config.json")
    handler = _HANDLERS["manage_config"]
    out = json.loads(await handler({"action": "set", "key": "negotiable_fee", "value": -5}, 1))
    assert "Invalid value" in out["result"]
```

Note: `_HANDLERS` is the existing module-level dict that `@_handler("name")` writes into. If the file calls it something else (e.g. `_handler_map`), use that — verify by reading `organist_bot/integrations/unified_agent.py` around the `_handler` decorator definition near line 635.

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -k negotiable_fee -v`

Expected: FAIL with "Unknown key 'negotiable_fee'" on the set test and missing key on get.

- [ ] **Step 3: Add Settings fields**

In `organist_bot/config.py`, find the existing `min_fee` field and add the two new fields right after it:

```python
    min_fee: int = 100
    negotiable_fee: int = 120
    enable_neg_drafts: bool = True
```

(Place these in the same section as the other fee/filter toggles. Keep alphabetical or proximity-based ordering consistent with the file.)

- [ ] **Step 4: Extend `manage_config`**

In `organist_bot/integrations/unified_agent.py`:

At line 466, change the enum to include `negotiable_fee`:

```python
                "key": {
                    "type": "string",
                    "enum": ["min_fee", "max_travel_minutes", "poll_minutes", "negotiable_fee"],
                    "description": "Required for set and reset actions.",
                },
```

At line 449, update the description string:

```python
        "description": (
            "Read or update runtime pipeline configuration. "
            "Editable keys: min_fee (int, ≥0), max_travel_minutes (int, 1–300), "
            "poll_minutes (int, 1–60), negotiable_fee (int, 0–100000). "
            "Changes take effect on the next polling tick. "
            "Use action='reset' to restore the .env default for a key."
        ),
```

At lines 1641-1650, extend the `_RANGES` and `_DEFAULTS` dicts:

```python
    _RANGES: dict[str, tuple[int, int]] = {
        "min_fee": (0, 100_000),
        "max_travel_minutes": (1, 300),
        "poll_minutes": (1, 60),
        "negotiable_fee": (0, 100_000),
    }
    _DEFAULTS = {
        "min_fee": settings.min_fee,
        "max_travel_minutes": settings.max_travel_minutes,
        "poll_minutes": settings.poll_minutes,
        "negotiable_fee": settings.negotiable_fee,
    }
```

At line 79, update the system-prompt examples block to mention the new key:

```python
- Editable keys: min_fee, max_travel_minutes, poll_minutes, negotiable_fee.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -k negotiable_fee -v`

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/config.py organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat(config): add NEGOTIABLE_FEE and ENABLE_NEG_DRAFTS settings"
```

---

## Task 3 — `negotiation.html.j2` template

**Files:**
- Create: `organist_bot/templates/negotiation.html.j2`
- Test: `tests/test_notifier.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_notifier.py`:

```python
from jinja2 import Environment, FileSystemLoader, select_autoescape

from organist_bot.notifier import TEMPLATES_DIR
from organist_bot.models import Gig


def _env():
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
    )


def _gig(**overrides):
    base = dict(
        header="St Mary's Sunday Service",
        organisation="St Mary's Church",
        locality="London",
        date="Sunday, July 12, 2026",
        time="10:00 AM",
        fee="NEG",
        link="https://example.com/g/123",
        contact="Jane Smith",
        email="jane@stmarys.org",
    )
    base.update(overrides)
    return Gig(**base)


def test_negotiation_template_includes_negotiable_fee():
    tmpl = _env().get_template("negotiation.html.j2")
    rendered = tmpl.render(
        gig=_gig(),
        applicant_name="Alex Organist",
        applicant_mobile="07700 900000",
        applicant_video_1="https://yt/v1",
        applicant_video_2="https://yt/v2",
        negotiable_fee=120,
    )
    assert "£120" in rendered
    assert "Jane Smith" in rendered
    assert "Sunday, July 12, 2026" in rendered
    assert "Alex Organist" in rendered
    assert "https://yt/v1" in rendered


def test_negotiation_template_falls_back_to_sir_madam_when_no_contact():
    tmpl = _env().get_template("negotiation.html.j2")
    rendered = tmpl.render(
        gig=_gig(contact=None),
        applicant_name="Alex",
        applicant_mobile="07700 900000",
        applicant_video_1="",
        applicant_video_2="",
        negotiable_fee=120,
    )
    assert "Sir/Madam" in rendered


def test_negotiation_template_omits_videos_section_when_empty():
    tmpl = _env().get_template("negotiation.html.j2")
    rendered = tmpl.render(
        gig=_gig(),
        applicant_name="Alex",
        applicant_mobile="07700 900000",
        applicant_video_1="",
        applicant_video_2="",
        negotiable_fee=120,
    )
    assert "Video 1" not in rendered
    assert "Video 2" not in rendered
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_notifier.py -k negotiation -v`

Expected: FAIL with `jinja2.exceptions.TemplateNotFound: negotiation.html.j2`

- [ ] **Step 3: Create the template**

Create `organist_bot/templates/negotiation.html.j2`:

```html
<!DOCTYPE html>
<html>
<body>
  <p>Dear {{ gig.contact or "Sir/Madam" }},</p>

  <p>I hope this email finds you well. My name is {{ applicant_name }}, and I am
    writing to express my interest in the position of organist for the {{ gig.date }}
    service, as advertised on organistonline.com.</p>

  <p>I have played the keyboard for two decades. Additionally, I have acquired
    considerable experience in the orthodox way of worship, owing to my Catholic
    background.</p>

  <p>I noticed that the fee is listed as negotiable. I would be happy to play this
    service for a fee of £{{ negotiable_fee }}, which I'm open to discussing further
    if that doesn't quite fit your budget.</p>

  {% if applicant_video_1 or applicant_video_2 %}
  <p>Please find below links to my videos showcasing my musical abilities:</p>
  <ul>
    {% if applicant_video_1 %}<li><a href="{{ applicant_video_1 }}">Video 1</a></li>{% endif %}
    {% if applicant_video_2 %}<li><a href="{{ applicant_video_2 }}">Video 2</a></li>{% endif %}
  </ul>
  {% endif %}

  <p>Kind regards,<br/>
    {{ applicant_name }}<br/>
    Mobile: {{ applicant_mobile }}</p>
</body>
</html>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_notifier.py -k negotiation -v`

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/templates/negotiation.html.j2 tests/test_notifier.py
git commit -m "feat(templates): add negotiation.html.j2 for NEG-fee applications"
```

---

## Task 4 — `Notifier.draft_negotiation` + `send_application_email` helper

**Files:**
- Modify: `organist_bot/notifier.py`
- Test: `tests/test_notifier.py`

This task extracts a module-level `send_application_email` so both `apply_to_gig` and the future agent approve-tool share one send path, and adds `Notifier.draft_negotiation` to render the draft without sending.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_notifier.py`:

```python
from organist_bot.notifier import (
    Notifier,
    FakeTransport,
    send_application_email,
)
from organist_bot.config import settings as _settings


def test_draft_negotiation_returns_subject_and_body():
    notifier = Notifier(_settings, FakeTransport())
    gig = _gig()  # helper from Task 3
    subject, body = notifier.draft_negotiation(gig, negotiable_fee=120)
    assert subject == f"Application for Organist Position – {gig.date}"
    assert "£120" in body
    assert "Jane Smith" in body


def test_draft_negotiation_uses_runtime_fee_value():
    notifier = Notifier(_settings, FakeTransport())
    gig = _gig()
    subject, body = notifier.draft_negotiation(gig, negotiable_fee=150)
    assert "£150" in body
    assert "£120" not in body


def test_send_application_email_dispatches_via_transport():
    transport = FakeTransport()
    send_application_email(
        transport=transport,
        settings=_settings,
        subject="Test Subject",
        body="<html><body>Hi</body></html>",
        recipient="recipient@example.com",
        cc=["cc@example.com"],
    )
    assert len(transport.sent) == 1
    sent = transport.sent[0]
    assert sent["sender"] == _settings.email_sender
    assert "recipient@example.com" in sent["recipients"]
    assert "cc@example.com" in sent["recipients"]
    assert "Test Subject" in sent["message"]


def test_send_application_email_without_cc():
    transport = FakeTransport()
    send_application_email(
        transport=transport,
        settings=_settings,
        subject="No CC",
        body="<html><body>Hi</body></html>",
        recipient="recipient@example.com",
        cc=None,
    )
    assert transport.sent[0]["recipients"] == ["recipient@example.com"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_notifier.py -k "draft_negotiation or send_application_email" -v`

Expected: FAIL with `ImportError: cannot import name 'send_application_email'` and `AttributeError: 'Notifier' object has no attribute 'draft_negotiation'`.

- [ ] **Step 3: Add `send_application_email` and `Notifier.draft_negotiation`; refactor `apply_to_gig`**

In `organist_bot/notifier.py`, after the `FakeTransport` class (around line 60), add a module-level helper:

```python
# ── Module-level send helper (shared by Notifier.apply_to_gig and the agent) ──


def send_application_email(
    *,
    transport: Transport,
    settings: Settings,
    subject: str,
    body: str,
    recipient: str,
    cc: list[str] | None = None,
) -> None:
    """Build and dispatch an application email via the given transport.

    Shared by Notifier.apply_to_gig (scheduler path) and the unified-agent
    approve-tool (Telegram path) so both routes use one MIME-build/dispatch.
    """
    msg = MIMEText(body, "html")
    msg["Subject"] = subject
    msg["From"] = settings.email_sender
    msg["To"] = recipient
    if cc:
        msg["Cc"] = ", ".join(cc)
    recipients = [recipient] + (cc or [])

    t0 = time.perf_counter()
    try:
        transport.send(settings.email_sender, recipients, msg.as_string())
    except Exception:
        logger.exception(
            "Email dispatch failed",
            extra={
                "subject": subject,
                "recipient": recipient,
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            },
        )
        raise
    logger.info(
        "Email dispatched",
        extra={
            "subject": subject,
            "recipient": recipient,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        },
    )
```

Then refactor `Notifier._dispatch` (currently lines 110-147) to delegate to `send_application_email`:

```python
    def _dispatch(
        self,
        subject: str,
        body: str,
        recipient: str,
        cc: list[str] | None = None,
    ) -> None:
        send_application_email(
            transport=self._transport,
            settings=self._settings,
            subject=subject,
            body=body,
            recipient=recipient,
            cc=cc,
        )
```

Remove the now-unused `_build_message` method (lines 94-108) — it duplicates what `send_application_email` does internally.

Add the new `draft_negotiation` method to `Notifier` (place near `apply_to_gig`, around line 171):

```python
    def draft_negotiation(self, gig: Gig, negotiable_fee: int) -> tuple[str, str]:
        """Render the NEG-fee application as (subject, body). Does NOT send.

        Returned strings are stored on the neg_pending application_store row
        and re-used verbatim when the user approves the draft in Telegram.
        """
        body = self._render(
            "negotiation.html.j2",
            gig=gig,
            applicant_name=self._settings.applicant_name,
            applicant_mobile=self._settings.applicant_mobile,
            applicant_video_1=self._settings.applicant_video_1,
            applicant_video_2=self._settings.applicant_video_2,
            negotiable_fee=negotiable_fee,
        )
        subject = f"Application for Organist Position – {gig.date}"
        return subject, body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_notifier.py -v`

Expected: all notifier tests pass (new + existing — refactor must not break `apply_to_gig`/`send_summary`).

- [ ] **Step 5: Commit**

```bash
git add organist_bot/notifier.py tests/test_notifier.py
git commit -m "feat(notifier): add draft_negotiation and send_application_email helper"
```

---

## Task 5 — `application_store` API for NEG-pending rows

**Files:**
- Modify: `organist_bot/application_store.py`
- Test: `tests/test_application_store.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_application_store.py`. The file already has an `autouse=True` fixture (`tmp_store`, around line 27) that monkeypatches `_PATH` to a tmp dir — your tests inherit it automatically, no extra fixture needed.

```python
import datetime
import hashlib

import organist_bot.application_store as application_store
from organist_bot.models import Gig


def _gig(link="https://example.com/g/abc"):
    return Gig(
        header="Test Gig",
        organisation="Test Org",
        locality="London",
        date="Sunday, July 12, 2026",
        time="10:00 AM",
        fee="NEG",
        link=link,
        contact="Jane",
        email="jane@example.com",
    )


def test_record_neg_pending_writes_row():
    gig = _gig()
    gig_id = application_store.record_neg_pending(
        gig, draft_subject="S", draft_body="<b>", negotiable_fee=120
    )
    expected = hashlib.sha256(gig.link.encode()).hexdigest()[:12]
    assert gig_id == expected
    rows = application_store.list_neg_pending()
    assert len(rows) == 1
    r = rows[0]
    assert r["gig_id"] == expected
    assert r["status"] == "neg_pending"
    assert r["draft_subject"] == "S"
    assert r["draft_body"] == "<b>"
    assert r["negotiable_fee"] == 120
    assert r["url"] == gig.link
    assert r["created_at"]
    assert r["decided_at"] is None
    assert r["decision"] is None


def test_record_neg_pending_is_idempotent_for_same_link():
    gig = _gig()
    id1 = application_store.record_neg_pending(gig, draft_subject="S", draft_body="b", negotiable_fee=120)
    id2 = application_store.record_neg_pending(gig, draft_subject="S2", draft_body="b2", negotiable_fee=130)
    assert id1 == id2
    rows = application_store.list_neg_pending()
    assert len(rows) == 1
    # First write wins (preserves the original draft the user is reviewing).
    assert rows[0]["draft_body"] == "b"


def test_list_neg_pending_returns_only_neg_pending_rows():
    application_store.record_neg_pending(_gig("https://e.com/1"), "S", "b", 120)
    application_store.record_application(_gig("https://e.com/2"))  # status=applied
    rows = application_store.list_neg_pending()
    assert len(rows) == 1
    assert rows[0]["url"] == "https://e.com/1"


def test_transition_neg_pending_to_applied_sets_applied_at():
    gig = _gig()
    gig_id = application_store.record_neg_pending(gig, "S", "draft body", 120)
    ok = application_store.transition_neg_pending(
        gig_id, to="applied", sent_body="final body"
    )
    assert ok is True
    rows = application_store._read()
    r = rows[0]
    assert r["status"] == "applied"
    assert r["decision"] == "applied"
    assert r["decided_at"]
    assert r["draft_body"] == "final body"  # overwritten on edit
    assert r["applied_at"]  # standard field set for get_income_forecast


def test_transition_neg_pending_to_rejected():
    gig = _gig()
    gig_id = application_store.record_neg_pending(gig, "S", "b", 120)
    ok = application_store.transition_neg_pending(gig_id, to="rejected")
    assert ok is True
    r = application_store._read()[0]
    assert r["status"] == "rejected"
    assert r["decision"] == "rejected"


def test_transition_neg_pending_idempotent_second_call_returns_false():
    gig = _gig()
    gig_id = application_store.record_neg_pending(gig, "S", "b", 120)
    assert application_store.transition_neg_pending(gig_id, to="applied") is True
    # Second call must not double-send / double-flip.
    assert application_store.transition_neg_pending(gig_id, to="rejected") is False
    r = application_store._read()[0]
    assert r["status"] == "applied"  # unchanged


def test_transition_neg_pending_unknown_id_returns_false():
    assert application_store.transition_neg_pending("deadbeefcafe", to="applied") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py -k "neg_pending or transition" -v`

Expected: FAIL with `AttributeError: module 'organist_bot.application_store' has no attribute 'record_neg_pending'`.

- [ ] **Step 3: Implement the three new functions**

In `organist_bot/application_store.py`, add these imports near the top:

```python
import hashlib
from typing import Literal
```

After `record_application` (around line 58) add:

```python
def _gig_id(link: str) -> str:
    """Deterministic short id derived from the gig URL."""
    return hashlib.sha256(link.encode()).hexdigest()[:12]


def record_neg_pending(
    gig: Gig,
    *,
    draft_subject: str,
    draft_body: str,
    negotiable_fee: int,
) -> str:
    """Write a new 'neg_pending' record. Returns the gig_id.

    Idempotent: if a row for this gig URL already exists in any state
    (neg_pending or otherwise), returns the existing gig_id without modifying
    the row — the original draft the user is reviewing is preserved.
    """
    gig_id = _gig_id(gig.link)
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("url") == gig.link:
                return gig_id
        now = _now_iso()
        records.append(
            {
                "gig_id": gig_id,
                "url": gig.link,
                "header": gig.header or "",
                "organisation": gig.organisation or "",
                "date": gig.date or "",
                "time": gig.time or "",
                "fee": gig.fee or "",
                "email": gig.email or "",
                "postcode": gig.postcode or "",
                "status": "neg_pending",
                "draft_subject": draft_subject,
                "draft_body": draft_body,
                "negotiable_fee": negotiable_fee,
                "created_at": now,
                "updated_at": now,
                "decided_at": None,
                "decision": None,
            }
        )
        _write(records)
    return gig_id


def list_neg_pending() -> list[dict]:
    """Return all records with status == 'neg_pending'."""
    return [r for r in _read() if r.get("status") == "neg_pending"]


def transition_neg_pending(
    gig_id: str,
    *,
    to: Literal["applied", "rejected", "expired"],
    sent_body: str | None = None,
) -> bool:
    """Transition a neg_pending row to applied/rejected/expired.

    Returns False if no neg_pending row with this gig_id exists (already
    transitioned, never existed, or in a different state) — caller should
    treat False as "already decided" and not double-send.

    On to='applied' the standard 'applied_at' field is set so downstream
    tools (get_income_forecast, manage_applications) see this like any
    other application. If sent_body is provided, draft_body is overwritten
    (for the edit case).
    """
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("gig_id") != gig_id:
                continue
            if r.get("status") != "neg_pending":
                return False
            now = _now_iso()
            r["status"] = to
            r["decision"] = to
            r["decided_at"] = now
            r["updated_at"] = now
            if to == "applied":
                r["applied_at"] = now
                if sent_body is not None:
                    r["draft_body"] = sent_body
            _write(records)
            return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py -k "neg_pending or transition" -v`

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/application_store.py tests/test_application_store.py
git commit -m "feat(application_store): add NEG-pending state + transition API"
```

---

## Task 6 — Extend `expire_past_applied` to expire `neg_pending` rows

**Files:**
- Modify: `organist_bot/application_store.py` (the `expire_past_applied` function at line 151)
- Test: `tests/test_application_store.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_application_store.py`:

```python
def test_expire_past_neg_pending_flips_to_expired(monkeypatch):
    # Create a neg_pending row with a past date.
    past = (datetime.date.today() - datetime.timedelta(days=5)).strftime("%A, %B %d, %Y")
    gig = _gig()
    gig.date = past
    application_store.record_neg_pending(gig, draft_subject="S", draft_body="b", negotiable_fee=120)
    changed = application_store.expire_past_applied()
    assert changed >= 1
    r = application_store._read()[0]
    assert r["status"] == "expired"


def test_expire_does_not_flip_future_neg_pending():
    future = (datetime.date.today() + datetime.timedelta(days=30)).strftime("%A, %B %d, %Y")
    gig = _gig()
    gig.date = future
    application_store.record_neg_pending(gig, draft_subject="S", draft_body="b", negotiable_fee=120)
    application_store.expire_past_applied()
    r = application_store._read()[0]
    assert r["status"] == "neg_pending"


def test_expire_still_flips_past_applied_to_no_response():
    # Regression: extending the function must not break the existing behavior.
    past = (datetime.date.today() - datetime.timedelta(days=5)).strftime("%A, %B %d, %Y")
    gig = _gig()
    gig.date = past
    application_store.record_application(gig)
    application_store.expire_past_applied()
    r = application_store._read()[0]
    assert r["status"] == "no_response"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py -k expire -v`

Expected: the two NEG tests fail (status stays `neg_pending`). The regression test should still pass.

- [ ] **Step 3: Extend `expire_past_applied`**

Replace the existing body of `expire_past_applied` (line 151) with:

```python
def expire_past_applied() -> int:
    """Mark past-date 'applied' rows as 'no_response' and past-date 'neg_pending'
    rows as 'expired'. Returns total count changed.
    """
    from organist_bot.filters import normalize_to_yyyymmdd

    today = datetime.date.today()
    with atomic_store.file_lock(_PATH):
        records = _read()
        changed = 0
        now = _now_iso()
        for r in records:
            status = r.get("status")
            if status not in ("applied", "neg_pending"):
                continue
            normalized = normalize_to_yyyymmdd(r.get("date", ""))
            if normalized is None:
                continue
            try:
                gig_date = datetime.datetime.strptime(normalized, "%Y%m%d").date()
            except ValueError:
                continue
            if gig_date < today:
                if status == "applied":
                    r["status"] = "no_response"
                else:  # neg_pending
                    r["status"] = "expired"
                    r["decision"] = "expired"
                    r["decided_at"] = now
                r["updated_at"] = now
                changed += 1
        if changed:
            _write(records)
    return changed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py -v`

Expected: all application_store tests pass.

- [ ] **Step 5: Commit**

```bash
git add organist_bot/application_store.py tests/test_application_store.py
git commit -m "feat(application_store): expire past-date neg_pending rows"
```

---

## Task 7 — Pipeline partitioning in `main.py`

**Files:**
- Modify: `main.py` (build chains without `FeeFilter` when `enable_neg_drafts`, partition `valid_gigs` after Phase 2, render drafts, record `neg_pending`, send Telegram alert)
- Test: `tests/test_main.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`. The existing file uses `MagicMock()` for settings and scraper plus `patch("main.X")` for collaborators — follow the same shape. Place these inside a new class `TestNegDrafts` at the bottom of the file:

```python
import datetime as _dt
from unittest.mock import MagicMock, patch

import main as main_module
import organist_bot.application_store as application_store


class TestNegDrafts:
    """Tests for the NEG-fee draft & approval pipeline branch."""

    def _settings(self, **overrides):
        s = MagicMock()
        s.target_url = "https://organistsonline.org/required/"
        s.min_fee = 100
        s.negotiable_fee = 120
        s.enable_neg_drafts = True
        s.poll_minutes = 2
        s.booked_dates = []
        s.home_postcode = ""
        s.google_maps_api_key = ""
        s.google_calendar_id = ""
        s.google_calendar_credentials_file = ""
        s.google_sheets_id = ""
        s.google_sheets_credentials_file = ""
        s.telegram_bot_token = "token"
        s.telegram_chat_id = "12345"
        s.email_password = "pass"
        s.email_sender = "bot@test.com"
        s.cc_email = ""
        s.applicant_name = "Alex"
        s.applicant_mobile = "07700 900000"
        s.applicant_video_1 = ""
        s.applicant_video_2 = ""
        s.enable_fee_filter = True
        s.enable_sunday_time_filter = False
        s.enable_blacklist_filter = False
        s.enable_booked_date_filter = False
        s.enable_seen_filter = False
        s.enable_postcode_filter = False
        s.enable_calendar_filter = False
        s.enable_availability_filter = False
        s.dry_run = False
        s.log_file = "/tmp/test.log"
        s.base_url = "https://example.com"
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def _future_sunday(self) -> str:
        today = _dt.date.today()
        days_ahead = (6 - today.weekday()) % 7 or 7  # next Sunday (always future)
        d = today + _dt.timedelta(days=days_ahead + 14)  # >14 days out
        return d.strftime("%A, %B %d, %Y")

    def _mock_scraper_with_one_gig(self, fee: str, link: str = "https://e.com/abc"):
        scraper = MagicMock()
        scraper.fetch.return_value = "<html/>"
        scraper.parse_gig_listings.return_value = [MagicMock()]
        scraper.extract_basic_details.return_value = {
            "header": "St Mary's Sunday Service",
            "organisation": "St Mary's",
            "locality": "London",
            "date": self._future_sunday(),
            "time": "10:00 AM",
            "link": link,
            "fee": fee,
        }
        scraper.extract_full_details.return_value = {
            "phone": "020 1234 5678",
            "contact": "Jane Smith",
            "email": "jane@stmarys.org",
            "address": "1 High St",
            "postcode": "SW1A 1AA",
        }
        return scraper

    def _patches(self, mock_settings, tmp_path):
        # monkeypatched application_store path so we don't write to data/applications.json
        application_store._PATH = tmp_path / "applications.json"
        return [
            patch("main.alert"),
            patch("main.settings", mock_settings),
            patch("organist_bot.notifier.application_store"),  # no real disk on send
            patch("main.load_seen_gigs", return_value=set()),
            patch("main.load_listings_hash", return_value="old_hash"),
            patch("main.save_listings_hash"),
            patch("main.save_seen_gigs"),
            patch("main.filter_store"),
            patch("main.SMTPTransport"),
            patch("main.set_run_id"),
            patch("main.runtime_config.get", side_effect=lambda k, d: d),
        ]

    def test_neg_gig_is_recorded_as_pending_and_alerts_telegram(self, tmp_path):
        mock_settings = self._settings()
        scraper = self._mock_scraper_with_one_gig(fee="NEG")
        from contextlib import ExitStack
        with ExitStack() as stack:
            patches = [stack.enter_context(p) for p in self._patches(mock_settings, tmp_path)]
            mock_alert = patches[0]
            main_module.main(scraper)

        rows = application_store.list_neg_pending()
        assert len(rows) == 1
        assert rows[0]["status"] == "neg_pending"
        assert "£120" in rows[0]["draft_body"]
        # Telegram alert with the gig_id.
        neg_calls = [
            c for c in mock_alert.send_alert.call_args_list
            if "NEG draft pending" in c.args[0]
        ]
        assert len(neg_calls) == 1
        assert rows[0]["gig_id"] in neg_calls[0].args[0]

    def test_below_min_fee_gig_is_not_drafted(self, tmp_path):
        mock_settings = self._settings()
        scraper = self._mock_scraper_with_one_gig(fee="£50")
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patches(mock_settings, tmp_path):
                stack.enter_context(p)
            main_module.main(scraper)
        assert application_store.list_neg_pending() == []

    def test_expenses_only_gig_is_not_drafted(self, tmp_path):
        mock_settings = self._settings()
        scraper = self._mock_scraper_with_one_gig(fee="Expenses only")
        from contextlib import ExitStack
        with ExitStack() as stack:
            patches = [stack.enter_context(p) for p in self._patches(mock_settings, tmp_path)]
            mock_alert = patches[0]
            main_module.main(scraper)
        assert application_store.list_neg_pending() == []
        for c in mock_alert.send_alert.call_args_list:
            assert "NEG draft pending" not in c.args[0]

    def test_enable_neg_drafts_false_rejects_neg(self, tmp_path):
        mock_settings = self._settings(enable_neg_drafts=False)
        scraper = self._mock_scraper_with_one_gig(fee="NEG")
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patches(mock_settings, tmp_path):
                stack.enter_context(p)
            main_module.main(scraper)
        assert application_store.list_neg_pending() == []
```

(One test from the spec — "NEG gig failing CalendarFilter is not drafted" — is omitted here because it requires a real CalendarFilter with a mocked Google Calendar client; this is exercised more cleanly in `tests/test_filters.py` via `CalendarFilter`'s own tests, and the partition logic already short-circuits on chain rejection. The four tests above cover the partition's decision matrix end-to-end.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_main.py -v`

Expected: NEG-related tests fail with `assert [] == [one row]` or similar — NEG path not implemented.

- [ ] **Step 3: Wire the partition into `main.py`**

In `main.py`, update the imports at line 14:

```python
from organist_bot.filters import (
    AvailabilityFilter,
    BlacklistFilter,
    CalendarFilter,
    FeeFilter,
    GigFilterChain,
    PostcodeFilter,
    SeenFilter,
    SundayTimeFilter,
    is_negotiable,
    parse_min_fee,
)
```

Around line 111-115, build `_fee_filter` exactly as today (we still need it for the explicit partition gate). Then add a flag for whether to include it in the chains:

```python
    _fee_filter = (
        FeeFilter(min_fee=runtime_config.get("min_fee", settings.min_fee))
        if settings.enable_fee_filter
        else None
    )
    # When NEG drafting is enabled we remove FeeFilter from BOTH chains so NEG
    # gigs survive past pre_filter (needed for the detail-page fetch that gets
    # us the contact email) and past filter_chain. The explicit partition gate
    # below Phase 2 then sorts them into normal / NEG / drop.
    _include_fee_in_chains = _fee_filter is not None and not settings.enable_neg_drafts
```

Change line 134-135 (pre_filter add):

```python
    if _include_fee_in_chains:
        pre_filter.add(_fee_filter)
```

Change lines 226-229 (filter_chain add):

```python
    if _include_fee_in_chains:
        filter_chain.add(_fee_filter)
    elif _fee_filter is None:
        logger.info("FeeFilter disabled")
    else:
        logger.info("FeeFilter excluded from chains — NEG drafting active")
```

After `valid_gigs = filter_chain.apply(gig_list)` (line 260), and BEFORE the "Filtering complete" log (line 262), insert the partition:

```python
    # ── Fee partition (only meaningful when enable_neg_drafts is True) ──────
    normal_gigs: list[Gig] = []
    neg_gigs: list[Gig] = []
    fee_dropped: list[str] = []  # link list for log breakdown

    if settings.enable_neg_drafts and _fee_filter is not None:
        for gig in valid_gigs:
            if _fee_filter(gig):
                normal_gigs.append(gig)
            elif is_negotiable(gig.fee):
                neg_gigs.append(gig)
            else:
                fee_dropped.append(gig.link)
        logger.info(
            "Fee partition applied",
            extra={
                "total_in": len(valid_gigs),
                "normal": len(normal_gigs),
                "neg": len(neg_gigs),
                "dropped": len(fee_dropped),
            },
        )
        valid_gigs = normal_gigs  # downstream code (Phase 3, seen-gigs save) only handles normal gigs
    # When enable_neg_drafts is False, FeeFilter was already in the chain so
    # valid_gigs is correct as-is and neg_gigs stays empty.
```

After the partition, between the "Filtering complete" log and Phase 3 (around line 270), draft, persist, and alert for each NEG gig:

```python
    # ── NEG drafts: render, persist, alert Telegram ────────────────────────
    # Notifier and SMTPTransport are already imported at the top of main.py.
    if neg_gigs and not dry_run:
        _neg_notifier = Notifier(settings, SMTPTransport(password=settings.email_password))
        _negotiable_fee = runtime_config.get("negotiable_fee", settings.negotiable_fee)
        _queued_ids: list[str] = []
        for gig in neg_gigs:
            if not gig.email:
                logger.warning(
                    "NEG draft skipped — no contact email",
                    extra={"header": gig.header, "link": gig.link},
                )
                continue
            try:
                subject, body = _neg_notifier.draft_negotiation(gig, negotiable_fee=_negotiable_fee)
                gig_id = application_store.record_neg_pending(
                    gig, draft_subject=subject, draft_body=body, negotiable_fee=_negotiable_fee,
                )
                _queued_ids.append(gig_id)
                _send_neg_alert(gig, gig_id, subject, body)
            except Exception:
                logger.exception(
                    "NEG draft failed for gig — skipping",
                    extra={"link": gig.link},
                )
        logger.info(
            "NEG drafts queued",
            extra={"count": len(_queued_ids), "gig_ids": _queued_ids},
        )
    elif neg_gigs and dry_run:
        logger.info(
            "Phase 3 — DRY-RUN: would draft NEG gigs",
            extra={"count": len(neg_gigs)},
        )
```

Define `_send_neg_alert` as a module-level helper in `main.py` (near the top, after the imports):

```python
import html as _html


def _send_neg_alert(gig: Gig, gig_id: str, subject: str, body: str) -> None:
    """One Telegram message per NEG draft with the body in plain text."""
    # Strip HTML tags crudely for Telegram. Production body is hand-written
    # Jinja2 so there's no scripts/styles to worry about.
    plain = _html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
    # Collapse runs of blank lines.
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    org = f" · {gig.organisation}" if gig.organisation else ""
    contact_line = (
        f"Contact: {gig.contact or '(none)'} <{gig.email}>"
        if gig.email
        else "Contact: (none)"
    )
    msg = (
        f"🟡 NEG draft pending — id: {gig_id}\n\n"
        f"Gig: {gig.header}{org} · {gig.date} · {gig.time}\n"
        f"{contact_line}\n\n"
        f"Subject: {subject}\n\n"
        f"{plain}\n\n"
        f"Reply:\n"
        f"  • \"approve {gig_id}\" to send as-is\n"
        f"  • \"edit {gig_id}: <new body>\" to send a revised version\n"
        f"  • \"reject {gig_id}\" to skip"
    )
    alert.send_alert(msg)
```

Add `import re` to the top of `main.py` if it isn't already imported.

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_main.py -v`

Expected: all main tests pass (new NEG tests + existing).

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat(pipeline): partition NEG gigs into draft queue, alert Telegram"
```

---

## Task 8 — Four `unified_agent` tools for approve/edit/reject/list

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py`
- Test: `tests/test_unified_agent.py`

This task adds four tools that all use the existing `confirmed=false/true` idiom from `add_gig`. Read the `add_gig` tool block (around line 798) for the existing pattern before implementing.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_unified_agent.py`:

```python
import json
from unittest.mock import patch
import pytest

import organist_bot.application_store as application_store
from organist_bot.integrations.unified_agent import _HANDLERS


def _seed_neg_pending(link="https://e.com/a"):
    from organist_bot.models import Gig
    gig = Gig(
        header="Test", organisation="Org", locality="London",
        date="Sunday, July 12, 2026", time="10:00 AM", fee="NEG",
        link=link, contact="Jane", email="jane@example.com",
    )
    return application_store.record_neg_pending(
        gig, draft_subject="Subject", draft_body="<p>Body</p>", negotiable_fee=120,
    )


@pytest.fixture(autouse=True)
def _app_store_tmp(tmp_path, monkeypatch):
    """Monkeypatch application_store._PATH for every NEG test."""
    monkeypatch.setattr(application_store, "_PATH", tmp_path / "applications.json")


@pytest.mark.asyncio
async def test_list_neg_pending_returns_pending_rows():
    gig_id = _seed_neg_pending()
    out = json.loads(await _HANDLERS["list_neg_pending"]({}, 1))
    assert gig_id in out["result"]
    assert "Test" in out["result"]


@pytest.mark.asyncio
async def test_approve_neg_application_without_confirmed_returns_preview():
    gig_id = _seed_neg_pending()
    out = json.loads(await _HANDLERS["approve_neg_application"]({"gig_id": gig_id}, 1))
    assert "confirm" in out["result"].lower()
    # Row still pending.
    assert application_store._read()[0]["status"] == "neg_pending"


@pytest.mark.asyncio
async def test_approve_neg_application_confirmed_sends_and_transitions():
    gig_id = _seed_neg_pending()
    with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
        out = json.loads(await _HANDLERS["approve_neg_application"]({"gig_id": gig_id, "confirmed": True}, 1))
    assert "sent" in out["result"].lower()
    mock_send.assert_called_once()
    r = application_store._read()[0]
    assert r["status"] == "applied"


@pytest.mark.asyncio
async def test_approve_unknown_gig_id_returns_error():
    out = json.loads(await _HANDLERS["approve_neg_application"]({"gig_id": "deadbeefcafe", "confirmed": True}, 1))
    assert "not found" in out["result"].lower() or "unknown" in out["result"].lower()


@pytest.mark.asyncio
async def test_approve_already_applied_returns_already_sent():
    gig_id = _seed_neg_pending()
    with patch("organist_bot.integrations.unified_agent.send_application_email"):
        await _HANDLERS["approve_neg_application"]({"gig_id": gig_id, "confirmed": True}, 1)
    # Second call.
    out = json.loads(await _HANDLERS["approve_neg_application"]({"gig_id": gig_id, "confirmed": True}, 1))
    assert "already" in out["result"].lower()


@pytest.mark.asyncio
async def test_edit_neg_application_confirmed_uses_new_body():
    gig_id = _seed_neg_pending()
    with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
        await _HANDLERS["edit_neg_application"](
            {"gig_id": gig_id, "new_body": "<p>EDITED</p>", "confirmed": True}, 1
        )
    sent_body = mock_send.call_args.kwargs["body"]
    assert "EDITED" in sent_body
    r = application_store._read()[0]
    assert r["status"] == "applied"
    assert "EDITED" in r["draft_body"]


@pytest.mark.asyncio
async def test_reject_neg_application_confirmed_skips_send():
    gig_id = _seed_neg_pending()
    with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
        out = json.loads(await _HANDLERS["reject_neg_application"]({"gig_id": gig_id, "confirmed": True}, 1))
    assert "rejected" in out["result"].lower() or "skipped" in out["result"].lower()
    mock_send.assert_not_called()
    r = application_store._read()[0]
    assert r["status"] == "rejected"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -k neg -v`

Expected: FAIL with `KeyError: 'list_neg_pending'` (handler not registered).

- [ ] **Step 3: Add four tool schemas to `TOOLS`**

In `organist_bot/integrations/unified_agent.py`, find the `TOOLS` list (line 99) and add these four schema entries in the same section style as `manage_applications`. Place them after `manage_applications` block, before `get_income_forecast`:

```python
    # ── NEG-fee drafts ──────────────────────────────────────────────────────
    {
        "name": "list_neg_pending",
        "description": (
            "List all NEG-fee application drafts awaiting user review. "
            "Returns gig_id, gig summary, and a draft-body preview for each. "
            "Use when the user asks about pending NEG drafts or wants to "
            "see what is awaiting their approval."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "approve_neg_application",
        "description": (
            "Approve a NEG-fee application draft and send it as-is to the "
            "gig contact. Two-step: first call (confirmed=false) returns the "
            "draft for the user to review; second call (confirmed=true) "
            "sends and transitions the row to 'applied'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {"type": "string", "description": "12-char id from the Telegram alert."},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["gig_id"],
        },
    },
    {
        "name": "edit_neg_application",
        "description": (
            "Edit a NEG-fee application draft and send the revised version. "
            "Provide either new_body (replace the whole HTML body) OR "
            "new_fee (re-render the template with a different £ amount). "
            "Two-step confirm pattern."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {"type": "string"},
                "new_body": {"type": "string", "description": "Replacement HTML body."},
                "new_fee": {"type": "integer", "description": "Re-render template with this fee."},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["gig_id"],
        },
    },
    {
        "name": "reject_neg_application",
        "description": (
            "Reject a NEG-fee application draft without sending. "
            "Transitions the row to 'rejected'. Two-step confirm pattern."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {"type": "string"},
                "confirmed": {"type": "boolean", "default": False},
            },
            "required": ["gig_id"],
        },
    },
```

- [ ] **Step 4: Register handlers and implement**

Add imports near the top of `unified_agent.py` (around line 34):

```python
from organist_bot.notifier import Notifier as _NEGNotifier
from organist_bot.notifier import SMTPTransport, send_application_email
```

Add the handlers near the other `@_handler(...)` functions (after `@_handler("manage_applications")` around line 1562, or anywhere consistent with surrounding tools):

```python
@_handler("list_neg_pending")
async def _handle_list_neg_pending(input_data: dict, chat_id: int) -> str:
    rows = application_store.list_neg_pending()
    if not rows:
        return json.dumps({"result": "No NEG drafts pending."})
    lines = [f"{len(rows)} NEG draft(s) pending:\n"]
    for r in rows:
        preview = (r.get("draft_body") or "")[:120].replace("\n", " ")
        lines.append(
            f"  • {r['gig_id']}  {r.get('date','?')}  {r.get('header','?')[:50]}"
            f"\n      → £{r.get('negotiable_fee')}  preview: {preview}..."
        )
    return json.dumps({"result": "\n".join(lines)})


def _find_neg_row(gig_id: str) -> dict | None:
    for r in application_store.list_neg_pending():
        if r.get("gig_id") == gig_id:
            return r
    return None


def _find_any_row_by_gig_id(gig_id: str) -> dict | None:
    """Search across all statuses — used to detect 'already decided'."""
    for r in application_store._read():
        if r.get("gig_id") == gig_id:
            return r
    return None


@_handler("approve_neg_application")
async def _handle_approve_neg(input_data: dict, chat_id: int) -> str:
    gig_id = input_data.get("gig_id", "")
    confirmed = input_data.get("confirmed", False)

    row = _find_neg_row(gig_id)
    if row is None:
        existing = _find_any_row_by_gig_id(gig_id)
        if existing is None:
            return json.dumps({"result": f"No draft found with id {gig_id}."})
        return json.dumps(
            {"result": f"Already {existing.get('status')} at {existing.get('decided_at') or existing.get('updated_at')}."}
        )

    if not confirmed:
        return json.dumps({
            "result": (
                f"Will send this draft to {row.get('email')}.\n\n"
                f"Subject: {row.get('draft_subject')}\n\n"
                f"{row.get('draft_body')}\n\n"
                f"Call again with confirmed=true to send."
            )
        })

    try:
        send_application_email(
            transport=SMTPTransport(password=settings.email_password),
            settings=settings,
            subject=row["draft_subject"],
            body=row["draft_body"],
            recipient=row["email"],
            cc=[settings.cc_email] if settings.cc_email else None,
        )
    except Exception as exc:
        logger.exception("NEG approve: send failed")
        return json.dumps({"result": f"Send failed: {exc}"})

    ok = application_store.transition_neg_pending(gig_id, to="applied", sent_body=row["draft_body"])
    if not ok:
        return json.dumps({"result": "Sent, but row state was unexpected — check applications.json."})
    logger.info("NEG application sent", extra={"details": {"gig_id": gig_id, "edited": False}})
    return json.dumps({"result": f"Sent ✅ to {row.get('email')}."})


@_handler("edit_neg_application")
async def _handle_edit_neg(input_data: dict, chat_id: int) -> str:
    gig_id = input_data.get("gig_id", "")
    new_body = input_data.get("new_body")
    new_fee = input_data.get("new_fee")
    confirmed = input_data.get("confirmed", False)

    row = _find_neg_row(gig_id)
    if row is None:
        existing = _find_any_row_by_gig_id(gig_id)
        if existing is None:
            return json.dumps({"result": f"No draft found with id {gig_id}."})
        return json.dumps({"result": f"Already {existing.get('status')}."})

    if new_body is None and new_fee is None:
        return json.dumps({"result": "Provide either new_body or new_fee."})

    if new_fee is not None:
        # Re-render the template with the new fee.
        from organist_bot.models import Gig as _Gig
        gig_kwargs = {k: row.get(k, "") for k in (
            "header", "organisation", "locality", "date", "time", "fee", "email", "postcode"
        )}
        gig_kwargs["link"] = row.get("url", "")
        gig_kwargs["contact"] = row.get("contact") or row.get("header", "")
        notifier = _NEGNotifier(settings, SMTPTransport(password=settings.email_password))
        _, rendered = notifier.draft_negotiation(_Gig(**gig_kwargs), negotiable_fee=int(new_fee))
        new_body = rendered

    if not confirmed:
        return json.dumps({
            "result": (
                f"Will send this edited draft to {row.get('email')}.\n\n"
                f"Subject: {row.get('draft_subject')}\n\n"
                f"{new_body}\n\n"
                f"Call again with confirmed=true to send."
            )
        })

    try:
        send_application_email(
            transport=SMTPTransport(password=settings.email_password),
            settings=settings,
            subject=row["draft_subject"],
            body=new_body,
            recipient=row["email"],
            cc=[settings.cc_email] if settings.cc_email else None,
        )
    except Exception as exc:
        logger.exception("NEG edit: send failed")
        return json.dumps({"result": f"Send failed: {exc}"})

    application_store.transition_neg_pending(gig_id, to="applied", sent_body=new_body)
    logger.info("NEG application sent", extra={"details": {"gig_id": gig_id, "edited": True}})
    return json.dumps({"result": f"Edited and sent ✅ to {row.get('email')}."})


@_handler("reject_neg_application")
async def _handle_reject_neg(input_data: dict, chat_id: int) -> str:
    gig_id = input_data.get("gig_id", "")
    confirmed = input_data.get("confirmed", False)

    row = _find_neg_row(gig_id)
    if row is None:
        existing = _find_any_row_by_gig_id(gig_id)
        if existing is None:
            return json.dumps({"result": f"No draft found with id {gig_id}."})
        return json.dumps({"result": f"Already {existing.get('status')}."})

    if not confirmed:
        return json.dumps({"result": f"Reject NEG draft for '{row.get('header')}'? Call again with confirmed=true to confirm."})

    application_store.transition_neg_pending(gig_id, to="rejected")
    logger.info("NEG application rejected", extra={"details": {"gig_id": gig_id}})
    return json.dumps({"result": "Draft rejected — no email sent."})
```

Verify the handler-registry symbol name (`_HANDLERS` vs `_handlers` etc.) by inspecting the `_handler` decorator definition near line 635 of `unified_agent.py`. If the symbol is different, fix both the test imports and the handlers will register correctly because they use the same `@_handler(...)` decorator.

Also update the system prompt block (around line 76-79) to mention the new tools. After the existing manage_config bullet, add:

```python
- "What NEG drafts are pending?" → list_neg_pending.
- "Approve NEG abc123" → approve_neg_application(gig_id=abc123, confirmed=false) — show draft, then confirmed=true.
- "Edit NEG abc123 fee to 150" → edit_neg_application(gig_id=abc123, new_fee=150, confirmed=false) — show, then confirmed=true.
- "Reject NEG abc123" → reject_neg_application(gig_id=abc123, confirmed=false) → confirmed=true.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -k neg -v`

Expected: all 7 NEG-related tests pass.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat(agent): add list/approve/edit/reject NEG-application tools"
```

---

## Task 9 — Documentation: `.env.example` and `CLAUDE.md`

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update `.env.example`**

In `.env.example`, find the existing `MIN_FEE` entry and add the two new vars right after it:

```dotenv
# Pipeline scraper / filter tuning
MIN_FEE=100
NEGOTIABLE_FEE=120
ENABLE_NEG_DRAFTS=true
```

- [ ] **Step 2: Update `CLAUDE.md`**

In `CLAUDE.md`, find the "Filters" section near the bottom and add a NEG-specific subsection at the end of it:

```markdown
### NEG-fee drafts

When `ENABLE_NEG_DRAFTS=true` (default), gigs whose fee is `"NEG"` or `"Negotiable"` are NOT rejected by `FeeFilter` — they're routed through every other filter, then a draft email proposing the value of `NEGOTIABLE_FEE` (default 120, runtime-overridable via the agent's `manage_config`) is rendered from `templates/negotiation.html.j2`, persisted to `applications.json` as `status: "neg_pending"`, and a Telegram alert with the draft + a 12-char `gig_id` is sent.

User approves/edits/rejects via Telegram chat:
- `approve <gig_id>` → unified_agent's `approve_neg_application` sends the draft as-is.
- `edit <gig_id>: <new body>` (or `edit <gig_id> fee 150`) → `edit_neg_application` re-renders / replaces and sends.
- `reject <gig_id>` → `reject_neg_application` transitions to `rejected` without sending.

Past-date `neg_pending` rows auto-flip to `expired` via `expire_past_applied`.

`ENABLE_NEG_DRAFTS=false` reverts to today's behavior (NEG gigs rejected by `FeeFilter`).
```

In the `## Configuration` section, add `NEGOTIABLE_FEE` to the **Scraper** bullet:

```markdown
- **Scraper** — `MIN_FEE` (default 100), `NEGOTIABLE_FEE` (default 120; proposed fee for NEG-flagged gigs), `POLL_MINUTES` (default 2), `TARGET_URL`, applicant fields (`APPLICANT_NAME`, `APPLICANT_MOBILE`, `APPLICANT_VIDEO_1/2`)
```

In the **Filter toggles** bullet, add `ENABLE_NEG_DRAFTS`:

```markdown
- **Filter toggles** — `ENABLE_FEE_FILTER`, `ENABLE_SUNDAY_TIME_FILTER`, `ENABLE_BLACKLIST_FILTER`, `ENABLE_SEEN_FILTER`, `ENABLE_POSTCODE_FILTER`, `ENABLE_CALENDAR_FILTER`, `ENABLE_AVAILABILITY_FILTER`, `ENABLE_NEG_DRAFTS` (all default `True`)
```

In the runtime_config_store paragraph just below, add `negotiable_fee`:

```markdown
`runtime_config_store` overrides `MIN_FEE`, `MAX_TRAVEL_MINUTES`, `POLL_MINUTES`, and `NEGOTIABLE_FEE` at runtime — the scheduler reads via `runtime_config.get(key, settings.foo)` so `.env` values are the fallback.
```

In the `## Data files` table, add a note to the `data/applications.json` row's purpose:

```markdown
| `data/applications.json` | Application lifecycle store (written by `application_store`); `neg_pending` rows hold unsent NEG drafts awaiting Telegram approval |
```

- [ ] **Step 3: Commit**

```bash
git add .env.example CLAUDE.md
git commit -m "docs: document NEG draft flow + new env vars in CLAUDE.md"
```

---

## Task 10 — Full verification

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q`

Expected: all tests pass. If anything fails, fix at the relevant task before continuing.

- [ ] **Step 2: Lint**

Run: `ruff check .`

Expected: no errors. Fix any.

- [ ] **Step 3: Format**

Run: `ruff format .`

If anything reformats, stage and commit.

- [ ] **Step 4: Type-check**

Run: `mypy organist_bot/`

Expected: no errors. Fix any.

- [ ] **Step 5: Final integration sanity check (optional — skip if no .env available)**

If a working `.env` exists with all required vars, run:

```bash
python main.py --dry-run
```

Expected: pipeline runs to completion, NEG gigs (if any in the listings) produce dry-run NEG draft logs without writing to `applications.json` or sending email/Telegram.

- [ ] **Step 6: Commit any final fixes from lint/format/mypy**

```bash
git add -A
git commit -m "chore: lint and format fixes for NEG draft feature"
```
