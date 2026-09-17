"""Unit tests for app/routers/llm_utils.py.

Run:
    python -m pytest tests/test_utils/ -v
"""

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from app.core.settings import settings
from app.models.dataset import Dataset
from app.routers import llm_utils as llm

# ===========================================================================
# _yield_json
# ===========================================================================


def test_yield_json_appends_newline():
    line = llm._yield_json({"done": True})
    assert line.endswith("\n")
    assert json.loads(line) == {"done": True}


def test_yield_json_handles_non_serializable_via_default_str():
    dt = datetime(2026, 1, 2, 3, 4, 5)
    line = llm._yield_json({"at": dt})
    parsed = json.loads(line)
    assert "2026-01-02" in parsed["at"]


# ===========================================================================
# _tool_call_dict
# ===========================================================================


def _make_tool_call(extra_content=None):
    tc = SimpleNamespace(
        id="tc_123",
        function=SimpleNamespace(name="get_profile", arguments='{"dataset": "d1"}'),
    )
    if extra_content is not None:
        tc.extra_content = extra_content
    return tc


def test_tool_call_dict_basic_mapping():
    d = llm._tool_call_dict(_make_tool_call())
    assert d == {
        "id": "tc_123",
        "type": "function",
        "function": {"name": "get_profile", "arguments": '{"dataset": "d1"}'},
    }
    assert "extra_content" not in d


def test_tool_call_dict_includes_extra_content_when_present():
    sig = {"thought_signature": "abc"}
    d = llm._tool_call_dict(_make_tool_call(extra_content=sig))
    assert d["extra_content"] == sig


# ===========================================================================
# _sanitize_history
# ===========================================================================

PLACEHOLDER = "[chart from a previous turn — not authoritative]"


