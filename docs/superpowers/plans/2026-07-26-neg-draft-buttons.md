# NEG Draft Accept/Edit/Reject Buttons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every NEG-fee draft alert gets tappable `Accept | Edit | Reject` buttons; free text keeps working with an optional `gig_id` (resolved from context, or a tappable picker when ambiguous); every action converges on a `Confirm | Cancel` button state before anything is actually sent or rejected; an edit immediately becomes the live draft and is re-shown with fresh buttons.

**Architecture:** Business logic and state (active draft, pending-picker instruction, deterministic send/reject/view functions) all live in `unified_agent.py`. `telegram_bot.py` stays a thin Telegram-protocol adapter: one new `CallbackQueryHandler` for all `neg:*` button taps (deterministic, no LLM call except the one picker-resolution tap, which replays the original free text through the existing agent loop). The three LLM-invoked tools (`approve_neg_application`, `edit_neg_application`, `reject_neg_application`) drop their `confirmed` parameter entirely — each call now resolves a target, does its one non-destructive step (persist an edit, or render a preview), and returns buttons. The actual "send the email" / "mark rejected" step only happens via a button tap (`neg:confirm_send` / `neg:confirm_reject`), never via the LLM.

**Tech Stack:** Python, `python-telegram-bot` (`CallbackQueryHandler`, `InlineKeyboardMarkup`), `anthropic` SDK, `pytest` + `pytest-asyncio`.

## Global Constraints

- Callback data format: `neg:<action>:<gig_id>` where `<action>` is one of `accept`, `edit`, `reject`, `confirm_send`, `confirm_reject`, `cancel`, `pick`.
- `approve_neg_application` / `edit_neg_application` / `reject_neg_application` no longer take a `confirmed` parameter. `gig_id` is optional in all three.
- Editing a draft (via button or free text) persists immediately as the row's current draft — before any send/reject confirmation.
- `_active_neg_draft` is persisted via `agent_state.py` (survives a bot restart). `_pending_neg_instruction` (mid-picker only) is in-memory only.
- Every callback branch calls `update.callback_query.answer()` first, and every `edit_message_*` call swallows `BadRequest` at `debug` level (never crashes the polling loop), matching the existing pattern in `telegram_bot._delete_quietly`/`on_step`.
- `neg_confirm_send`/`neg_confirm_reject` re-check `application_store.transition_neg_pending`'s return value before reporting success — this is what prevents a double-tap race from double-sending.

---

### Task 1: `application_store.update_neg_draft`

**Files:**
- Modify: `organist_bot/application_store.py` (add a new function after `transition_neg_pending`, i.e. after line 160)
- Test: `tests/test_application_store.py` (append to `class TestNegPending`, after line 522)

**Interfaces:**
- Produces: `update_neg_draft(gig_id: str, *, draft_subject: str | None = None, draft_body: str | None = None, negotiable_fee: int | None = None) -> bool`. Persists the given fields onto a `neg_pending` row without changing its `status`. Returns `False` if no `neg_pending` row with this `gig_id` exists (unknown id, or already decided) and makes no changes in that case. Task 4 calls this from `edit_neg_application`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_application_store.py`, inside `class TestNegPending` (after `test_transition_unknown_id_returns_false`, before the closing of the class at line 523):

```python
    def test_update_neg_draft_persists_body_and_fee(self):
        gig_id = store.record_neg_pending(
            _neg_gig(), draft_subject="S", draft_body="old body", negotiable_fee=120
        )
        ok = store.update_neg_draft(
            gig_id, draft_subject="New subject", draft_body="new body", negotiable_fee=150
        )
        assert ok is True
        r = store._read()[0]
        assert r["draft_subject"] == "New subject"
        assert r["draft_body"] == "new body"
        assert r["negotiable_fee"] == 150
        assert r["status"] == "neg_pending"

    def test_update_neg_draft_partial_update_leaves_other_fields(self):
        gig_id = store.record_neg_pending(
            _neg_gig(), draft_subject="S", draft_body="old body", negotiable_fee=120
        )
        store.update_neg_draft(gig_id, draft_body="only body changed")
        r = store._read()[0]
        assert r["draft_subject"] == "S"
        assert r["draft_body"] == "only body changed"
        assert r["negotiable_fee"] == 120

    def test_update_neg_draft_unknown_id_returns_false(self):
        assert store.update_neg_draft("deadbeefcafe", draft_body="x") is False

    def test_update_neg_draft_already_decided_returns_false(self):
        gig_id = store.record_neg_pending(
            _neg_gig(), draft_subject="S", draft_body="b", negotiable_fee=120
        )
        store.transition_neg_pending(gig_id, to="rejected")
        assert store.update_neg_draft(gig_id, draft_body="too late") is False
        assert store._read()[0]["draft_body"] == "b"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py -k update_neg_draft -v`

Expected: FAIL with `AttributeError: module 'organist_bot.application_store' has no attribute 'update_neg_draft'`.

- [ ] **Step 3: Implement `update_neg_draft`**

In `organist_bot/application_store.py`, add this function immediately after `transition_neg_pending` (which currently ends at line 160, right before `def update_status`):

```python
def update_neg_draft(
    gig_id: str,
    *,
    draft_subject: str | None = None,
    draft_body: str | None = None,
    negotiable_fee: int | None = None,
) -> bool:
    """Persist a revised draft onto a neg_pending row without changing its
    status. Returns False if no neg_pending row with this gig_id exists
    (unknown id, or already decided) — caller should treat False as "can't
    edit this anymore" and not proceed.
    """
    with atomic_store.file_lock(_PATH):
        records = _read()
        for r in records:
            if r.get("gig_id") != gig_id:
                continue
            if r.get("status") != "neg_pending":
                return False
            if draft_subject is not None:
                r["draft_subject"] = draft_subject
            if draft_body is not None:
                r["draft_body"] = draft_body
            if negotiable_fee is not None:
                r["negotiable_fee"] = negotiable_fee
            r["updated_at"] = _now_iso()
            _write(records)
            return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_application_store.py -v`

Expected: PASS (full file).

- [ ] **Step 5: Commit**

```bash
git add organist_bot/application_store.py tests/test_application_store.py
git commit -m "feat: add application_store.update_neg_draft"
```

---

### Task 2: Buttons on the new-draft Telegram alert

**Files:**
- Modify: `organist_bot/alert.py`
- Modify: `main.py:52-81` (`_send_neg_alert`)
- Test: `tests/test_main.py` (append to `class TestNegDrafts`, after line 869)
- Test: `tests/test_alert.py` if it exists, otherwise create it

**Interfaces:**
- Produces: `alert.send_alert(message: str, reply_markup: dict | None = None) -> None` — `reply_markup` is the raw Telegram Bot API `InlineKeyboardMarkup` shape (`{"inline_keyboard": [[{"text": ..., "callback_data": ...}, ...]]}`), included in the POST body only when given.
- `_send_neg_alert`'s draft message now carries `Accept | Edit | Reject` buttons with `callback_data` = `neg:accept:<gig_id>` / `neg:edit:<gig_id>` / `neg:reject:<gig_id>` — this exact format is what Task 5's `CallbackQueryHandler` parses.

- [ ] **Step 1: Read the existing alert test file**

`tests/test_alert.py` already exists (with real, valuable tests — a module-level regression guard plus a `class TestSendAlert:` with an autouse `_block_real_post` safety fixture). Do NOT overwrite this file. Read it first: `Read tests/test_alert.py`. Note its exact patch style — tests patch `organist_bot.alert.settings` and `organist_bot.alert._requests.post`, then inspect `mock_post.call_args.kwargs["json"]` for the payload (see `test_posts_to_telegram_when_configured` for the pattern to match).

- [ ] **Step 2: Write the failing tests**

Add these two methods to the existing `class TestSendAlert:` in `tests/test_alert.py` (after `test_network_failure_is_swallowed`, matching that class's existing style exactly — `mock_post = MagicMock()`, the same `with (patch(...), patch(...))` block shape, and `mock_post.call_args.kwargs["json"]` for payload assertions):

```python
    def test_reply_markup_included_when_given(self):
        """reply_markup is included in the POST payload when provided."""
        mock_post = MagicMock()
        buttons = {
            "inline_keyboard": [[{"text": "Accept", "callback_data": "neg:accept:abc123"}]]
        }
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = "TOKEN123"
            mock_settings.telegram_chat_id = 42
            send_alert("test message", reply_markup=buttons)

        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["json"]["reply_markup"] == buttons

    def test_reply_markup_omitted_when_not_given(self):
        """reply_markup key is absent from the payload when not provided —
        existing callers of send_alert are unaffected."""
        mock_post = MagicMock()
        with (
            patch("organist_bot.alert.settings") as mock_settings,
            patch("organist_bot.alert._requests.post", mock_post),
        ):
            mock_settings.telegram_bot_token = "TOKEN123"
            mock_settings.telegram_chat_id = 42
            send_alert("test message")

        assert "reply_markup" not in mock_post.call_args.kwargs["json"]
