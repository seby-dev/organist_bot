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
  - **Monday–Friday gigs are always held** (`parse_weekday(gig.date) not in
    (5, 6)`) — no LLM call needed, this is a pure date check. Confirmed with
    the user: this is unconditional, no Funeral/Wedding exception on a
    weekday — funerals are overwhelmingly Mon–Fri, so this is in practice
    where most funerals land.
  - **Saturday and Sunday gigs both go through the LLM classifier** (same
    call, same prompt — Saturday isn't just "not-Sunday", it gets the same
    treatment as Sunday, per the user's confirmation that a Saturday
    Funeral/Wedding should auto-send): reading `gig.header` +
    `gig.musical_requirements` + `gig.time` + `gig.fee` (see §3 — `time` in
    particular is the strongest multi-service signal, e.g. "9:00 AM & 6:00
    PM"), multi-service bundles and anything other than a single Funeral,
    Wedding, or plain Sunday-service are held; a single-service Funeral,
    Wedding, or plain Sunday/Saturday service auto-sends as today.
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

Add four methods to the existing `GmailClient`, alongside the current
read-only ones:

```python
def create_draft(
    self, *, sender: str, recipient: str, cc: list[str] | None, subject: str, body_html: str
) -> str:
    """Builds the MIME message (mirrors send_application_email's MIMEText
    construction) and calls drafts().create. Returns the new draft id."""


def send_draft(self, draft_id: str) -> None:
    """drafts().send — sends exactly what's currently in the draft."""


def delete_draft(self, draft_id: str) -> None:
    """drafts().delete."""


def has_compose_access(self) -> bool:
    """A light drafts().list(maxResults=1) call, True on success, False on
    a 403 — used only by the startup smoke-check below, not the per-gig
    create/send/delete path."""
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

**Startup scope smoke-check.** `google-auth`'s `Credentials.refresh` does
not hard-fail when the token on disk was minted under the old
`gmail.readonly`-only scope and the code now asks for `gmail.compose` too —
it just logs an internal warning ("Not all requested scopes were granted")
and returns credentials that still can't create drafts. Without a check,
the first sign of a stale/under-scoped token would be `drafts.create`
returning 403 on some gig, mid-tick. Add `warn_if_gmail_write_scope_missing()`
to `main.py` (same "alert once at scheduler startup" shape as
`warn_if_gmail_monitoring_unconfigured`) — calls
`gmail_client.has_compose_access()` once at startup; `False` triggers a
clear `alert.send_alert` naming the fix (re-run
`scripts/setup_gmail_auth.py`) instead of a confusing per-gig failure alert
(§4) with no obvious cause.

### 2. `application_store.py` — generalize `neg_pending` into a shared "held draft" shape

Replace the NEG-specific fields with a shape both statuses share:

```python
{
    "gig_id": ...,
    "url": ...,
    "header": ...,
    "organisation": ...,
    "contact": ...,
    "date": ...,
    "time": ...,
    "fee": ...,
    "email": ...,
    "postcode": ...,
    "status": "neg_pending" | "review_pending",
    "draft_id": "<gmail draft id>",
    "draft_subject": "...",  # kept, for the listing tool + logs
    "negotiable_fee": int | None,  # only meaningful for neg_pending
    "hold_reason": "fee_negotiation" | "multi_service" | "other_service_type" | "weekday",
    "created_at": ...,
    "updated_at": ...,
    "decided_at": None,
    "decision": None,
}
```

`draft_body` is dropped — the draft's content now lives in Gmail, not in
`applications.json`. `contact` is a new field on the held-draft shape (today's
`neg_pending` row never stored it — see `_send_neg_alert`'s gig-details
message, which reads `gig.contact` from the live `Gig`, not the stored row);
storing it now is what lets §5/§6's "cancel" and "already-decided" paths
render a details message from the stored row alone, without needing the
scheduler process's live `Gig` object.

Function changes:

- `record_neg_pending(...)` → `record_held_draft(gig, *, status, draft_id, draft_subject, hold_reason, negotiable_fee=None) -> tuple[str, bool]` — returns `(gig_id, created)`. `created=False` means a row for this URL already existed (any status) and nothing was written, **exactly like today's idempotency-by-URL contract** — but now the caller must react to it: see "Duplicate-draft race" in §7, since unlike the old text-only draft, a real Gmail draft was just created via `gmail_client.create_draft(...)` *before* this call, and an unused one must be cleaned up when `created` comes back `False`.
- `list_neg_pending()` → `list_held(status: str | None = None)` (both statuses if omitted).
- `transition_neg_pending(gig_id, to=...)` → `transition_held(gig_id, *, to)`. Same "returns False if not in a pending status" contract — this is what makes double-tap/race handling safe, unchanged.
- `update_neg_draft` is **removed** — nothing edits a draft's stored text anymore (edits happen in Gmail directly).
- `get_by_gig_id` unchanged.
- `expire_past_applied()` (`application_store.py:285-318`) currently flips only `applied` and `neg_pending` rows whose gig date has passed, inline (not via `transition_neg_pending`/`transition_held`), and returns just a count (`int`). It must also flip `review_pending` rows the same way, and its return type changes to `list[dict]` — the full expired rows, not just a count — so its caller in `main.py` can act on each row's `draft_id`. `main.py`'s existing call site (`expired = application_store.expire_past_applied(); ... extra={"count": expired}`) becomes `expired_rows = application_store.expire_past_applied()`, logs `len(expired_rows)`, and calls `gmail_client.delete_draft(row["draft_id"])` for each row that has one (plain `applied` rows don't), tolerating 404 (see §7) — mirroring the Decline behavior for a held row nobody ever got to act on.

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
    """Reads gig.header, gig.musical_requirements, gig.time, gig.fee. Only
    called for gigs already confirmed to be Saturday or Sunday — see
    main.py partition below (weekday gigs never reach this, they're held
    on the date check alone). On any classification/API error, returns
    hold_for_review — a classifier failure must never silently auto-send."""
```

The prompt includes `gig.time` and `gig.fee` alongside `header` and
`musical_requirements` — `musical_requirements` alone is a weak signal (per
the scraper, it's a repertoire note like "Traditional hymns and organ
voluntaries", not a service-count indicator); `time` is the strongest
available multi-service signal in the scraped data (e.g. "9:00 AM & 6:00
PM" or "8am and 10:30am" plainly says two services), and `fee` sometimes
reads "£80 per service". `header` still carries the primary service-type
word (`"Sunday Service"`, `"Funeral"`, `"Wedding"`, `"Evensong"`, etc., per
the real values seen in tests). Note `analytics._classify_gig_type`
(`analytics.py:91-113`) already does simple keyword matching over `header`
for Wedding/Funeral/Service/Other, for a different purpose (income-forecast
labeling) — not reused directly here since it's a lighter, keyword-only
pass with no bundle-detection, but worth the implementer's look as prior
art for the header-matching half of this prompt.

Prompt instructs Haiku to answer with a single word from
`{multi_service, other_service_type, auto_eligible}` (multi_service takes
priority if both apply), same "reply with ONLY ..." + untrusted-input
framing as `_CLASSIFY_PROMPT`. Anything not exactly one of those three words
maps to `hold_for_review` / `other_service_type`, matching
`_classify_reply`'s "unexpected → safe default" handling.

**Classifier unavailable.** `settings.anthropic_api_key` is optional
(defaults to `""`) — if it's unset, or Anthropic is down, `classify_gig`
fails to `hold_for_review` for every Saturday/Sunday gig it's asked about,
silently, forever, with no signal to the user that auto-send has
effectively stopped. Add a startup check mirroring
`warn_if_gmail_monitoring_unconfigured` — `warn_if_gig_classifier_unconfigured()`,
alerting once at scheduler startup if `anthropic_api_key` is empty. (An
Anthropic *outage* rather than a missing key isn't distinguishable at
startup — that's just accepted as "everything holds until Anthropic
recovers", consistent with the fail-safe design; the classifier
intentionally does not use the agent's multi-provider litellm failover, per
the earlier design decision to match `reply_monitor`'s fixed-model
precedent.)

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
    if is_negotiable(gig.fee):
        # Belt-and-braces: only reachable when ENABLE_FEE_FILTER=false
        # (then _fee_filter is None and the fee partition above never ran,
        # so a NEG gig can still be sitting in valid_gigs — see §"NEG +
        # ENABLE_FEE_FILTER=false" note below). Never held or classified —
        # falls through to auto-send exactly like it does today in that
        # config, unchanged by this feature.
        auto_send_gigs.append(gig)
        continue
    weekday = parse_weekday(gig.date)
    if weekday is None or weekday not in (5, 6):  # not Saturday or Sunday
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
is on) and, when `enable_neg_drafts=True` (the normal case), never sees a
NEG gig at all — `neg_gigs` was already pulled out by the fee partition
before `valid_gigs` reaches this loop.

**NEG + `ENABLE_FEE_FILTER=false` edge case.** The fee partition
(`main.py:387`) only runs `if settings.enable_neg_drafts and _fee_filter is
not None`. With `ENABLE_FEE_FILTER=false`, `_fee_filter is None`, so that
block is skipped entirely and NEG gigs are never pulled out — they're
already in `valid_gigs` (unchanged pre-existing behavior: today they
auto-apply like any other gig in that config, with no fee proposal). The
`is_negotiable(gig.fee)` check above preserves that — without it, a NEG gig
in this specific config would newly get held with a plain
`application.html.j2` draft (no fee proposal) instead of auto-sending as it
does today, which would be a silent behavior change in an edge-case config
this feature isn't meant to touch.

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
                sender=settings.email_sender,
                recipient=gig.email,
                cc=[settings.cc_email] if settings.cc_email else None,
                subject=subject,
                body_html=body,
            )
            gig_id, created = application_store.record_held_draft(
                gig,
                status="review_pending",
                draft_id=draft_id,
                draft_subject=subject,
                hold_reason=hold_reason,
            )
            if not created:
                # A row for this URL already existed (any status) — the
                # idempotency-by-URL contract means our create_draft call
                # above was wasted and its draft is now orphaned (the
                # existing row's real draft_id, if any, is untouched).
                # Delete the just-created duplicate; tolerate 404 (§7).
                try:
                    gmail_client.delete_draft(draft_id)
                except Exception:
                    logger.warning(
                        "Could not delete orphaned duplicate draft",
                        extra={"draft_id": draft_id, "link": gig.link},
                    )
                continue
            _send_review_alert(gig, gig_id, status="review_pending", hold_reason=hold_reason)
        except Exception:
            logger.exception("Review draft failed for gig — skipping", extra={"link": gig.link})
            alert.send_alert(f"⚠️ Review draft failed for {gig.header} — {gig.link}")
```

`create_draft` failing (last line above) is `alert.send_alert`'d, not just
logged — unlike today's fully-automatic apply, a held gig depends on a
Gmail API call succeeding *before* the user ever sees it, so a silent
failure here means a gig the user should have been asked about just
vanishes with only a log line. This gig's URL is **not** added to
`newly_seen` (`main.py`'s existing seen-gigs write, further down) when its
`create_draft` call fails, so it's retried automatically next tick rather
than being permanently lost — self-healing once Gmail access is restored,
at the cost of a repeat alert every tick it keeps failing (accepted
trade-off: unlike the `reply_monitor` "unclear reply" spam this session
just fixed, here the underlying condition — Gmail still broken — is
genuinely still true each time, so repeat alerting is real signal, not
noise from re-evaluating something that can't change).

The existing NEG-drafts block is updated the same way: instead of
`_neg_notifier.draft_negotiation(...)` producing text stored verbatim, it
now also calls `gmail_client.create_draft(...)` and stores the returned
`draft_id` via `record_held_draft(..., status="neg_pending",
hold_reason="fee_negotiation", negotiable_fee=...)` — same `created=False`
duplicate-cleanup and `newly_seen` exclusion on failure as the review-drafts
block above.

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
    """status: "neg_pending" | "review_pending". hold_reason is only shown
    for review_pending (neg_pending's "reason" is implicitly the fee, shown
    via the existing Fee: NEG / Proposed: £{negotiable_fee} lines)."""
    label = "🟡 NEG gig" if status == "neg_pending" else "🔵 Review needed"
    reason_line = f"Reason: {hold_reason}\n" if status == "review_pending" else ""
    ...
    buttons = {
        "inline_keyboard": [
            [
                {"text": "✅ Accept", "callback_data": f"review:accept:{gig_id}"},
                {"text": "❌ Decline", "callback_data": f"review:decline:{gig_id}"},
            ]
        ]
    }
    alert.send_alert(details_msg, reply_markup=buttons)
```

This is the single call site both the NEG-drafts block and the review-drafts
block use (§4) — e.g.
`_send_review_alert(gig, gig_id, status="neg_pending", hold_reason="fee_negotiation")`
for NEG, `_send_review_alert(gig, gig_id, status="review_pending",
hold_reason=hold_reason)` for review. For `neg_pending`, the proposed fee is
folded into the message body via the existing `Fee:`/negotiable-fee lines,
not a second message — dropping the old second "draft text" Telegram
message (which used to be the only place the proposed fee was visible) means
this single message must show it, e.g. `Proposed: £{negotiable_fee}` under
the `Fee: NEG` line, or the fee negotiation amount is otherwise invisible in
Telegram now.

### 6. Telegram callback handler (`telegram_bot.py` + `unified_agent.py`)

`handle_neg_callback` is renamed `handle_review_callback`, registered on
`pattern=r"^review:"`, and simplified to the two-button + confirm shape
(no `edit`, no `pick`):

| Tap | Effect |
|---|---|
| `accept` | Swap the message's **reply markup only** (`_edit_buttons_quietly`, not the text) to `Confirm \| Cancel` (`review:confirm_send:<id>` / `review:cancel:<id>`) |
| `decline` | `unified_agent.review_decline(gig_id)` — calls `gmail_client.delete_draft(draft_id)`, `transition_held(gig_id, to="rejected")`, edits message to "❌ Declined — draft deleted." No confirmation step (matches "once I decline, the draft will be deleted"). |
| `confirm_send` | `unified_agent.review_confirm_send(gig_id)` — calls `gmail_client.send_draft(draft_id)`, `transition_held(gig_id, to="applied")`, edits message to "✅ Sent" |
| `cancel` | Swap the reply markup **back** to `Accept \| Decline` (`_edit_buttons_quietly` again, same as `accept`/`reject` today) — row stays pending, nothing sent or deleted |

`cancel` swaps buttons only, same as `accept` — it does **not** rebuild the
gig-details text. Today's `neg_draft_view`/`_draft_buttons`-based "cancel"
implicitly assumed the scheduler process could regenerate the original
message text from the stored row, but the row never carried `gig.contact`
(only `email`), so that path was already silently degraded (`row.get("contact")`
is always `None` today). Since the original message already has the correct
gig-details text — it just needs its buttons restored — there is no text to
rebuild and no need for the row to carry every field the original message
used; `contact` is added to the row (§2) anyway, for the "already-decided"
lookup message, not for reconstructing this one.

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
- **`send_draft`/`delete_draft` Gmail API failure (non-404)**: caught in
  `review_confirm_send`/`review_decline`; message shows "❌ Send failed:
  …" / "❌ Delete failed: …", row stays in its pending status (retryable —
  a later tap can try again), mirroring today's `neg_confirm_send` failure
  behavior exactly.