def test_sanitize_history_user_passes_through_and_system_dropped():
    history = [
        {"role": "system", "content": "sys prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]
    clean = llm._sanitize_history(history)
    assert clean == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
    ]


def test_sanitize_history_dict_content_replaced_with_placeholder():
    clean = llm._sanitize_history(
        [{"role": "assistant", "content": {"rows": [1, 2, 3]}}]
    )
    assert clean == [{"role": "assistant", "content": PLACEHOLDER}]


def test_sanitize_history_record_typed_bracket_string_replaced():
    clean = llm._sanitize_history(
        [{"role": "assistant", "content": "[chart]", "type": "record"}]
    )
    assert clean == [{"role": "assistant", "content": PLACEHOLDER}]


def test_sanitize_history_record_typed_brace_string_replaced():
    clean = llm._sanitize_history(
        [{"role": "assistant", "content": '{"chart": true}', "type": "record"}]
    )
    assert clean == [{"role": "assistant", "content": PLACEHOLDER}]


def test_sanitize_history_long_text_truncated():
    long_text = "x" * 301
    clean = llm._sanitize_history([{"role": "assistant", "content": long_text}])
    content = clean[0]["content"]
    assert content == "x" * 300 + "…[truncated]"


def test_sanitize_history_non_string_content_serialised():
    clean = llm._sanitize_history([{"role": "assistant", "content": ["a", "b"]}])
    assert clean == [{"role": "assistant", "content": '["a", "b"]'}]


def test_sanitize_history_limit_keeps_last_n():
    history = [{"role": "user", "content": f"q{i}"} for i in range(5)]
    clean = llm._sanitize_history(history, limit=2)
    assert clean == [
        {"role": "user", "content": "q3"},
        {"role": "user", "content": "q4"},
    ]


def test_sanitize_history_empty_input():
    assert llm._sanitize_history([]) == []


# ===========================================================================
# _tools_with_dataset_id
# ===========================================================================


def test_tools_with_dataset_id_exact_list():
    assert llm._tools_with_dataset_id() == ["get_profile", "validate_dsl_tool"]


# ===========================================================================
# _save_message
# ===========================================================================


def test_save_message_with_message_id_prefixes_uuid():
    session = MagicMock()
    msg = llm._save_message(
        session,
        dataset_id="dst_1",
        role="user",
        content="hi",
        message_id="client-abc",
    )
    assert msg.id.endswith("_client-abc")
    assert msg.dataset_id == "dst_1"
    assert msg.role == "user"
    session.add.assert_called_once_with(msg)
    session.commit.assert_called_once()
    session.refresh.assert_called_once_with(msg)


def test_save_message_without_message_id_uses_default_id():
    session = MagicMock()
    msg = llm._save_message(session, dataset_id="dst_1", role="assistant", content="yo")
    assert msg.id.startswith("msg_")
    assert not msg.id.endswith("None")


def test_save_message_operational_error_retries_after_rollback():
    session = MagicMock()
    session.commit.side_effect = [
        OperationalError("stale conn", {}, Exception("gone away")),
        None,
    ]
    msg = llm._save_message(session, dataset_id="dst_1", role="user", content="hi")
    session.rollback.assert_called_once()
    assert session.add.call_count == 2
    assert session.commit.call_count == 2
    session.refresh.assert_called_once_with(msg)


# ===========================================================================
# _friendly_llm_error
# ===========================================================================


@pytest.mark.parametrize(
    ("message", "expected_snippet"),
    [
        ("Invalid API key supplied", "invalid or missing"),
        ("authentication failed upstream", "invalid or missing"),
        ("unauthorized request", "invalid or missing"),
        ("Rate limit reached (429)", "(rate limited)"),
        ("quota exhausted", "(rate limited)"),
        ("provider overloaded (529)", "overloaded"),
        ("upstream busy, try again later", "overloaded"),
        ("402 insufficient balance", "balance"),
        ("payment required", "balance"),
        ("Model gpt-x not found", "model is not found"),
        ("connection refused by host", "unreachable"),
        ("request timed out", "took too long"),
        ("kaboom happened", "unexpected error"),
    ],
)
def test_friendly_llm_error_mapping(message, expected_snippet):
    friendly = llm._friendly_llm_error(Exception(message))
    assert expected_snippet in friendly


# ===========================================================================
# _build_system_prompt
# ===========================================================================


def _make_dataset(schema):
    return Dataset(user_id=1, name="test-ds", id="dst_xyz", dataset_schema=schema)


def test_build_system_prompt_includes_dataset_id_and_schema_lines():
    ds = _make_dataset({"amount": {"type": "number"}, "region": {"type": "string"}})
    prompt = llm._build_system_prompt(ds)
    assert "dst_xyz" in prompt
    assert "- amount: number" in prompt
    assert "- region: string" in prompt


def test_build_system_prompt_empty_schema_has_no_field_lines():
    ds = _make_dataset({})
    prompt = llm._build_system_prompt(ds)
    assert "dst_xyz" in prompt


def test_build_system_prompt_non_dict_schema_falls_back_to_json():
    ds = _make_dataset("weird-schema")
    prompt = llm._build_system_prompt(ds)
    assert '"weird-schema"' in prompt


# ===========================================================================
# _streamed_llm_message
# ===========================================================================


def _delta(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _chunk(delta):
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _empty_chunk():
    """Chunk with no choices at all (keep-alive style) -> skipped by consumer."""
    return SimpleNamespace(choices=[])


def _fake_client(chunks, captured):
    def create(**kwargs):
        captured.update(kwargs)
        return iter(chunks)

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )


TOOLS = [{"type": "function", "function": {"name": "get_profile"}}]


def test_streamed_llm_message_accumulates_content_across_chunks():
    chunks = [
        _chunk(_delta(content="Hello")),
        _empty_chunk(),  # empty choices -> skipped
        _chunk(_delta(content=" world")),
    ]
    captured = {}
    client = _fake_client(chunks, captured)
    message = llm._streamed_llm_message(client, [], tools=TOOLS, max_tokens=512)
    assert message.content == "Hello world"
    assert message.tool_calls is None
    assert captured["stream"] is True
    assert captured["tools"] == TOOLS
    assert captured["max_tokens"] == 512
    assert captured["model"] == settings.LLM_MODEL


def test_streamed_llm_message_stitches_tool_call_arguments():
    call_a = SimpleNamespace(
        index=0,
        id="call_1",
        function=SimpleNamespace(name="get_profile", arguments='{"dat'),
    )
    call_b = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(name=None, arguments='aset": "d1"}'),
    )
    chunks = [
        _chunk(_delta(tool_calls=[call_a])),
        _chunk(_delta(tool_calls=[call_b])),
    ]
    client = _fake_client(chunks, {})
    message = llm._streamed_llm_message(client, [], tools=TOOLS, max_tokens=10)
    assert message.content is None
    assert len(message.tool_calls) == 1
    tc = message.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.function.name == "get_profile"
    assert tc.function.arguments == '{"dataset": "d1"}'


def test_streamed_llm_message_orders_tool_calls_by_index():
    late = SimpleNamespace(
        index=1, id="call_2", function=SimpleNamespace(name="t2", arguments="{}")
    )
    early = SimpleNamespace(
        index=0, id="call_1", function=SimpleNamespace(name="t1", arguments="{}")
    )
    chunks = [_chunk(_delta(tool_calls=[late])), _chunk(_delta(tool_calls=[early]))]
    client = _fake_client(chunks, {})
    message = llm._streamed_llm_message(client, [], tools=TOOLS, max_tokens=10)
    assert [tc.id for tc in message.tool_calls] == ["call_1", "call_2"]


def test_streamed_llm_message_all_chunks_empty_yields_nones():
    # Deltas present but with no content and no tool calls.
    chunks = [_chunk(_delta()), _chunk(_delta())]
    client = _fake_client(chunks, {})
    message = llm._streamed_llm_message(client, [], tools=TOOLS, max_tokens=10)
    assert message.content is None
    assert message.tool_calls is None