```

Also add this test to `tests/test_main.py`, inside `class TestNegDrafts` (after `test_neg_gig_is_recorded_as_pending_and_alerts_telegram`, which currently ends at line 869):

```python
    def test_neg_draft_alert_carries_accept_edit_reject_buttons(self, tmp_path, monkeypatch):
        mock_alert = self._run(
            self._settings(), self._mock_scraper_with_one_gig(fee="NEG"), tmp_path, monkeypatch
        )
        rows = application_store.list_neg_pending()
        gig_id = rows[0]["gig_id"]
        draft_calls = [
            c for c in mock_alert.send_alert.call_args_list if gig_id in c.args[0]
        ]
        assert len(draft_calls) == 1
        buttons = draft_calls[0].kwargs["reply_markup"]["inline_keyboard"][0]
        callback_data = {b["callback_data"] for b in buttons}
        assert callback_data == {
            f"neg:accept:{gig_id}",
            f"neg:edit:{gig_id}",
            f"neg:reject:{gig_id}",
        }
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_alert.py tests/test_main.py -k "reply_markup or buttons" -v`

Expected: FAIL — `send_alert()` doesn't accept `reply_markup` yet (`TypeError: send_alert() got an unexpected keyword argument 'reply_markup'`), and the draft alert call has no `reply_markup` kwarg to inspect.

- [ ] **Step 4: Implement**

Replace `organist_bot/alert.py`'s `send_alert` function with:

```python
def send_alert(message: str, reply_markup: dict | None = None) -> None:
    """Post a plain-text alert to the configured Telegram chat, optionally
    with an inline keyboard (reply_markup, in Telegram Bot API shape).

    No-op if telegram_bot_token or telegram_chat_id is not configured.
    Any network or API failure is caught and logged at WARNING.
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.debug("send_alert: Telegram not configured — skipping")
        return
    payload: dict = {"chat_id": settings.telegram_chat_id, "text": message}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = _requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json=payload,
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram alert returned %s", resp.status_code)
    except Exception as exc:
        logger.warning(
            "Telegram alert failed",
            extra={"error": str(exc)},
        )
```

In `main.py`, replace the `_send_neg_alert` function (currently lines 52-81) with:

```python
def _send_neg_alert(gig: Gig, gig_id: str, subject: str, body: str) -> None:
    """Two Telegram messages per NEG draft: gig details first, then the draft
    (with Accept/Edit/Reject buttons — free text still works too)."""
    org = f" — {gig.organisation}" if gig.organisation else ""
    contact_line = (
        f"Contact: {gig.contact or '(none)'} <{gig.email}>" if gig.email else "Contact: (none)"
    )
    location_line = f"Location: {gig.postcode}\n" if gig.postcode else ""
    details_msg = (
        f"🟡 NEG gig — {gig.header}{org}\n\n"
        f"Date:     {gig.date} · {gig.time}\n"
        f"Fee:      {gig.fee or 'NEG'}\n"
        f"{location_line}"
        f"{contact_line}\n"
        f"Link:     {gig.link}"
    )
    alert.send_alert(details_msg)

    # Strip HTML tags from the draft body for Telegram display.
    plain = _html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    draft_msg = (
        f"Draft email — id: {gig_id}\n\n"
        f"Subject: {subject}\n\n"
        f"{plain}\n\n"
        f"Reply:\n"
        f'  • "approve {gig_id}" to send as-is\n'
        f'  • "edit {gig_id}: <new body>" to send a revised version\n'
        f'  • "reject {gig_id}" to skip'
    )
    buttons = {
        "inline_keyboard": [
            [
                {"text": "✅ Accept", "callback_data": f"neg:accept:{gig_id}"},
                {"text": "✏️ Edit", "callback_data": f"neg:edit:{gig_id}"},
                {"text": "❌ Reject", "callback_data": f"neg:reject:{gig_id}"},
            ]
        ]
    }
    alert.send_alert(draft_msg, reply_markup=buttons)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_alert.py tests/test_main.py -v`

Expected: PASS (both files in full).

- [ ] **Step 6: Commit**

```bash
git add organist_bot/alert.py main.py tests/test_alert.py tests/test_main.py
git commit -m "feat: attach Accept/Edit/Reject buttons to NEG draft alerts"
```

---

### Task 3: NEG state, `AgentResponse.buttons`, and deterministic action functions in `unified_agent.py`

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` (multiple locations — see steps)
- Modify: `organist_bot/integrations/agent_state.py`
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Consumes: `application_store.update_neg_draft` (Task 1), `application_store.transition_neg_pending`, `application_store.list_neg_pending` (existing), the existing private helpers `_find_neg_row(gig_id) -> dict | None` and `_neg_row_lookup_error(gig_id) -> str` (unchanged, already in the file at lines 1782-1794).
- Produces (all module-level in `unified_agent.py`, consumed by Task 4 in the same file and Task 5 in `telegram_bot.py`):
  - `AgentResponse.buttons: list[list[dict]] | None` field.
  - `set_active_neg_draft(chat_id: int, gig_id: str) -> None`
  - `get_active_neg_draft(chat_id: int) -> str | None`
  - `stash_pending_neg_instruction(chat_id: int, text: str) -> None`
  - `pop_pending_neg_instruction(chat_id: int) -> str | None`
  - `neg_confirm_buttons(gig_id: str, *, send: bool) -> list[list[dict]]` — the `Confirm | Cancel` row.
  - `neg_confirm_send(gig_id: str) -> tuple[bool, str]` — sends the email and transitions to `applied`; does NOT touch Telegram.
  - `neg_confirm_reject(gig_id: str) -> tuple[bool, str]` — transitions to `rejected`.
  - `neg_draft_view(gig_id: str) -> tuple[str, list[list[dict]]] | None` — current draft text + `Accept | Edit | Reject` buttons, or `None` if the row isn't `neg_pending`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_unified_agent.py`, as a new section right after the `class TestNegTools:` block ends (it currently ends with `test_reject_confirmed_skips_send` at line 2291, right before the `# ── process_message on_step progress reporting ──` comment at line 2295):

```python
# ── NEG active-draft state, buttons, and deterministic actions ──────────────

from organist_bot.integrations import unified_agent  # noqa: E402


class TestNegActiveDraftState:
    def test_set_and_get_active_neg_draft(self):
        unified_agent.set_active_neg_draft(999, "abc123")
        try:
            assert unified_agent.get_active_neg_draft(999) == "abc123"
        finally:
            unified_agent._active_neg_draft.pop(999, None)

    def test_get_active_neg_draft_defaults_to_none(self):
        assert unified_agent.get_active_neg_draft(88888) is None

    def test_stash_and_pop_pending_neg_instruction(self):
        unified_agent.stash_pending_neg_instruction(999, "raise the fee to 180")
        assert unified_agent.pop_pending_neg_instruction(999) == "raise the fee to 180"
        # pop is destructive — a second pop finds nothing.
        assert unified_agent.pop_pending_neg_instruction(999) is None


class TestNegConfirmButtons:
    def test_send_buttons_use_confirm_send_callback(self):
        buttons = unified_agent.neg_confirm_buttons("abc123", send=True)
        assert buttons == [
            [
                {"text": "Confirm", "callback_data": "neg:confirm_send:abc123"},
                {"text": "Cancel", "callback_data": "neg:cancel:abc123"},
            ]
        ]

    def test_reject_buttons_use_confirm_reject_callback(self):
        buttons = unified_agent.neg_confirm_buttons("abc123", send=False)
        assert buttons == [
            [
                {"text": "Confirm", "callback_data": "neg:confirm_reject:abc123"},
                {"text": "Cancel", "callback_data": "neg:cancel:abc123"},
            ]
        ]


class TestNegDeterministicActions:
    async def test_neg_confirm_send_success(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch("organist_bot.integrations.unified_agent.send_application_email") as mock_send:
            ok, msg = await unified_agent.neg_confirm_send(gig_id)
        assert ok is True
        assert "sent" in msg.lower()
        mock_send.assert_called_once()
        assert application_store._read()[0]["status"] == "applied"

    async def test_neg_confirm_send_unknown_id(self, neg_store):
        ok, msg = await unified_agent.neg_confirm_send("deadbeefcafe")
        assert ok is False
        assert "no draft found" in msg.lower()

    async def test_neg_confirm_send_already_decided(self, neg_store):
        gig_id = _seed_neg_pending()
        application_store.transition_neg_pending(gig_id, to="rejected")
        ok, msg = await unified_agent.neg_confirm_send(gig_id)
        assert ok is False
        assert "already" in msg.lower()

    async def test_neg_confirm_send_failure_keeps_row_pending(self, neg_store):
        gig_id = _seed_neg_pending()
        with patch(
            "organist_bot.integrations.unified_agent.send_application_email",
            side_effect=RuntimeError("smtp down"),
        ):
            ok, msg = await unified_agent.neg_confirm_send(gig_id)
        assert ok is False
        assert "failed" in msg.lower()
        assert application_store._read()[0]["status"] == "neg_pending"

    def test_neg_confirm_reject_success(self, neg_store):
        gig_id = _seed_neg_pending()
        ok, msg = unified_agent.neg_confirm_reject(gig_id)
        assert ok is True
        assert "rejected" in msg.lower()
        assert application_store._read()[0]["status"] == "rejected"

    def test_neg_confirm_reject_already_decided(self, neg_store):
        gig_id = _seed_neg_pending()
        application_store.transition_neg_pending(gig_id, to="applied")
        ok, msg = unified_agent.neg_confirm_reject(gig_id)
        assert ok is False

    def test_neg_draft_view_returns_text_and_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        view = unified_agent.neg_draft_view(gig_id)
        assert view is not None
        text, buttons = view
        assert gig_id in text
        assert buttons == [
            [
                {"text": "Accept", "callback_data": f"neg:accept:{gig_id}"},
                {"text": "Edit", "callback_data": f"neg:edit:{gig_id}"},
                {"text": "Reject", "callback_data": f"neg:reject:{gig_id}"},
            ]
        ]

    def test_neg_draft_view_none_when_not_pending(self, neg_store):
        assert unified_agent.neg_draft_view("deadbeefcafe") is None
```

`TestNegDeterministicActions` uses the `neg_store` fixture (already defined at module level in this file, near `class TestNegTools:` — fixtures are visible to any test class in the same module, no import needed).

Also add this to `tests/test_unified_agent.py`'s `AgentResponse` usage check — add a standalone test near the top-level `TestAgentStatePersistence` class (any top-level location is fine):

```python
def test_agent_response_buttons_defaults_to_none():
    from organist_bot.integrations.unified_agent import AgentResponse

    assert AgentResponse(text="hi").buttons is None
    assert AgentResponse(text="hi", buttons=[[{"text": "A", "callback_data": "x"}]]).buttons == [
        [{"text": "A", "callback_data": "x"}]
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -k "ActiveDraft or ConfirmButtons or DeterministicActions or agent_response_buttons" -v`

Expected: FAIL — `AttributeError` for every new name (`set_active_neg_draft`, `get_active_neg_draft`, `stash_pending_neg_instruction`, `pop_pending_neg_instruction`, `neg_confirm_buttons`, `neg_confirm_send`, `neg_confirm_reject`, `neg_draft_view`), and `AgentResponse(...).buttons` fails with `TypeError: __init__() got an unexpected keyword argument 'buttons'`.

- [ ] **Step 3: Implement**

**3a.** In `organist_bot/integrations/unified_agent.py`, add `buttons` to the `AgentResponse` dataclass (currently lines 700-704):

```python
@dataclass
class AgentResponse:
    text: str | None = None
    file_path: str | None = None
    file_caption: str | None = None
    buttons: list[list[dict]] | None = None
```

**3b.** Add the new per-chat state dicts right after `_last_application_listing` (currently line 711, right before the `# Chats whose persisted reference-context has been loaded this process.` comment on line 713):

```python
_active_neg_draft: dict[int, str] = {}
_pending_neg_instruction: dict[int, str] = {}
```

**3c.** Update `_hydrate_chat` (currently lines 717-737) to also restore `active_neg_draft`:

```python
def _hydrate_chat(chat_id: int) -> None:
    """Load persisted last_* context for chat_id into the in-memory dicts, once
    per process, so a bot restart doesn't drop the user's reference context
    (e.g. "email that invoice" after a restart)."""
    if chat_id in _hydrated:
        return
    _hydrated.add(chat_id)
    try:
        persisted = agent_state.load_chat(chat_id)
    except Exception:
        logger.warning("agent_state: failed to load chat %s context", chat_id, exc_info=True)
        return
    if chat_id not in _last_invoice and persisted.get("last_invoice") is not None:
        _last_invoice[chat_id] = persisted["last_invoice"]
    if chat_id not in _last_gig_listing and persisted.get("last_gig_listing") is not None:
        _last_gig_listing[chat_id] = persisted["last_gig_listing"]
    if (
        chat_id not in _last_application_listing
        and persisted.get("last_application_listing") is not None
    ):
        _last_application_listing[chat_id] = persisted["last_application_listing"]
    if chat_id not in _active_neg_draft and persisted.get("active_neg_draft") is not None:
        _active_neg_draft[chat_id] = persisted["active_neg_draft"]
```

**3d.** Update `_persist_chat` (currently lines 740-753) to also save `active_neg_draft`:

```python
def _persist_chat(chat_id: int) -> None:
    """Write the chat's current last_* reference-context to disk (best-effort —
    a persistence failure must never break the user's reply)."""
    try:
        agent_state.save_chat(
            chat_id,
            {
                "last_invoice": _last_invoice.get(chat_id),
                "last_gig_listing": _last_gig_listing.get(chat_id),
                "last_application_listing": _last_application_listing.get(chat_id),
                "active_neg_draft": _active_neg_draft.get(chat_id),
            },
        )
    except Exception:
        logger.warning("agent_state: failed to persist chat %s context", chat_id, exc_info=True)