- **404 on `delete_draft`** (the draft is already gone — deleted directly
  in Gmail, or already sent from Gmail's own UI, or a second `decline` tap
  racing the first): treated as **success**, not failure — the desired
  end state (no draft left) already holds. Proceeds to
  `transition_held(gig_id, to="rejected")` exactly as a clean delete would.
- **404 on `send_draft`** (the draft was already sent — from Gmail's UI
  directly, bypassing the bot entirely, or a second `confirm_send` tap
  racing the first): re-read the row via `get_by_gig_id` before deciding
  what to show. If it's already `applied` (the other tap/path won the
  race and transitioned it), show "Already sent — {status} at {time}",
  same shape as the stale-tap case below, **not** "Send failed" (which
  would misleadingly suggest the application never went out when it did).
  If it's still `neg_pending`/`review_pending` (genuinely sent from Gmail's
  UI, bot never told), show "❌ Draft no longer exists in Gmail — if you
  already sent it there directly, no action needed; otherwise this
  application was not sent." and leave the row as-is (not auto-transitioned
  either way, since the bot cannot tell which case it is) — it's still
  reachable via `list_pending_drafts` and still auto-expires like any other
  held row once the gig date passes (§2).
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

### 8. Removal completeness

§6's removal list names the obvious pieces; the reviewer found several more
references throughout `unified_agent.py` that a fresh implementer would
otherwise miss and leave dangling (all confirmed present in the current
code):

