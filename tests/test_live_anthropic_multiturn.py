"""Live, opt-in test of a real multi-turn tool-calling round trip against
Anthropic via LiteLLM.

Every other test in this suite fakes litellm.acompletion, so nothing exercises
LiteLLM's actual Anthropic message translation. That translation is exactly
where the original multi-provider spec's target bug lived: a tool-call turn's
assistant message has `content: None` alongside `tool_calls`, and
process_message appends `msg.model_dump()` (not `exclude_none=True`, see the
comment at unified_agent.process_message) specifically so that `content: None`
survives as an explicit key. If a future change swapped in `exclude_none=True`,
or otherwise dropped `content` from a tool-call message, a fake-response test
would not notice -- only a real second call to the Anthropic backend, fed that
exact history shape, would reject it.

Run explicitly with: pytest -m live tests/test_live_anthropic_multiturn.py
Skipped by default (see pyproject.toml's addopts) and whenever no Anthropic
API key is configured.
"""

from __future__ import annotations

import json

import pytest

from organist_bot.config import settings

pytestmark = pytest.mark.live

_MODEL = "anthropic/claude-haiku-4-5-20251001"

_ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "add_numbers",
        "description": "Add two integers and return their sum.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    },
}


@pytest.mark.skipif(not settings.anthropic_api_key, reason="no ANTHROPIC_API_KEY configured")
async def test_real_multiturn_tool_call_round_trip_survives_anthropic_translation():
    """Reproduces process_message's exact history-append shape across two real
    Anthropic calls: the first call must request add_numbers; appending that
    assistant message (via model_dump(), preserving `content: None`) plus a
    tool-result message, then sending that history back to Anthropic for a
    second real call, must not raise and must produce a final answer that
    uses the tool's result."""
    import litellm

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                "Use the add_numbers tool to compute 37 plus 5. "
                "Do not compute it yourself -- call the tool."
            ),
        }
    ]

    first = await litellm.acompletion(
        model=_MODEL,
        max_tokens=1024,
        api_key=settings.anthropic_api_key,
        messages=messages,
        tools=[_ADD_TOOL],
    )
    msg = first.choices[0].message
    assert msg.tool_calls, f"model did not call add_numbers; content was {msg.content!r}"

    tool_call = msg.tool_calls[0]
    assert tool_call.function.name == "add_numbers"
    args = json.loads(tool_call.function.arguments)

    # Mirrors process_message: append via model_dump() so `content: None`
    # stays an explicit key on this tool-call turn, not silently dropped.
    messages.append(msg.model_dump())
    messages.append(
        {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "name": "add_numbers",
            "content": json.dumps({"result": args["a"] + args["b"]}),
        }
    )

    second = await litellm.acompletion(
        model=_MODEL,
        max_tokens=1024,
        api_key=settings.anthropic_api_key,
        messages=messages,
        tools=[_ADD_TOOL],
    )
    final_text = second.choices[0].message.content
    assert final_text
    assert "42" in final_text