```

**3e.** In `organist_bot/integrations/agent_state.py`, add `"active_neg_draft"` to `_KEYS` (currently line 18):

```python
_KEYS = ("last_invoice", "last_gig_listing", "last_application_listing", "active_neg_draft")
```

**3f.** Update `reset_conversation` (currently lines 2166-2172) to also clear the new state:

```python
def reset_conversation(chat_id: int) -> None:
    _histories.pop(chat_id, None)
    _last_invoice.pop(chat_id, None)
    _last_gig_listing.pop(chat_id, None)
    _last_application_listing.pop(chat_id, None)
    _active_neg_draft.pop(chat_id, None)
    _pending_neg_instruction.pop(chat_id, None)
    _hydrated.discard(chat_id)
    agent_state.save_chat(chat_id, {})
```

**3g.** Add the new public functions right after `_neg_row_lookup_error` (currently lines 1789-1794, immediately before `@_handler("list_neg_pending")` at line 1797):

```python
def set_active_neg_draft(chat_id: int, gig_id: str) -> None:
    _active_neg_draft[chat_id] = gig_id


def get_active_neg_draft(chat_id: int) -> str | None:
    return _active_neg_draft.get(chat_id)


def stash_pending_neg_instruction(chat_id: int, text: str) -> None:
    _pending_neg_instruction[chat_id] = text


