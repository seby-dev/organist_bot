# NEG-gig draft & approval flow

**Date:** 2026-06-09
**Status:** Approved (pending implementation)

## Problem

When the gig scraper sees a listing whose fee is `"NEG"` or `"Negotiable"`, the existing `FeeFilter` rejects it outright (because `parse_min_fee` returns `None` for those strings, and the filter requires a parseable amount ≥ `MIN_FEE`). These gigs may actually be worth pursuing at a negotiated fee — but the user wants visibility and approval before any email goes out, since the fee proposal is a judgment call.

## Goal

For gigs whose fee is `NEG`/`Negotiable` and which pass every other pipeline filter, the bot should:

1. Draft an application email that explicitly proposes the user's negotiable fee (default £120).
2. Send the draft to Telegram for review.
3. Wait for the user to approve, edit, or reject via the existing unified Telegram agent before any email is sent to the recipient.

Other "no parseable fee" cases (`"Expenses only"`, blank fee) remain rejected, as do numeric fees below `MIN_FEE`.

## Non-goals

- Inline Telegram keyboard buttons. Approvals flow through normal chat replies, consistent with the existing unified-agent pattern.
- Auto-counter / multi-round negotiation. The £120 figure is static per draft until the user explicitly edits.
- Changes to `reply_monitor`. When the recipient replies, it's classified by the existing Claude classifier the same as any other application reply.

## Detection

Today, `organist_bot/filters.py::parse_min_fee` uses the regex `r"neg|negotiable|expenses"` (case-insensitive) to short-circuit non-numeric strings to `None`. This regex is the single source of truth for "no parseable amount", but it lumps together two semantically different cases — negotiable (worth approaching) and expenses-only (not worth approaching).

**Change:** extract the negotiable-only regex as a module-level constant and add a sibling helper:

```python
_NEG_REGEX = re.compile(r"\b(neg|negotiable)\b", re.IGNORECASE)

def is_negotiable(fee_str: str | None) -> bool:
    if not fee_str:
        return False
    return bool(_NEG_REGEX.search(fee_str))
```

`parse_min_fee` continues to also match `expenses` so those gigs stay rejected by `FeeFilter`. `is_negotiable` is strict — only `NEG` and `Negotiable` qualify.

## Pipeline placement

After Phase 1 (scrape + detail fetch), gigs go through a **fee-excluded copy** of the filter chain — every filter except `FeeFilter`. The result is partitioned by `parse_min_fee` outcome:

| `parse_min_fee` result | Action |
|---|---|
| ≥ `MIN_FEE` | Normal path: continue to Phase 3, send application email immediately (unchanged behavior). |
| `None` and `is_negotiable(fee) == True` | NEG path: render draft, write `neg_pending` row, send Telegram alert. |
| `None` and `is_negotiable(fee) == False` (expenses-only, blank, garbled) | Reject (unchanged). |
| `< MIN_FEE` numeric | Reject (unchanged). |

NEG gigs must pass **every other filter** (`SeenFilter`, `SundayTimeFilter`, `CalendarFilter`, `AvailabilityFilter`, `BlacklistFilter`, `PostcodeFilter`). The fee-excluded chain handles this — if the date conflicts with a calendar block, or the postcode is too far, the NEG gig is silently dropped exactly like a normal gig would be.

The cleanest implementation: `GigFilterChain.apply(gigs, exclude={FeeFilter})` or a parallel chain constructed without `FeeFilter`. Exact API is a plan-level detail.

## Email template

New file: `organist_bot/templates/negotiation.html.j2`. Mirrors `application.html.j2`'s tone and structure, with one new paragraph proposing the fee:

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

Subject: `Application for Organist Position – {gig.date}` (same string used by `apply_to_gig`, so threading stays consistent).

Template variables: `gig.contact`, `gig.date`, `applicant_name`, `applicant_mobile`, `applicant_video_1`, `applicant_video_2`, `negotiable_fee`.

## Configuration

Two new fields on `Settings` (`organist_bot/config.py`):

- `negotiable_fee: int = 120` — proposed fee for NEG drafts. Default 120.
- `enable_neg_drafts: bool = True` — kill-switch. When false, NEG gigs revert to today's behavior (rejected by `FeeFilter`).

`runtime_config_store` accepts `"negotiable_fee"` as a writable key, so the unified agent's existing `manage_config` tool can update it without an `.env` edit or process restart. Read pattern: `runtime_config.get("negotiable_fee", settings.negotiable_fee)`.

