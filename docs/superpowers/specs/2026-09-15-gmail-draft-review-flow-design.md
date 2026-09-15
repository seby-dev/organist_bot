# Real Gmail drafts + Accept/Decline review flow, with an AI hold classifier

## Problem

Today, `neg_pending` (fee-negotiable) gigs already get a two-message Telegram
alert (gig details, then a draft with `Accept | Edit | Reject` buttons plus a
free-text fallback — `2026-07-26-neg-draft-buttons-design.md`), but the
"draft" is just `draft_subject`/`draft_body` text sitting in
`applications.json`. There's no real Gmail draft to review/tweak in the
user's own mail client, and Reject just flips a status — nothing external
exists to clean up.

Separately, every *non*-NEG gig that survives the filter chain gets
auto-applied immediately (`notifier.apply_to_gig`, Phase 3 of `main.py`) with
no human check at all — including weekday gigs, gigs bundling more than one
service in one posting, and services outside {Funeral, Wedding, plain
one-service Sunday service}, none of which the user wants sent without
review.

## Goals

- Every held-for-review gig (NEG or otherwise) gets a **real Gmail draft**
  (created via the Gmail API, in the user's own account), addressed and
  bodied exactly as the email that would be sent.
- The Telegram alert shows the gig's own details (as scraped) with two
  buttons: **Accept** / **Decline**. Accept asks for a plain `Confirm` /
  `Cancel` re-confirmation, then sends the *actual Gmail draft* via
  `drafts.send` (so a hand-edit made directly in Gmail before confirming goes
  out as edited). Decline deletes the Gmail draft and marks the row
  rejected — immediately, no second confirmation.
- A new classifier decides, for every non-NEG gig that already passed the
  filter chain, whether it's safe to auto-send or must be held for review:
  - Weekday gigs (`parse_weekday(gig.date) != 6`) are **always** held — no
    LLM call needed, this is a pure date check.
  - Sunday gigs go through an LLM classifier reading `gig.header` +
    `gig.musical_requirements`: multi-service bundles, and anything other
    than a single Funeral / Wedding / plain Sunday-service, are held. A
    single-service Funeral, Wedding, or plain Sunday service auto-sends as
    today.
  - NEG takes priority: a gig that's both fee-negotiable and would
    independently trip the new classifier is only ever held once, via the
    existing NEG path (unchanged fee-negotiation draft) — the classifier
    never runs on NEG gigs.
- Buttons fully replace the existing typed-command flow (`approve <id>`,
  `edit <id>: ...`, `reject <id>`) — there is no more free-text path, and no
  in-bot editing (edit the draft directly in Gmail instead, then Accept).
  This removes the disambiguation-picker machinery (`_active_neg_draft`,
  `_pending_neg_instruction`, `_resolve_neg_gig_id`) that only existed to
  resolve an omitted `gig_id` from free text — every button now carries its
  exact `gig_id`, so there's nothing left to disambiguate.

## Non-goals

- Changing anything about *how* the filter chain / fee partition decides
  which gigs reach Phase 3 in the first place — the classifier is an
  additional partition on gigs that already passed, not a new filter.
- Any in-Telegram way to edit a draft's text — editing happens in Gmail.
- Changing the Gmail account the bot operates against, or `reply_monitor`'s
  read-only search behavior.
- A generic "held reasons" reporting/analytics surface — `hold_reason` is
  stored for debugging/display only, no new tool exposes reporting on it
  beyond the existing listing tool described below.

## Design

### 1. Gmail write client (`integrations/gmail_client.py`)

Add three methods to the existing `GmailClient`, alongside the current
read-only ones:

```python
def create_draft(self, *, sender: str, recipient: str, cc: list[str] | None,
                  subject: str, body_html: str) -> str:
    """Builds the MIME message (mirrors send_application_email's MIMEText
    construction) and calls drafts().create. Returns the new draft id."""

def send_draft(self, draft_id: str) -> None:
    """drafts().send — sends exactly what's currently in the draft."""

def delete_draft(self, draft_id: str) -> None:
    """drafts().delete."""
```

`_build_service`'s hardcoded `scopes = [...]` list gains
`"https://www.googleapis.com/auth/gmail.compose"` alongside the existing
`gmail.readonly`. `scripts/setup_gmail_auth.py`'s `SCOPES` gets the same
addition. (The live token has already been re-consented with both scopes as
part of this feature's setup — re-running `setup_gmail_auth.py` is no longer
required before shipping, only the code needs to declare the same scopes it
was actually granted.)

Both new failure modes (`create_draft` failing, `send_draft`/`delete_draft`
hitting an id that no longer exists in Gmail e.g. manually deleted) raise —
callers are responsible for catching and reporting, matching the existing
`SMTPTransport.send` / `_search_messages` conventions of "let the caller
decide fail-open vs fail-loud" rather than swallowing silently.

### 2. `application_store.py` — generalize `neg_pending` into a shared "held draft" shape

Replace the NEG-specific fields with a shape both statuses share:

```python
{
    "gig_id": ..., "url": ..., "header": ..., "organisation": ...,
    "date": ..., "time": ..., "fee": ..., "email": ..., "postcode": ...,
    "status": "neg_pending" | "review_pending",
    "draft_id": "<gmail draft id>",
    "draft_subject": "...",          # kept, for the listing tool + logs
    "negotiable_fee": int | None,    # only meaningful for neg_pending
    "hold_reason": "fee_negotiation" | "multi_service" | "other_service_type" | "weekday",
    "created_at": ..., "updated_at": ..., "decided_at": None, "decision": None,
}
```

`draft_body` is dropped — the draft's content now lives in Gmail, not in
`applications.json`.

Function changes:

- `record_neg_pending(...)` → `record_held_draft(gig, *, status, draft_id, draft_subject, hold_reason, negotiable_fee=None)`. Same idempotency-by-URL contract.
- `list_neg_pending()` → `list_held(status: str | None = None)` (both statuses if omitted).
- `transition_neg_pending(gig_id, to=...)` → `transition_held(gig_id, *, to)`. Same "returns False if not in a pending status" contract — this is what makes double-tap/race handling safe, unchanged.
- `update_neg_draft` is **removed** — nothing edits a draft's stored text anymore (edits happen in Gmail directly).
- `get_by_gig_id` unchanged.

### 3. Non-NEG hold classifier (`gig_classifier.py`, new)

Mirrors `reply_monitor._classify_reply`'s shape exactly — same fixed-model
(`claude-haiku-4-5-20251001`) direct `anthropic.Anthropic` call, same
fail-toward-safe default on any error:

```python
@dataclass
class Classification:
    decision: Literal["auto_send", "hold_for_review"]
    reason: str  # "multi_service" | "other_service_type" | "auto_eligible"

def classify_gig(gig: Gig) -> Classification:
    """Reads gig.header + gig.musical_requirements. Only called for gigs
    already confirmed to be on a Sunday — see main.py partition below.
    On any classification/API error, returns hold_for_review — a
    classifier failure must never silently auto-send."""
```

Prompt instructs Haiku to answer with a single word from
`{multi_service, other_service_type, auto_eligible}` (multi_service takes
priority if both apply), same "reply with ONLY ..." + untrusted-input
framing as `_CLASSIFY_PROMPT`. Anything not exactly one of those three words
maps to `hold_for_review` / `other_service_type`, matching
`_classify_reply`'s "unexpected → safe default" handling.

### 4. `main.py` — new partition after the existing fee partition

The existing fee-partition block already leaves `valid_gigs` holding exactly
"gigs to proceed toward Phase 3, fee-negotiable ones already pulled into
`neg_gigs`" — that's true whether `enable_neg_drafts` is on (where it
reassigns `valid_gigs = normal_gigs`) or off (where `valid_gigs` is just
left as the fee-filtered set, no `normal_gigs` variable exists). The new
classifier partition reads from `valid_gigs` directly for exactly this
reason — it must not assume `normal_gigs` exists:

```python
auto_send_gigs: list[Gig] = []
review_gigs: list[tuple[Gig, str]] = []  # (gig, hold_reason)

for gig in valid_gigs:
    weekday = parse_weekday(gig.date)
    if weekday is None or weekday != 6:
        review_gigs.append((gig, "weekday"))
        continue
    result = classify_gig(gig)
    if result.decision == "hold_for_review":
        review_gigs.append((gig, result.reason))
    else:
        auto_send_gigs.append(gig)

valid_gigs = auto_send_gigs  # Phase 3 auto-send + seen-gigs handle these only
```

An unparseable date (`weekday is None`) is treated the same as a weekday —
hold rather than guess. This partition is unconditional (runs regardless of
`enable_neg_drafts` — the classifier is independent of whether NEG drafting
is on) and only ever sees gigs that already passed `FeeFilter` normally
(`neg_gigs` never reaches this loop, per the "NEG takes priority" goal — the
fee partition already pulled those out first, whichever branch it took).

Phase 3's existing `for gig in valid_gigs: notifier.apply_to_gig(gig)` loop
is unchanged (it now just sees a smaller set). One `GmailClient` instance and one `Notifier` instance are constructed once,
above both the existing NEG-drafts block and the new review-drafts block
(guarded by `if (neg_gigs or review_gigs) and not dry_run`), and shared by
both — the existing `_neg_notifier` local is renamed `_draft_notifier` and
used for both `draft_negotiation` (NEG) and the new `draft_application`
(review) calls; likewise one `gmail_client`, not one per block:

```python
gmail_client = GmailClient(settings.gmail_credentials_file, settings.gmail_token_file)
_draft_notifier = Notifier(settings, SMTPTransport(password=settings.email_password))
```

A new block parallel to the existing "NEG drafts: render, persist, alert
Telegram" block handles `review_gigs`:

```python
if review_gigs and not dry_run:
    for gig, hold_reason in review_gigs:
        if not gig.email:
            logger.warning("Review draft skipped — no contact email", ...)
            continue
        try:
            subject, body = _draft_notifier.draft_application(gig)  # new Notifier method, see below
            draft_id = gmail_client.create_draft(
                sender=settings.email_sender, recipient=gig.email,
                cc=[settings.cc_email] if settings.cc_email else None,
                subject=subject, body_html=body,
            )
            gig_id = application_store.record_held_draft(
                gig, status="review_pending", draft_id=draft_id,
                draft_subject=subject, hold_reason=hold_reason,
            )
            _send_review_alert(gig, gig_id, status_label="review")
        except Exception:
            logger.exception("Review draft failed for gig — skipping", extra={"link": gig.link})
```

The existing NEG-drafts block is updated the same way: instead of
`_neg_notifier.draft_negotiation(...)` producing text stored verbatim, it
now also calls `gmail.create_draft(...)` and stores the returned `draft_id`
via `record_held_draft(..., status="neg_pending", hold_reason="fee_negotiation", negotiable_fee=...)`.

`Notifier` gains `draft_application(gig) -> tuple[str, str]`, mirroring
`draft_negotiation` exactly but rendering the existing `application.html.j2`
template (the same one `apply_to_gig` already uses) instead of
`negotiation.html.j2` — so a held-for-review draft is byte-for-byte the same
email an auto-sent gig would have gotten, just not sent yet.

### 5. Telegram alert (`main.py`, `_send_neg_alert` → shared `_send_review_alert`)

One shared function replaces `_send_neg_alert`, used for both statuses —
the gig-details message is unchanged in shape (header, org, date/time, fee,
location, contact, link), just prefixed `🟡 NEG gig` or `🔵 Review needed`
depending on status. It's now the **only** message sent — the second
"draft text + 3 buttons + typed-command instructions" message is dropped
entirely, since the draft itself lives in Gmail now:

```python
def _send_review_alert(gig: Gig, gig_id: str, status: str, hold_reason: str) -> None:
    label = "🟡 NEG gig" if status == "neg_pending" else "🔵 Review needed"
    reason_line = f"Reason: {hold_reason}\n" if status == "review_pending" else ""
    ...
    buttons = {"inline_keyboard": [[
        {"text": "✅ Accept", "callback_data": f"review:accept:{gig_id}"},
        {"text": "❌ Decline", "callback_data": f"review:decline:{gig_id}"},
    ]]}
    alert.send_alert(details_msg, reply_markup=buttons)
```

### 6. Telegram callback handler (`telegram_bot.py` + `unified_agent.py`)

`handle_neg_callback` is renamed `handle_review_callback`, registered on
`pattern=r"^review:"`, and simplified to the two-button + confirm shape
(no `edit`, no `pick`):