def pop_pending_neg_instruction(chat_id: int) -> str | None:
    return _pending_neg_instruction.pop(chat_id, None)


def _draft_buttons(gig_id: str) -> list[list[dict]]:
    return [
        [
            {"text": "Accept", "callback_data": f"neg:accept:{gig_id}"},
            {"text": "Edit", "callback_data": f"neg:edit:{gig_id}"},
            {"text": "Reject", "callback_data": f"neg:reject:{gig_id}"},
        ]
    ]


def neg_confirm_buttons(gig_id: str, *, send: bool) -> list[list[dict]]:
    confirm_action = "confirm_send" if send else "confirm_reject"
    return [
        [
            {"text": "Confirm", "callback_data": f"neg:{confirm_action}:{gig_id}"},
            {"text": "Cancel", "callback_data": f"neg:cancel:{gig_id}"},
        ]
    ]


async def neg_confirm_send(gig_id: str) -> tuple[bool, str]:
    """Send the current draft for gig_id and transition it to applied.

    Called by the deterministic Telegram button handler, never by the LLM —
    this is the one place a NEG email actually gets sent.
    """
    row = _find_neg_row(gig_id)
    if row is None:
        return False, _neg_row_lookup_error(gig_id)
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
        logger.exception("NEG confirm_send: send failed", extra={"gig_id": gig_id})
        return False, f"Send failed: {exc}"
    ok = application_store.transition_neg_pending(gig_id, to="applied", sent_body=row["draft_body"])
    if not ok:
        return False, "Already sent or no longer pending."
    logger.info("NEG application sent", extra={"gig_id": gig_id})
    return True, f"Sent to {row.get('email')}."


def neg_confirm_reject(gig_id: str) -> tuple[bool, str]:
    """Transition gig_id to rejected. Called by the deterministic button handler."""
    row = _find_neg_row(gig_id)
    if row is None:
        return False, _neg_row_lookup_error(gig_id)
    ok = application_store.transition_neg_pending(gig_id, to="rejected")
    if not ok:
        return False, "Already decided."
    logger.info("NEG application rejected", extra={"gig_id": gig_id})
    return True, "Draft rejected — no email sent."