`.env.example` and `CLAUDE.md` updated per the project's add-config-field convention.

## Persistence

`applications.json` gets a new status `"neg_pending"` alongside the existing `applied`, `accepted`, `no_response`, `declined`, `rejected`.

A NEG-pending row shape:

```json
{
  "gig_id": "abc123def456",
  "status": "neg_pending",
  "gig": { "header": "...", "organisation": "...", "date": "2026-07-12", "fee": "NEG", ... },
  "draft_subject": "Application for Organist Position – 2026-07-12",
  "draft_body": "<rendered HTML>",
  "negotiable_fee": 120,
  "created_at": "2026-06-09T14:33:00Z",
  "decided_at": null,
  "decision": null
}
```

`gig_id` is `hashlib.sha256(gig.link.encode()).hexdigest()[:12]` — deterministic, short enough to type in Telegram, no schema migration of existing rows required.

**Why store the rendered `draft_body` at draft time** rather than re-rendering at send time: ensures the bytes you approved in Telegram are the bytes that go out. If `runtime_config` changes between draft and approval (e.g. you bump `negotiable_fee` from 120 to 150), the existing draft still goes out at £120 unless you explicitly edit it.

### `application_store` API additions

- `record_neg_pending(gig, draft_subject, draft_body, negotiable_fee) -> str` — writes the row, returns the `gig_id`.
- `list_neg_pending() -> list[dict]` — all rows with `status == "neg_pending"`.
- `transition_neg_pending(gig_id, *, to: Literal["applied","rejected","expired"], decided_at, sent_body=None)` — flips status. On `to="applied"`, overwrites `draft_body` with `sent_body` (if provided, for the edit case) and sets the standard `applied_at` field so `get_income_forecast` and `manage_applications` see it like any other application.

`expire_past_applied()` is extended to also flip `neg_pending` rows whose gig date is in the past to `"expired"`. The same sweep runs every tick after Phase 3.

### Idempotency

All three transition tools check `status == "neg_pending"` before acting. A second `approve_neg_application` call on the same `gig_id` returns "already sent at X" instead of double-sending. No locking required: every transition function checks the current status before acting, so a race between the scheduler's `expire_past_applied` sweep and an agent-driven `approve_neg_application` resolves to whichever runs first — the second observes the new status and returns a "already <decision>" message.

## Telegram alert

One alert per NEG gig, sent at the end of `main.py`'s tick after `record_neg_pending`:

```
🟡 NEG draft pending — id: abc123def456

Gig: St. Mary's Sunday Service · 2026-07-12 · 10:00
Contact: Jane Smith <jane@stmarys.org>

Subject: Application for Organist Position – 2026-07-12

[full rendered draft body, HTML stripped to plain text]

Reply:
  • "approve abc123def456" to send as-is
  • "edit abc123def456: <new body>" to send a revised version
  • "reject abc123def456" to skip
```

Three NEG gigs in one tick produce three separate messages — easier to act on individually than a batched list.

## Unified-agent tools

Four new tools on `unified_agent.py`. All use the existing `confirmed=false/true` idiom from `add_gig` — first call shows a preview and asks for confirmation, second call with `confirmed=true` acts.

| Tool | Inputs | Behavior |
|---|---|---|
| `list_neg_pending` | — | Returns all `neg_pending` rows with `gig_id`, gig summary, and a draft-body preview. |
| `approve_neg_application` | `gig_id`, optional `confirmed: bool` | First call: returns the draft for confirmation. Confirmed: sends via `notifier._dispatch` (existing SMTP transport), transitions row to `applied`. Replies "Sent ✅". |
| `edit_neg_application` | `gig_id`, `new_body: str` (or `new_fee: int` as a shortcut that re-renders the template with the new fee), optional `confirmed: bool` | First call: shows the diff. Confirmed: sends edited body, transitions to `applied` with `sent_body=new_body`. |
| `reject_neg_application` | `gig_id`, optional `confirmed: bool` | Confirmed: transitions row to `rejected` with no email sent. |

`telegram_bot.py` is unchanged — every chat message still flows through `unified_agent.process_message`. No `CallbackQueryHandler`, no inline keyboards.

The agent's system prompt gets a short addition pointing it at these tools when the user replies with "approve <id>", "edit <id>: ...", or "reject <id>".

## Observability