- The system prompt's "## NEG-fee drafts" section (instructs the LLM to
  call `list_neg_pending`/`approve_neg_application`/`edit_neg_application`/
  `reject_neg_application`) — left in place, the LLM will hallucinate calls
  to tools that no longer exist. Replace with a short note about the new
  read-only `list_pending_drafts` tool and that acting on a draft is
  button-only.
- `_VERBATIM_RESPONSE_TOOLS` currently lists all four removed tools — drop
  them, add `list_pending_drafts`.
- `process_message`'s `needs_pick` branch calls
  `stash_pending_neg_instruction` — the picker flow it supports is gone
  entirely (§ "Buttons fully replace..." in Goals), so this branch goes too.
- `reset_conversation`, `_hydrate_chat`, `_persist_chat` all reference
  `_active_neg_draft`/`"active_neg_draft"` — drop alongside `agent_state.py`'s
  `_KEYS` change (§6).
- Imports `Notifier`, `SMTPTransport`, `send_application_email` in
  `unified_agent.py` become unused once `neg_confirm_send` and
  `_handle_edit_neg` are removed — `ruff` (F401) fails `make ship` on an
  unused import, so this isn't optional cleanup, it's required for the
  build to pass. (`Gig` stays — used elsewhere in the file.)
- `tests/test_agent_state.py` asserts `"active_neg_draft": None` in the
  persisted shape — update alongside the `_KEYS` change.