| Tap | Effect |
|---|---|
| `accept` | Edit message to `Confirm \| Cancel` (`review:confirm_send:<id>` / `review:cancel:<id>`) |
| `decline` | `unified_agent.review_decline(gig_id)` — calls `gmail.delete_draft(draft_id)`, `transition_held(gig_id, to="rejected")`, edits message to "❌ Declined — draft deleted." No confirmation step (matches "once I decline, the draft will be deleted"). |
| `confirm_send` | `unified_agent.review_confirm_send(gig_id)` — calls `gmail.send_draft(draft_id)`, `transition_held(gig_id, to="applied")`, edits message to "✅ Sent" |
| `cancel` | Re-render the original gig-details message with `Accept \| Decline` restored — row stays pending, nothing sent or deleted |

Both `review_confirm_send` and `review_decline` work identically regardless
of whether the row is `neg_pending` or `review_pending` — `transition_held`
takes any pending status. This single handler/tool pair replaces
`handle_neg_callback`, `neg_confirm_send`, `neg_confirm_reject`,
`neg_draft_view`, `_draft_buttons`, `_resolve_neg_gig_id`,
`_neg_picker_response`, `set_active_neg_draft`/`get_active_neg_draft`,
`stash_pending_neg_instruction`/`pop_pending_neg_instruction`, and the
`approve_neg_application`/`edit_neg_application`/`reject_neg_application`
LLM tool handlers. `agent_state.py`'s `_KEYS` drops `"active_neg_draft"`.

A single **read-only** LLM tool survives for conversational status checks —
`list_pending_drafts` (replaces `list_neg_pending`), listing both statuses
with their `hold_reason`, still useful for "what's waiting on me?"-style
chat questions even though acting on them is button-only now.

### 7. Error handling

- **Stale/already-decided taps**: `transition_held` returning `False` (row
  no longer in a pending status) is surfaced as "Already {status} at
  {time}." — same pattern as today's `_neg_row_lookup_error`, generalized
  to both statuses.
- **`send_draft`/`delete_draft` Gmail API failure**: caught in
  `review_confirm_send`/`review_decline`; message shows "❌ Send failed:
  …" / "❌ Delete failed: …", row stays in its pending status (retryable —
  a later tap can try again), mirroring today's `neg_confirm_send` failure
  behavior exactly.
- **`create_draft` failure at scrape time**: caught per-gig in the
  NEG-drafts and review-drafts loops (already `try/except Exception` +
  `logger.exception` + continue to the next gig) — one gig's Gmail API
  failure doesn't drop the rest of the tick.
- **Classifier failure**: `classify_gig` itself never raises to its caller
  (internal try/except, fails to `hold_for_review`) — `main.py`'s new
  partition loop has no special-case error handling to write.
- **Race on `confirm_send`/`decline`**: unchanged from today —
  `transition_held` is file-locked and returns `False` on a lost race,
  checked before editing the Telegram message to a terminal state.
- **Auth**: `handle_review_callback` reuses the same `_is_authorised` check
  as every other handler.

## Testing

- `GmailClient.create_draft`/`send_draft`/`delete_draft`: a `FakeGmailClient`
  (mirrors `FakeTransport`) recording calls, used by every test that would
  otherwise hit the real Gmail API.
- `application_store`: `record_held_draft`/`list_held`/`transition_held`
  round-trip for both statuses; idempotency-by-URL; `transition_held`
  returns `False` on an already-decided or unknown `gig_id`.
- `gig_classifier.classify_gig`: mocked Anthropic client, one test per
  expected word, one test for an unexpected/garbled response → safe
  default, one test for an API exception → safe default (same shape as
  existing `_classify_reply` tests).
- `main.py` partition logic: weekday gig → held with `hold_reason="weekday"`
  and classifier never called (mock asserts zero calls); Sunday +
  `auto_eligible` → `auto_send_gigs`; Sunday + `multi_service`/
  `other_service_type` → `review_gigs`; a NEG gig never reaches the
  classifier at all.
- `handle_review_callback`: every `review:*` action, both statuses, stale-tap
  handling, double-tap race safety, auth rejection — same coverage shape as
  the current `handle_neg_callback` tests, updated for the two-button flow.
- End-to-end-ish: gig → classifier holds it → Gmail draft created (fake) →
  Accept → Confirm → fake `send_draft` called → status `applied`. Same for
  Decline → fake `delete_draft` called → status `rejected`.