- `main.py` logs `logger.info("NEG drafts queued", extra={"details": {"count": N, "gig_ids": [...]}})` after partitioning. Drains to Sheets via `SheetsLogger` like everything else; correlated by the tick's `run_id`.
- Each approval/edit/reject in the agent logs `logger.info("NEG application sent" | "NEG application rejected", extra={"details": {"gig_id": ..., "edited": bool}})`.
- No schema change to the Sheets log. `get_gig_stats` and the observability dashboard pick this up via existing columns.

## Tests

- `tests/test_filters.py`: `test_is_negotiable_detects_NEG`, `test_is_negotiable_detects_Negotiable_case_insensitive`, `test_is_negotiable_false_for_numeric_fee`, `test_is_negotiable_false_for_expenses_only`, `test_is_negotiable_false_for_blank`.
- `tests/test_negotiation.py` (new) or extend `test_notifier.py`: `test_negotiation_template_renders_with_fee`, `test_negotiation_template_uses_runtime_fee_override`.
- `tests/test_application_store.py`: `test_record_neg_pending_writes_row`, `test_transition_neg_pending_to_applied`, `test_transition_neg_pending_overrides_body_on_edit`, `test_expire_past_neg_pending`, `test_double_approve_is_idempotent`.
- `tests/test_pipeline.py`: `test_neg_gig_passes_other_filters_and_is_recorded_as_pending`, `test_neg_gig_failing_calendar_filter_is_not_drafted`, `test_neg_gig_failing_postcode_filter_is_not_drafted`, `test_normal_gig_below_min_fee_is_still_rejected`, `test_expenses_only_gig_is_still_rejected`.
- `tests/test_unified_agent.py`: `test_approve_neg_application_sends_email_and_transitions`, `test_edit_neg_application_uses_new_body`, `test_reject_neg_application_skips_send`, `test_approve_unknown_gig_id_returns_error`, `test_approve_already_sent_returns_already_sent`.

Existing `FakeTransport` is reused for SMTP assertions. Tests use a tmpdir for `applications.json`.

## File footprint

| File | Change |
|---|---|
| `organist_bot/filters.py` | Extract `_NEG_REGEX` constant, add `is_negotiable(fee_str)` helper. |
| `organist_bot/templates/negotiation.html.j2` | New. |
| `organist_bot/config.py` | Add `negotiable_fee: int = 120`, `enable_neg_drafts: bool = True`. |
| `organist_bot/runtime_config_store.py` | Allow `"negotiable_fee"` as a writable key. |
| `organist_bot/application_store.py` | Add `record_neg_pending`, `list_neg_pending`, `transition_neg_pending`; extend `expire_past_applied`. |
| `organist_bot/notifier.py` | Add `draft_negotiation(gig, negotiable_fee) -> (subject, body)`; extract a module-level `send_application_email(transport, settings, subject, body, recipient, cc=None)` helper so both `apply_to_gig` and the agent's approve-tool share one send path. |
| `main.py` | Build fee-excluded chain, partition outputs, render drafts, record `neg_pending`, send Telegram alerts. |
| `organist_bot/integrations/unified_agent.py` | Four new tools + JSON schemas; small system-prompt addition. |
| `.env.example` | Add `NEGOTIABLE_FEE=120`, `ENABLE_NEG_DRAFTS=true`. |
| `CLAUDE.md` | Document the new state, new env vars, and the new agent tools. |
| `tests/...` | New cases enumerated above. |

## Success criteria

1. A scraped gig with `fee == "NEG"` that would otherwise pass all filters generates a Telegram alert with the rendered draft and a `gig_id`.
2. Replying `approve <gig_id>` in Telegram sends the application email and transitions the row to `applied`, observable in `manage_applications` and `get_income_forecast`.
3. Replying `edit <gig_id>: <new body>` sends the edited body instead.
4. Replying `reject <gig_id>` sends nothing and transitions to `rejected`.
5. A NEG gig that fails another filter (calendar conflict, postcode too far) produces no draft and no Telegram alert.
6. `"Expenses only"` and blank-fee gigs are still rejected by `FeeFilter` — no draft generated.
7. Numeric fees below `MIN_FEE` are still rejected by `FeeFilter` — no draft generated.
8. Pending drafts whose gig date has passed are auto-flipped to `expired` by `expire_past_applied`.
9. `ENABLE_NEG_DRAFTS=false` reverts behavior to today's (NEG gigs rejected).