def neg_draft_view(gig_id: str) -> tuple[str, list[list[dict]]] | None:
    """Current draft text + Accept/Edit/Reject buttons for gig_id, or None if
    it's not a pending draft (already decided or unknown id)."""
    row = _find_neg_row(gig_id)
    if row is None:
        return None
    text = (
        f"Draft email — id: {gig_id}\n\n"
        f"Subject: {row.get('draft_subject')}\n\n"
        f"{_neg_body_as_text(row.get('draft_body') or '')}"
    )
    return text, _draft_buttons(gig_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -v`

Expected: PASS (full file — this also confirms `_neg_gig`/`neg_store`/`_seed_neg_pending` fixtures and `application_store` import already used by `TestNegTools` are still available at the point these new classes are added).

- [ ] **Step 5: Commit**

```bash
git add organist_bot/integrations/unified_agent.py organist_bot/integrations/agent_state.py tests/test_unified_agent.py
git commit -m "feat: add NEG active-draft state and deterministic confirm/reject/view actions"
```

---

### Task 4: Update the three NEG tools to drop `confirmed`, resolve `gig_id`, and return buttons

**Files:**
- Modify: `organist_bot/integrations/unified_agent.py` (TOOLS schema, the three `@_handler` functions, `process_message`, `SYSTEM_PROMPT`)
- Test: `tests/test_unified_agent.py`

**Interfaces:**
- Consumes: Task 3's `set_active_neg_draft`, `get_active_neg_draft`, `stash_pending_neg_instruction`, `_draft_buttons`, `neg_confirm_buttons`; Task 1's `application_store.update_neg_draft`.
- Produces: `_resolve_neg_gig_id(chat_id: int, gig_id: str | None) -> str | None` (returns `None` when ambiguous — caller must show a picker); every NEG tool's JSON result can now carry an optional `"buttons"` key and an optional `"needs_pick": true` flag, both understood by `process_message`.

- [ ] **Step 1: Write the failing tests**

Replace the entire body of `class TestNegTools:` in `tests/test_unified_agent.py` (currently spans from its `class TestNegTools:` declaration through `test_reject_confirmed_skips_send`, i.e. from the line right after `class TestNegTools:` up to — but not including — the `# ── NEG active-draft state...` section added in Task 3) with:

```python
class TestNegTools:
    async def test_list_neg_pending_returns_pending_rows(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["list_neg_pending"]({}, 1))
        assert gig_id in out["result"]
        assert "Test" in out["result"]

    async def test_list_neg_pending_empty(self, neg_store):
        out = json.loads(await _TOOL_HANDLERS["list_neg_pending"]({}, 1))
        assert "No NEG drafts pending" in out["result"]

    async def test_approve_returns_confirm_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": gig_id}, 1))
        assert "confirm" in out["result"].lower() or "will send" in out["result"].lower()
        assert out["buttons"] == [
            [
                {"text": "Confirm", "callback_data": f"neg:confirm_send:{gig_id}"},
                {"text": "Cancel", "callback_data": f"neg:cancel:{gig_id}"},
            ]
        ]
        assert application_store._read()[0]["status"] == "neg_pending"

    async def test_approve_unknown_gig_id_returns_error(self, neg_store):
        out = json.loads(
            await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": "deadbeefcafe"}, 1)
        )
        assert "no draft found" in out["result"].lower()
        assert "buttons" not in out

    async def test_approve_already_applied_returns_already(self, neg_store):
        gig_id = _seed_neg_pending()
        application_store.transition_neg_pending(gig_id, to="applied")
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({"gig_id": gig_id}, 1))
        assert "already" in out["result"].lower()

    async def test_approve_omitted_gig_id_resolves_single_pending(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({}, 1))
        assert out["buttons"][0][0]["callback_data"] == f"neg:confirm_send:{gig_id}"

    async def test_approve_omitted_gig_id_multiple_pending_needs_pick(self, neg_store):
        id_a = _seed_neg_pending(link="https://e.com/a")
        id_b = _seed_neg_pending(link="https://e.com/b")
        out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({}, 1))
        assert out.get("needs_pick") is True
        picked_ids = {row[0]["callback_data"] for row in out["buttons"]}
        assert picked_ids == {f"neg:pick:{id_a}", f"neg:pick:{id_b}"}

    async def test_approve_omitted_gig_id_uses_active_draft(self, neg_store):
        id_a = _seed_neg_pending(link="https://e.com/a")
        _seed_neg_pending(link="https://e.com/b")
        unified_agent.set_active_neg_draft(1, id_a)
        try:
            out = json.loads(await _TOOL_HANDLERS["approve_neg_application"]({}, 1))
        finally:
            unified_agent._active_neg_draft.pop(1, None)
        assert out["buttons"][0][0]["callback_data"] == f"neg:confirm_send:{id_a}"

    async def test_edit_with_new_body_persists_and_returns_draft_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(
            await _TOOL_HANDLERS["edit_neg_application"](
                {"gig_id": gig_id, "new_body": "<p>EDITED</p>"}, 1
            )
        )
        assert "EDITED" in out["result"]
        assert out["buttons"] == [
            [
                {"text": "Accept", "callback_data": f"neg:accept:{gig_id}"},
                {"text": "Edit", "callback_data": f"neg:edit:{gig_id}"},
                {"text": "Reject", "callback_data": f"neg:reject:{gig_id}"},
            ]
        ]
        r = application_store._read()[0]
        assert r["status"] == "neg_pending"
        assert "EDITED" in r["draft_body"]

    async def test_edit_requires_new_body_or_new_fee(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["edit_neg_application"]({"gig_id": gig_id}, 1))
        assert "new_body or new_fee" in out["result"]
        assert "buttons" not in out

    async def test_edit_with_new_fee_rerenders_and_persists(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(
            await _TOOL_HANDLERS["edit_neg_application"]({"gig_id": gig_id, "new_fee": 150}, 1)
        )
        assert "£150" in out["result"]
        r = application_store._read()[0]
        assert "£150" in r["draft_body"]
        assert r["negotiable_fee"] == 150
        assert r["status"] == "neg_pending"

    async def test_edit_sets_active_draft(self, neg_store):
        gig_id = _seed_neg_pending()
        await _TOOL_HANDLERS["edit_neg_application"]({"gig_id": gig_id, "new_fee": 150}, 42)
        try:
            assert unified_agent.get_active_neg_draft(42) == gig_id
        finally:
            unified_agent._active_neg_draft.pop(42, None)

    async def test_reject_returns_confirm_buttons(self, neg_store):
        gig_id = _seed_neg_pending()
        out = json.loads(await _TOOL_HANDLERS["reject_neg_application"]({"gig_id": gig_id}, 1))
        assert out["buttons"] == [
            [
                {"text": "Confirm", "callback_data": f"neg:confirm_reject:{gig_id}"},
                {"text": "Cancel", "callback_data": f"neg:cancel:{gig_id}"},
            ]
        ]
        assert application_store._read()[0]["status"] == "neg_pending"

    async def test_reject_omitted_gig_id_no_pending_returns_error(self, neg_store):
        out = json.loads(await _TOOL_HANDLERS["reject_neg_application"]({}, 1))
        assert "no draft found" in out["result"].lower() or "no neg drafts" in out["result"].lower()
```

Also add a new top-level test class for `process_message`'s picker/buttons plumbing, placed right after the `# ── process_message on_step progress reporting ──` tests block near the end of the file:

```python
# ── process_message NEG buttons/picker plumbing ─────────────────────────────


@pytest.mark.asyncio
async def test_process_message_passes_through_tool_buttons(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 424242
    unified_agent._hydrated.discard(cid)

    tool_use_block = SimpleNamespace(
        type="tool_use", name="approve_neg_application", input={"gig_id": "abc123"}, id="t1"
    )
    tool_use_response = SimpleNamespace(content=[tool_use_block], stop_reason="tool_use")
    text_block = SimpleNamespace(type="text", text="ok")
    end_turn_response = SimpleNamespace(content=[text_block], stop_reason="end_turn")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=[tool_use_response, end_turn_response])
    fake_anthropic_module = SimpleNamespace(AsyncAnthropic=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)

    buttons = [[{"text": "Confirm", "callback_data": "neg:confirm_send:abc123"}]]
    monkeypatch.setattr(
        unified_agent,
        "_execute_tool",
        AsyncMock(return_value=json.dumps({"result": "Will send.", "buttons": buttons})),
    )

    try:
        responses = await unified_agent.process_message(cid, "approve abc123")
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)

    assert responses[0].buttons == buttons


@pytest.mark.asyncio
async def test_process_message_stashes_instruction_on_needs_pick(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from organist_bot.integrations import agent_state, unified_agent

    monkeypatch.setattr(agent_state, "_PATH", tmp_path / "agent_state.json")
    cid = 535353
    unified_agent._hydrated.discard(cid)
    unified_agent._pending_neg_instruction.pop(cid, None)

    tool_use_block = SimpleNamespace(
        type="tool_use", name="approve_neg_application", input={}, id="t1"
    )
    tool_use_response = SimpleNamespace(content=[tool_use_block], stop_reason="tool_use")
    text_block = SimpleNamespace(type="text", text="ok")
    end_turn_response = SimpleNamespace(content=[text_block], stop_reason="end_turn")

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=[tool_use_response, end_turn_response])
    fake_anthropic_module = SimpleNamespace(AsyncAnthropic=MagicMock(return_value=fake_client))
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic_module)

    picker_buttons = [[{"text": "A", "callback_data": "neg:pick:aaa"}]]
    monkeypatch.setattr(
        unified_agent,
        "_execute_tool",
        AsyncMock(
            return_value=json.dumps(
                {"result": "Which draft?", "buttons": picker_buttons, "needs_pick": True}
            )
        ),
    )

    try:
        responses = await unified_agent.process_message(cid, "approve it")
        assert unified_agent.pop_pending_neg_instruction(cid) == "approve it"
    finally:
        unified_agent._histories.pop(cid, None)
        unified_agent._hydrated.discard(cid)
        unified_agent._pending_neg_instruction.pop(cid, None)

    assert responses[0].buttons == picker_buttons
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -k "TestNegTools or needs_pick or passes_through_tool_buttons" -v`

Expected: FAIL — `approve_neg_application`/`edit_neg_application`/`reject_neg_application` still require `gig_id` and return the old `confirmed`-style text with no `buttons` key; `process_message` doesn't yet read `buttons`/`needs_pick` from tool results.

- [ ] **Step 3: Implement**

**3a.** In the `TOOLS` list, replace the three NEG-tool entries (currently lines 579-635, `approve_neg_application` through `reject_neg_application`) with:

```python
    {
        "name": "approve_neg_application",
        "description": (
            "Show a Confirm/Cancel prompt to send a NEG-fee application draft "
            "as-is to the gig contact. Omit gig_id if the user didn't specify "
            "one — it resolves automatically, or the user is shown a picker "
            "if more than one draft is pending. The actual send only happens "
            "when the user taps Confirm — never call this a second time to "
            "'confirm' it yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {
                    "type": "string",
                    "description": "12-char id from the Telegram alert or list_neg_pending. Omit if not specified by the user.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "edit_neg_application",
        "description": (
            "Revise a NEG-fee application draft. Provide either new_body "
            "(replace the whole HTML body) OR new_fee (re-render the "
            "template with a different £ amount). The revision is saved "
            "immediately and shown back with Accept/Edit/Reject buttons — "
            "it is not sent until the user taps Accept then Confirm. Omit "
            "gig_id if the user didn't specify one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {"type": "string"},
                "new_body": {"type": "string", "description": "Replacement HTML body."},
                "new_fee": {
                    "type": "integer",
                    "description": "Re-render the negotiation template with this fee.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "reject_neg_application",
        "description": (
            "Show a Confirm/Cancel prompt to reject a NEG-fee application "
            "draft without sending any email. Omit gig_id if the user "
            "didn't specify one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "gig_id": {"type": "string"},
            },
            "required": [],
        },
    },
```

**3b.** Add `_resolve_neg_gig_id` and `_neg_picker_response` right after the `neg_draft_view` function added in Task 3 (i.e., immediately before `@_handler("list_neg_pending")`):

```python
def _resolve_neg_gig_id(chat_id: int, gig_id: str | None) -> str | None:
    """Resolve which draft a NEG tool call targets when gig_id is omitted.

    Returns the resolved gig_id, or None if ambiguous — caller must show a
    picker (via _neg_picker_response) in that case.
    """
    if gig_id:
        set_active_neg_draft(chat_id, gig_id)
        return gig_id
    pending = application_store.list_neg_pending()
    if len(pending) == 1:
        return pending[0]["gig_id"]
    active = get_active_neg_draft(chat_id)
    if active and any(r["gig_id"] == active for r in pending):
        return active
    return None


def _neg_picker_response(pending: list[dict]) -> str:
    if not pending:
        return json.dumps({"result": "No NEG drafts pending."})
    buttons = [
        [
            {
                "text": f"{r.get('header', '?')[:30]} ({r['gig_id']})",
                "callback_data": f"neg:pick:{r['gig_id']}",
            }
        ]
        for r in pending
    ]
    return json.dumps(
        {"result": "Which draft did you mean?", "buttons": buttons, "needs_pick": True}
    )
```

**3c.** Replace `_handle_approve_neg` (currently lines 1812-1852) with:

```python
@_handler("approve_neg_application")
async def _handle_approve_neg(input_data: dict, chat_id: int) -> str:
    gig_id = _resolve_neg_gig_id(chat_id, input_data.get("gig_id") or None)
    if gig_id is None:
        return _neg_picker_response(application_store.list_neg_pending())

    row = _find_neg_row(gig_id)
    if row is None:
        return json.dumps({"result": _neg_row_lookup_error(gig_id)})

    return json.dumps(
        {
            "result": (
                f"Will send this draft to {row.get('email')}.\n\n"
                f"Subject: {row.get('draft_subject')}\n\n"
                f"{_neg_body_as_text(row.get('draft_body') or '')}"
            ),
            "buttons": neg_confirm_buttons(gig_id, send=True),
        }
    )
```

**3d.** Replace `_handle_edit_neg` (currently lines 1855-1915) with:

```python
@_handler("edit_neg_application")
async def _handle_edit_neg(input_data: dict, chat_id: int) -> str:
    gig_id = _resolve_neg_gig_id(chat_id, input_data.get("gig_id") or None)
    if gig_id is None:
        return _neg_picker_response(application_store.list_neg_pending())

    new_body: str | None = input_data.get("new_body")
    new_fee: int | None = input_data.get("new_fee")

    row = _find_neg_row(gig_id)
    if row is None:
        return json.dumps({"result": _neg_row_lookup_error(gig_id)})

    if new_body is None and new_fee is None:
        return json.dumps({"result": "Provide either new_body or new_fee."})

    if new_fee is not None:
        gig = Gig(
            header=row.get("header", ""),
            organisation=row.get("organisation", ""),
            locality="",
            date=row.get("date", ""),
            time=row.get("time", ""),
            fee=row.get("fee", ""),
            link=row.get("url", ""),
            contact=row.get("contact") or None,
            email=row.get("email", ""),
            postcode=row.get("postcode", ""),
        )
        notifier = Notifier(settings, SMTPTransport(password=settings.email_password))
        _, new_body = notifier.draft_negotiation(gig, negotiable_fee=int(new_fee))

    assert new_body is not None
    application_store.update_neg_draft(
        gig_id,
        draft_body=new_body,
        negotiable_fee=int(new_fee) if new_fee is not None else None,
    )
    set_active_neg_draft(chat_id, gig_id)

    return json.dumps(
        {
            "result": (
                f"Revised draft for '{row.get('header')}':\n\n{_neg_body_as_text(new_body)}"
            ),
            "buttons": _draft_buttons(gig_id),
        }
    )
```

**3e.** Replace `_handle_reject_neg` (currently lines 1918-1939) with:

```python
@_handler("reject_neg_application")
async def _handle_reject_neg(input_data: dict, chat_id: int) -> str:
    gig_id = _resolve_neg_gig_id(chat_id, input_data.get("gig_id") or None)
    if gig_id is None:
        return _neg_picker_response(application_store.list_neg_pending())

    row = _find_neg_row(gig_id)
    if row is None:
        return json.dumps({"result": _neg_row_lookup_error(gig_id)})

    return json.dumps(
        {
            "result": f"Reject NEG draft for '{row.get('header')}'?",
            "buttons": neg_confirm_buttons(gig_id, send=False),
        }
    )
```

**3f.** In `process_message`'s `_VERBATIM_RESPONSE_TOOLS` block (currently lines 2135-2142), pass `buttons` through and stash the pending instruction on `needs_pick`:

```python
            if block.name in _VERBATIM_RESPONSE_TOOLS:
                try:
                    data = json.loads(result)
                    if "result" in data:
                        responses.append(
                            AgentResponse(text=data["result"], buttons=data.get("buttons"))
                        )
                        if data.get("needs_pick"):
                            stash_pending_neg_instruction(chat_id, text)
                        result = json.dumps({"result": "Listing sent to user."})
                except (json.JSONDecodeError, KeyError):
                    pass
```

**3g.** Replace the `## NEG-fee drafts` section of `SYSTEM_PROMPT` (currently lines 103-107) with:

```
## NEG-fee drafts
- "What NEG drafts are pending?" → list_neg_pending.
- "Approve <id>" / "approve it" / "send it" → approve_neg_application(gig_id if the user gave one, otherwise omit it).
- "Edit <id>: <new text>" / "raise the fee to 150" → edit_neg_application(gig_id if given, new_body or new_fee).
- "Reject <id>" / "reject it" → reject_neg_application(gig_id if given, otherwise omit it).
- Every one of these tools returns tappable buttons for the user to actually confirm sending or rejecting — never ask the user to reply "confirmed" yourself, and never call any of these tools a second time to "confirm" something. The buttons handle that.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_unified_agent.py -v`

Expected: PASS (full file).

- [ ] **Step 5: Run the full test suite and lint/type-check**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q && ruff check . && ruff format --check . && mypy organist_bot/`

Expected: all pass. If `ruff format --check` fails only on files just edited, run `ruff format` on them and re-check.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/unified_agent.py tests/test_unified_agent.py
git commit -m "feat: NEG tools drop confirmed param, resolve gig_id, return buttons"
```

---

### Task 5: `CallbackQueryHandler` for `neg:*` button taps in `telegram_bot.py`

**Files:**
- Modify: `organist_bot/integrations/telegram_bot.py`
- Test: `tests/test_telegram_integration.py`

**Interfaces:**
- Consumes (all from Task 3/4, module `unified_agent`): `set_active_neg_draft`, `pop_pending_neg_instruction`, `neg_confirm_buttons`, `neg_confirm_send`, `neg_confirm_reject`, `neg_draft_view`, `process_message`.
- Produces: `handle_neg_callback(update, context) -> None`, registered as `CallbackQueryHandler(handle_neg_callback, pattern=r"^neg:")` in `run()`. `handle_message`'s response loop now sends `resp.buttons` as an inline keyboard.

- [ ] **Step 1: Write the failing tests**

First, extend the shared `_make_context()` helper (currently lines 24-29) so its mock `context.bot` also has `edit_message_reply_markup` and `send_message` as `AsyncMock` — without this, `await context.bot.edit_message_reply_markup(...)` in the new handler raises `TypeError: object MagicMock can't be used in 'await' expression`, since a plain `MagicMock` auto-attribute is not awaitable:

```python
def _make_context() -> MagicMock:
    context = MagicMock()
    context.bot.edit_message_text = AsyncMock()
    context.bot.edit_message_reply_markup = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.send_document = AsyncMock()
    context.bot.send_message = AsyncMock()
    return context
```

Now add this new test class to `tests/test_telegram_integration.py`, after the existing `class TestHandleMessage:` block:

```python
# ── NEG callback handler ─────────────────────────────────────────────────────


def _make_callback_update(chat_id: int = 7973955362, data: str = "", message_id: int = 55):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.message_id = message_id
    return update


class TestHandleNegCallback:
    @pytest.fixture(autouse=True)
    def patch_settings(self):
        with patch("organist_bot.integrations.telegram_bot.settings") as mock:
            mock.telegram_chat_id = "7973955362"
            yield mock

    @pytest.mark.asyncio
    async def test_rejects_unauthorised_chat(self):
        update = _make_callback_update(chat_id=9999, data="neg:accept:abc123")
        context = _make_context()
        with patch("organist_bot.integrations.unified_agent.neg_confirm_buttons") as mock_fn:
            await handle_neg_callback(update, context)
        mock_fn.assert_not_called()
        update.callback_query.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_non_neg_callback_data(self):
        update = _make_callback_update(data="something:else")
        context = _make_context()
        await handle_neg_callback(update, context)
        context.bot.edit_message_reply_markup.assert_not_called()
        context.bot.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_accept_swaps_to_confirm_send_buttons(self):
        update = _make_callback_update(data="neg:accept:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_buttons",
            return_value=[[{"text": "Confirm", "callback_data": "neg:confirm_send:abc123"}]],
        ):
            await handle_neg_callback(update, context)
        context.bot.edit_message_reply_markup.assert_called_once()
        kwargs = context.bot.edit_message_reply_markup.call_args.kwargs
        assert kwargs["chat_id"] == 7973955362
        assert kwargs["message_id"] == 55

    @pytest.mark.asyncio
    async def test_reject_swaps_to_confirm_reject_buttons(self):
        update = _make_callback_update(data="neg:reject:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_buttons"
        ) as mock_buttons:
            mock_buttons.return_value = []
            await handle_neg_callback(update, context)
        mock_buttons.assert_called_once_with("abc123", send=False)

    @pytest.mark.asyncio
    async def test_edit_sets_active_draft_and_prompts(self):
        update = _make_callback_update(data="neg:edit:abc123")
        context = _make_context()
        with patch("organist_bot.integrations.unified_agent.set_active_neg_draft") as mock_set:
            await handle_neg_callback(update, context)
        mock_set.assert_called_once_with(7973955362, "abc123")
        context.bot.edit_message_text.assert_called_once()
        assert "what would you like to change" in context.bot.edit_message_text.call_args.kwargs[
            "text"
        ].lower()

    @pytest.mark.asyncio
    async def test_confirm_send_success_shows_sent(self):
        update = _make_callback_update(data="neg:confirm_send:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_send",
            new=AsyncMock(return_value=(True, "Sent to jane@example.com.")),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "✅" in text
        assert "Sent to jane@example.com." in text

    @pytest.mark.asyncio
    async def test_confirm_send_failure_shows_failure(self):
        update = _make_callback_update(data="neg:confirm_send:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_send",
            new=AsyncMock(return_value=(False, "Send failed: smtp down")),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "❌" in text

    @pytest.mark.asyncio
    async def test_confirm_reject_success_shows_rejected(self):
        update = _make_callback_update(data="neg:confirm_reject:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_confirm_reject",
            return_value=(True, "Draft rejected — no email sent."),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "✅" in text

    @pytest.mark.asyncio
    async def test_cancel_restores_draft_view(self):
        update = _make_callback_update(data="neg:cancel:abc123")
        context = _make_context()
        view_text = "Draft email — id: abc123"
        view_buttons = [[{"text": "Accept", "callback_data": "neg:accept:abc123"}]]
        with patch(
            "organist_bot.integrations.unified_agent.neg_draft_view",
            return_value=(view_text, view_buttons),
        ):
            await handle_neg_callback(update, context)
        kwargs = context.bot.edit_message_text.call_args.kwargs
        assert kwargs["text"] == view_text

    @pytest.mark.asyncio
    async def test_cancel_when_draft_gone(self):
        update = _make_callback_update(data="neg:cancel:abc123")
        context = _make_context()
        with patch(
            "organist_bot.integrations.unified_agent.neg_draft_view", return_value=None
        ):
            await handle_neg_callback(update, context)
        text = context.bot.edit_message_text.call_args.kwargs["text"]
        assert "no longer available" in text.lower()

    @pytest.mark.asyncio
    async def test_pick_replays_instruction_through_agent(self):
        update = _make_callback_update(data="neg:pick:abc123")
        context = _make_context()
        with (
            patch("organist_bot.integrations.unified_agent.set_active_neg_draft") as mock_set,
            patch(
                "organist_bot.integrations.unified_agent.pop_pending_neg_instruction",
                return_value="raise the fee to 180",
            ),
            patch(
                "organist_bot.integrations.unified_agent.process_message",
                new=AsyncMock(return_value=[AgentResponse(text="Revised draft.", buttons=None)]),
            ) as mock_pm,
        ):
            await handle_neg_callback(update, context)
        mock_set.assert_called_once_with(7973955362, "abc123")
        mock_pm.assert_called_once_with(7973955362, "For gig abc123: raise the fee to 180")
        context.bot.send_message.assert_called_once()
        assert context.bot.send_message.call_args.kwargs["text"] == "Revised draft."

    @pytest.mark.asyncio
    async def test_pick_with_no_pending_instruction_asks_to_repeat(self):
        update = _make_callback_update(data="neg:pick:abc123")
        context = _make_context()
        with (
            patch("organist_bot.integrations.unified_agent.set_active_neg_draft"),
            patch(
                "organist_bot.integrations.unified_agent.pop_pending_neg_instruction",
                return_value=None,
            ),
        ):
            await handle_neg_callback(update, context)
        text = context.bot.send_message.call_args.kwargs["text"]
        assert "lost track" in text.lower()

    @pytest.mark.asyncio
    async def test_edit_message_badrequest_is_swallowed(self):
        update = _make_callback_update(data="neg:cancel:abc123")
        context = _make_context()
        context.bot.edit_message_text = AsyncMock(side_effect=BadRequest("message is not found"))
        with patch(
            "organist_bot.integrations.unified_agent.neg_draft_view",
            return_value=("text", []),
        ):
            await handle_neg_callback(update, context)  # must not raise
```

Also add a test to `class TestHandleMessage:` confirming `resp.buttons` reaches `reply_text` as an inline keyboard:

```python
    @pytest.mark.asyncio
    async def test_sends_buttons_when_response_has_them(self):
        update = _make_update(text="approve abc123")
        context = _make_context()
        responses = [
            AgentResponse(
                text="Will send this draft.",
                buttons=[[{"text": "Confirm", "callback_data": "neg:confirm_send:abc123"}]],
            )
        ]
        with patch(
            "organist_bot.integrations.unified_agent.process_message",
            new=AsyncMock(return_value=responses),
        ):
            await handle_message(update, context)
        last_call = update.message.reply_text.call_args_list[-1]
        markup = last_call.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].text == "Confirm"
        assert markup.inline_keyboard[0][0].callback_data == "neg:confirm_send:abc123"
```

Update the import line at the top of `tests/test_telegram_integration.py` to also import `handle_neg_callback`:

```python
from organist_bot.integrations.telegram_bot import _is_authorised, handle_message, handle_neg_callback
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_telegram_integration.py -v`

Expected: FAIL — `handle_neg_callback` doesn't exist yet (`ImportError`), and `handle_message` doesn't send `reply_markup`.

- [ ] **Step 3: Implement**

In `organist_bot/integrations/telegram_bot.py`:

**3a.** Update imports (currently lines 14-24):

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
)
from telegram.ext import (
    filters as tg_filters,
)
```

**3b.** Add a button-markup helper and update `_reply` to accept `reply_markup` (replace the current `_reply` function, lines 57-70):

```python
def _build_reply_markup(buttons: list[list[dict]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(**cell) for cell in row] for row in buttons]
    )