- `tests/test_main.py::TestNegDrafts` — see Testing below; several of its
  existing tests assert today's auto-send behavior using a fixture date
  (`_future_date()`, today+21 days) that lands on an arbitrary weekday, so
  they'll silently start exercising the new hold path instead of what
  they're meant to test unless the fixture date is pinned.

**Documentation.** `CLAUDE.md`'s "NEG-fee drafts" section (the
`approve <id>`/`edit <id>`/`reject <id>` commands, `list_neg_pending`, the
`neg_pending`-only visibility caveat) describes the flow this feature
replaces — it needs rewriting to match the new Accept/Decline-button,
real-Gmail-draft flow (and the new `review_pending` status, hold reasons,
and `list_pending_drafts` tool) as part of this change, not left stale.

## Testing

- `GmailClient.create_draft`/`send_draft`/`delete_draft`/`has_compose_access`:
  a `FakeGmailClient` (mirrors `FakeTransport`) recording calls, used by
  every test that would otherwise hit the real Gmail API. Include a
  404-simulating variant for the `send_draft`/`delete_draft` error-handling
  tests below.
- `application_store`: `record_held_draft`/`list_held`/`transition_held`
  round-trip for both statuses; idempotency-by-URL now returning
  `(gig_id, created=False)` on a duplicate URL rather than silently
  succeeding; `transition_held` returns `False` on an already-decided or
  unknown `gig_id`; `expire_past_applied` now also expires `review_pending`
  rows and returns the full expired-row list (not a count) including each
  row's `draft_id`.