async def _reply(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Send with Markdown formatting; fall back to plain text if the LLM's
    output contains characters (stray `_`, `*`, backticks) that Telegram's
    legacy Markdown parser can't balance into valid entities."""
    try:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except BadRequest as exc:
        if "can't parse entities" not in str(exc).lower():
            raise
        logger.warning(
            "Telegram: Markdown parse failed, resending as plain text",
            extra={"error": str(exc)},
        )
        await message.reply_text(text, reply_markup=reply_markup)
```

**3c.** In `handle_message`, pass buttons through on the text-send line (currently line 129-130, `if resp.text: await _reply(update.message, resp.text)`):

```python
            if resp.text:
                await _reply(
                    update.message, resp.text, reply_markup=_build_reply_markup(resp.buttons)
                )
```

**3d.** Add `handle_neg_callback` and its small edit helpers, placed after `handle_message` and before the `# ── Bot setup ──` section:

```python
# ── NEG draft callback handler ──────────────────────────────────────────────


async def _edit_buttons_quietly(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    buttons: list[list[dict]] | None,
) -> None:
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=_build_reply_markup(buttons)
        )
    except BadRequest as exc:
        logger.debug("Telegram: NEG button edit failed: %s", exc)


async def _edit_text_quietly(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    text: str,
    buttons: list[list[dict]] | None = None,
) -> None:
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=_build_reply_markup(buttons),
        )
    except BadRequest as exc:
        logger.debug("Telegram: NEG message edit failed: %s", exc)


async def handle_neg_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.callback_query is not None
    await update.callback_query.answer()

    if not _is_authorised(update):
        _reject(update)
        return

    assert update.effective_chat is not None
    assert update.callback_query.message is not None
    chat_id = update.effective_chat.id
    message_id = update.callback_query.message.message_id

    data = update.callback_query.data or ""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "neg":
        return
    _, action, gig_id = parts

    if action == "accept":
        await _edit_buttons_quietly(
            context, chat_id, message_id, unified_agent.neg_confirm_buttons(gig_id, send=True)
        )
    elif action == "reject":
        await _edit_buttons_quietly(
            context, chat_id, message_id, unified_agent.neg_confirm_buttons(gig_id, send=False)
        )
    elif action == "edit":
        unified_agent.set_active_neg_draft(chat_id, gig_id)
        await _edit_text_quietly(
            context, chat_id, message_id, "✏️ What would you like to change about this draft?"
        )
    elif action == "confirm_send":
        ok, result = await unified_agent.neg_confirm_send(gig_id)
        await _edit_text_quietly(context, chat_id, message_id, f"{'✅' if ok else '❌'} {result}")
    elif action == "confirm_reject":
        ok, result = unified_agent.neg_confirm_reject(gig_id)
        await _edit_text_quietly(context, chat_id, message_id, f"{'✅' if ok else '❌'} {result}")
    elif action == "cancel":
        view = unified_agent.neg_draft_view(gig_id)
        if view is None:
            await _edit_text_quietly(
                context, chat_id, message_id, "This draft is no longer available."
            )
        else:
            text, buttons = view
            await _edit_text_quietly(context, chat_id, message_id, text, buttons)
    elif action == "pick":
        unified_agent.set_active_neg_draft(chat_id, gig_id)
        instruction = unified_agent.pop_pending_neg_instruction(chat_id)
        if instruction is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text="I lost track of what you asked — please repeat it for this draft.",
            )
            return
        responses = await unified_agent.process_message(
            chat_id, f"For gig {gig_id}: {instruction}"
        )
        for resp in responses:
            if resp.text:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=resp.text,
                    reply_markup=_build_reply_markup(resp.buttons),
                )
```

**3e.** Register the handler in `run()` (currently lines 140-152):

```python
def run(token: str) -> None:
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(tg_filters.TEXT & ~tg_filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_neg_callback, pattern=r"^neg:"))

    cal = unified_agent._make_calendar_client()
    if cal:
        unified_agent.sync_calendar_blocks(cal)

    logger.info("Telegram bot polling", extra={"chat_id": settings.telegram_chat_id})
    alert.send_alert("🤖 Telegram bot started")
    app.run_polling()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest tests/test_telegram_integration.py -v`

Expected: PASS (full file).

- [ ] **Step 5: Run the full test suite and lint/type-check**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com pytest --tb=short -q && ruff check . && ruff format --check . && mypy organist_bot/`

Expected: all pass. If `ruff format --check` fails only on files just edited, run `ruff format` on them and re-check.

- [ ] **Step 6: Commit**

```bash
git add organist_bot/integrations/telegram_bot.py tests/test_telegram_integration.py
git commit -m "feat: add CallbackQueryHandler for NEG draft Accept/Edit/Reject/Confirm/Cancel/Pick buttons"
```

---

## Shipping

Before Task 1, create the feature branch (this repo's `CLAUDE.md` forbids committing directly to `main`):

```bash
git checkout -b neg-draft-buttons
```

All five tasks' commits land on this branch. Once Task 5 is committed, run:

```bash
make ship
```

`make ship` runs the full local quality gate (ruff lint, ruff format --check, mypy, bandit + semgrep, pytest), pushes the branch, opens a ready-for-review PR, and enables squash auto-merge.