- `gig_classifier.classify_gig`: mocked Anthropic client, one test per
  expected word, one test for an unexpected/garbled response → safe
  default, one test for an API exception → safe default (same shape as
  existing `_classify_reply` tests); one test confirming the prompt/call
  includes `gig.time` and `gig.fee`, not just `header`/`musical_requirements`.
- `main.py` partition logic: **fixture gig dates must be pinned to specific
  weekdays** (a Sunday, a Saturday, and a Monday — not `_future_date()`'s
  today+21-days, which lands on an arbitrary day and would make these tests
  flaky/wrong depending on when they run) —
  Monday gig → held with `hold_reason="weekday"` and `classify_gig` never
  called (mock asserts zero calls); Saturday + `auto_eligible` →
  `auto_send_gigs`; Sunday + `auto_eligible` → `auto_send_gigs`; Saturday or
  Sunday + `multi_service`/`other_service_type` → `review_gigs`; a NEG gig
  (`is_negotiable(gig.fee)` true) never reaches `classify_gig` regardless of
  weekday, via both the normal fee-partition path and the
  `ENABLE_FEE_FILTER=false` edge case (§4) where it falls through to
  `auto_send_gigs` directly. `main.py`'s existing tests need `GmailClient`
  patched (`patch("main.GmailClient")`, since there's no dependency
  injection on `_run(scraper, dry_run)`) wherever a gig now reaches the
  NEG-draft or review-draft block.
- `record_held_draft` duplicate-URL race: `create_draft` called once,
  `record_held_draft` returns `created=False` on the second attempt for the
  same URL, caller's `delete_draft` is called with the orphaned draft id.
- `expire_past_applied` + draft cleanup: an expired `review_pending`/
  `neg_pending` row's `draft_id` is passed to `gmail_client.delete_draft` by
  the `main.py` call site; a 404 there doesn't raise or alert (already-gone
  is fine, mirrors the Decline 404 case).
- `handle_review_callback`: every `review:*` action, both statuses,
  stale-tap handling, double-tap race safety, auth rejection — same
  coverage shape as the current `handle_neg_callback` tests, updated for
  the two-button flow. `accept`/`cancel` assert only the reply markup was
  edited (`edit_message_reply_markup`), never the message text. 404 cases:
  `decline` on an already-gone draft still transitions to `rejected`;
  `confirm_send` on an already-gone draft shows "already sent" when the row
  is `applied`, and the no-action-taken message when it isn't.
- End-to-end-ish: gig → classifier holds it → Gmail draft created (fake) →
  Accept → Confirm → fake `send_draft` called → status `applied`. Same for
  Decline → fake `delete_draft` called → status `rejected`.
