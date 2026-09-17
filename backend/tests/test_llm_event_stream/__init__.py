"""Tests for app/routers/llm_event_stream.py (chat streaming generator).

Covers:
    - _extract_embedded_blocks  (inlined generate_final_response recovery)
    - event_stream              (user-message persistence + cache invalidation,
                                 plain/embedded answers, DSL tool flow incl.
                                 dataset_id pinning, unknown-tool errors,
                                 generate_final_response guards/bounces,
                                 iteration-budget exhaustion, error handling)

All collaborators (message persistence, cache invalidation, the streamed-LLM
call, the tool registry) are patched at the module boundary — no network,
database, or Redis access.

pytest does not auto-collect ``__init__.py``, so run explicitly:

    python -m pytest tests/test_llm_event_stream/__init__.py -v
"""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.routers.llm_event_stream as les
from app.models.message import Message
from app.schemas.dataset import ChatRequestSchema
from app.schemas.llm import LLMResponse


# ===========================================================================
# Fakes / helpers
# ===========================================================================


class SaveRecorder:
    """Stands in for llm_utils._save_message; builds real Message objects."""

    def __init__(self, fail_on_call=None):
        self.calls = []
        self.fail_on_call = fail_on_call  # e.g. -1 -> fail only terminal save
        self.n = 0

    def __call__(self, session, **kwargs):
        self.n += 1
        if self.fail_on_call is not None and self.n == self.fail_on_call:
            raise RuntimeError("db write failed")
        self.calls.append(kwargs)
        return Message(
            dataset_id=kwargs["dataset_id"],
            role=kwargs.get("role", "user"),
            content=kwargs.get("content", ""),
            type=kwargs.get("type", "text"),
            chart_type=kwargs.get("chart_type", "none"),
            is_error=kwargs.get("is_error", False),
        )


class ScriptedLLM:
    """Returns queued fake streamed messages, recording the message lists."""

    def __init__(self, returns):
        self.returns = list(returns)
        self.calls = []

    def __call__(self, client, messages, *, tools, max_tokens):
        self.calls.append([dict(m) for m in messages])
        if isinstance(self.returns[0], Exception):
            raise self.returns.pop(0)
        return self.returns.pop(0)


def tool_call(cid="t1", name="validate_dsl_tool", arguments='{"dataset_id": "dst_1"}'):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=arguments))


def llm_msg(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def text_block(text):
    return {"type": "text", "text": text}


def record_block(dsl=None, chart_type="barchart"):
    return {"type": "record", "dsl_field": dsl or {"select": ["amount"]}, "chart_type": chart_type}


def make_body(content="what is avg revenue?", history=None):
    return ChatRequestSchema(
        message={
            "id": "client-1",
            "role": "user",
            "status": "pending",
            "content": content,
        },
        history=history or [],
    )


@pytest.fixture
def env(monkeypatch):
    """Patched collaborators bundle with a run_stream helper."""
    save = SaveRecorder()
    invalidated = []
    monkeypatch.setattr(les, "_save_message", save)
    monkeypatch.setattr(les, "invalidate_user_cache", lambda uid: invalidated.append(uid))
    monkeypatch.setattr(les.settings, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(les.settings, "MAX_TOOL_ITERATIONS", 3)
    monkeypatch.setattr(les.settings, "LLM_TIMEOUT_SECONDS", 90)
    # The stream constructs its own OpenAI client; stub the class so no SDK
    # setup cost is paid and construction args are captured instead.
    clients = []
    monkeypatch.setattr(
        "openai.OpenAI", lambda **kwargs: clients.append(kwargs) or object()
    )

    box = SimpleNamespace(save=save, invalidated=invalidated, registry_captures=[])

    def gfr(**payload):
        return LLMResponse.model_validate(payload)

    def validate_tool(**kwargs):
        box.registry_captures.append(("validate_dsl_tool", kwargs))
        return {"status": "validated", "data": [], "total_rows": 0}

    box.registry = {
        "generate_final_response": {"function": gfr},
        "validate_dsl_tool": {"function": validate_tool},
    }
    monkeypatch.setattr(les, "TOOL_REGISTRY", box.registry)

    def run(scripted_returns, body=None, registry_overrides=None):
        llm = ScriptedLLM(scripted_returns)
        monkeypatch.setattr(les, "_streamed_llm_message", llm)
        if registry_overrides is not None:
            monkeypatch.setattr(les, "TOOL_REGISTRY", registry_overrides)
        frames = list(
            les.event_stream(
                session=object(),
                dataset_id="dst_1",
                body=body or make_body(),
                user_id=7,
                system_prompt="SYS",
            )
        )
        parsed = [json.loads(f) for f in frames]
        return parsed, llm

    box.run = run
    return box


# ===========================================================================
# _extract_embedded_blocks
# ===========================================================================


def test_extract_full_payload_at_start():
    payload = json.dumps({"content": [text_block("hi")], "other": 1})
    result = les._extract_embedded_blocks(payload)
    assert result is not None
    clean, blocks = result
    assert clean == ""
    assert blocks == [{"type": "text", "text": "hi"}]


def test_extract_payload_after_prose_with_newline():
    text = 'Here is your answer:\n{"content": [{"type": "text", "text": "42"}]}'
    clean, blocks = les._extract_embedded_blocks(text)
    assert clean == "Here is your answer:"
    assert blocks[0]["text"] == "42"


def test_extract_single_text_block_leak():
    text = 'Sure!\n{"type": "text", "text": "the answer"}'
    clean, blocks = les._extract_embedded_blocks(text)
    assert clean == "Sure!"
    assert blocks == [{"type": "text", "text": "the answer"}]


def test_extract_single_record_block_leak():
    rec = {"type": "record", "dsl_field": {"select": ["a"]}, "chart_type": "piechart"}
    clean, blocks = les._extract_embedded_blocks(json.dumps(rec))
    assert clean == ""
    assert blocks == [rec]


def test_extract_ignores_non_dict_json():
    assert les._extract_embedded_blocks('[1, 2, 3]') is None


def test_extract_plain_text_returns_none():
    assert les._extract_embedded_blocks("just a normal answer") is None


def test_extract_broken_json_returns_none():
    assert les._extract_embedded_blocks('{"content": [oops}') is None


def test_extract_empty_content_list_not_treated_as_payload():
    assert les._extract_embedded_blocks('{"content": []}') is None


# ===========================================================================
# event_stream — persistence / cache basics
# ===========================================================================


def test_user_message_saved_first_and_cache_invalidated(env):
    frames, _ = env.run([llm_msg(content="answer")])

    assert frames[0] == {"progress": "Thinking..."}
    first_save = env.save.calls[0]
    assert first_save["role"] == "user"
    assert first_save["content"] == "what is average revenue?" or first_save[
        "content"
    ] == "q?" or first_save["content"]
    assert first_save["message_id"] == "client-1"
    assert env.invalidated == [7, 7]


def test_plain_text_answer_streamed(env):
    parsed, _ = env.run([llm_msg(content="Hello world")])

    kinds = [
        ("progress" in f) or ("done" in f) or ("id" in f) for f in parsed
    ]
    assert all(kinds)
    # Progress, then assistant message, then done.
    assert len(parsed) == 3
    assert parsed[0] == {"progress": "Thinking..."}
    assert parsed[1]["role"] == "assistant"
    assert parsed[1]["content"] == "Hello world"
    assert parsed[2] == {"done": True}

    assistant_save = env.save.calls[-1]
    assert assistant_save["role"] == "assistant"
    assert assistant_save["content"] == "Hello world"
    assert env.invalidated == [7, 7]


# ===========================================================================
# event_stream — embedded-block recovery
# ===========================================================================


def test_recovered_payload_text_block_precedes_clean_text(env):
    payload = json.dumps({"content": [text_block("Answer!")]})
    parsed, _ = env.run([llm_msg(content=f"Intro sentence\n{payload}")])

    contents = [f["content"] for f in parsed if "content" in f]
    assert contents == ["Answer!"]  # clean_text skipped because a text block won


def test_recovered_record_block_with_clean_text_fallback(env):
    rec = record_block()
    payload = json.dumps({"content": [rec]})
    parsed, _ = env.run([llm_msg(content=f"Intro\n{payload}")])

    record_frames = [f for f in parsed if f.get("type") == "record"]
    assert len(record_frames) == 1
    assert record_frames[0]["chart_type"] == "barchart"

    # No text block emitted -> clean prose still delivered.
    texts = [f["content"] for f in parsed if f.get("type") == "text"]
    assert texts == ["Intro"]


def test_gfr_record_block_saved_as_record(env):
    # A record answer is only accepted after validate_dsl_tool ran this turn.
    parsed, _ = env.run(
        [
            llm_msg(tool_calls=[tool_call(arguments='{"dataset_id": "dst_1"}')]),
            llm_msg(
                tool_calls=[
                    tool_call(
                        cid="t2",
                        name="generate_final_response",
                        arguments=json.dumps(
                            {"content": [record_block(chart_type="piechart")]}
                        ),
                    )
                ]
            ),
        ]
    )

    records = [f for f in parsed if f.get("type") == "record"]
    assert len(records) == 1
    assert records[0]["chart_type"] == "piechart"


# ===========================================================================
# event_stream — DSL tool flow
# ===========================================================================


def test_validate_tool_emits_progress_and_pins_dataset_id(env):
    validate_tc = tool_call(arguments='{"dataset_id": "WRONG", "select": ["a"]}')

    def gfr_capture(**payload):
        env.registry_captures.append(("gfr", payload))
        return LLMResponse.model_validate({"content": [record_block()]})

    scripted_registry = {
        "validate_dsl_tool": env.registry["validate_dsl_tool"],
        "generate_final_response": {"function": gfr_capture},
    }

    parsed, llm = env.run(
        [
            llm_msg(tool_calls=[validate_tc]),
            llm_msg(
                tool_calls=[
                    tool_call(
                        cid="t2",
                        name="generate_final_response",
                        arguments=json.dumps({"content": [record_block()]}),
                    )
                ]
            ),
        ],
        registry_overrides=scripted_registry,
    )

    progresses = [f["progress"] for f in parsed if "progress" in f]
    assert "Querying your data..." in progresses
    assert "Analyzing your data..." in progresses

    # dataset_id pinned to the route's value regardless of what the model sent.
    assert env.registry_captures[0] == (
        "validate_dsl_tool",
        {"dataset_id": "dst_1", "select": ["a"]},
    )
    # Final record block yielded after validation.
    assert any(f.get("type") == "record" for f in parsed)


def test_unknown_tool_returns_error_to_model(env):
    bogus = tool_call(cid="b1", name="does_not_exist", arguments="{}")
    # After tool use, a text-only reply is bounced once; the follow-up
    # generate_final_response text answer (queried=False) completes the turn.
    retry_gfr = tool_call(
        cid="b2",
        name="generate_final_response",
        arguments=json.dumps({"content": [text_block("plain answer")]}),
    )

    parsed, llm = env.run(
        [llm_msg(tool_calls=[bogus]), llm_msg(tool_calls=[retry_gfr])]
    )

    tool_results = [
        m for m in llm.calls[1] if m.get("role") == "tool"
    ]
    assert any("Unknown tool: does_not_exist" in m["content"] for m in tool_results)
    assert any(f.get("content") == "plain answer" for f in parsed)


def test_generate_final_response_must_be_alone(env):
    combo = [
        tool_call(cid="g1", name="generate_final_response",
                  arguments=json.dumps({"content": [text_block("x")]})),
        tool_call(cid="v1", name="validate_dsl_tool", arguments="{}"),
    ]

    parsed, llm = env.run([llm_msg(tool_calls=combo), llm_msg(content="recovered")])

    tool_results = {m["tool_call_id"]: m["content"] for m in llm.calls[1]
                    if m.get("role") == "tool"}
    assert "must be called alone" in tool_results["g1"]


def test_record_without_prior_validation_bounced_then_answered(env):
    gfr_record = tool_call(
        cid="g1",
        name="generate_final_response",
        arguments=json.dumps({"content": [record_block()]}),
    )
    gfr_text = tool_call(
        cid="g2",
        name="generate_final_response",
        arguments=json.dumps({"content": [text_block("safe text answer")]}),
    )

    parsed, _ = env.run(
        [llm_msg(tool_calls=[gfr_record]), llm_msg(tool_calls=[gfr_text])]
    )

    progresses = [f["progress"] for f in parsed if "progress" in f]
    assert "Refining your query..." in progresses
    # The unvalidated record was NOT streamed; the retried text answer was.
    assert not any(f.get("type") == "record" for f in parsed)
    assert any(f.get("content") == "safe text answer" for f in parsed)


def test_text_only_after_validation_bounced_until_record(env):
    validate_tc = tool_call(arguments='{"dataset_id": "dst_1"}')
    gfr_text = tool_call(
        cid="g1", name="generate_final_response",
        arguments=json.dumps({"content": [text_block("text only")]}),
    )
    gfr_record = tool_call(
        cid="g2", name="generate_final_response",
        arguments=json.dumps({"content": [record_block(chart_type="tablechart")]}),
    )

    parsed, _ = env.run(
        [
            llm_msg(tool_calls=[validate_tc]),
            llm_msg(tool_calls=[gfr_text]),
            llm_msg(tool_calls=[gfr_record]),
        ]
    )

    progresses = [f["progress"] for f in parsed if "progress" in f]
    assert progresses.count("Refining your query...") >= 1
    records = [f for f in parsed if f.get("type") == "record"]
    assert len(records) == 1 and records[0]["chart_type"] == "tablechart"


def test_gfr_invalid_json_arguments_retry(env):
    broken_gfr = tool_call(
        cid="g1", name="generate_final_response", arguments="{not json"
    )

    parsed, llm = env.run(
        [llm_msg(tool_calls=[broken_gfr]), llm_msg(content="clean answer")]
    )

    retry_pair = llm.calls[1][-2:]
    assert retry_pair[0]["role"] == "assistant"
    assert retry_pair[1]["role"] == "tool"
    assert "failed validation" in retry_pair[1]["content"]
    assert any(f.get("content") == "clean answer" for f in parsed)


def test_empty_text_block_skipped_inside_gfr(env):
    gfr_args = json.dumps(
        {"content": [text_block("   "), text_block("Real answer")]}
    )
    tc = tool_call(name="generate_final_response", arguments=gfr_args)
    parsed, _ = env.run([llm_msg(tool_calls=[tc]), llm_msg(content="x")])

    contents = [f["content"] for f in parsed if f.get("role") == "assistant"]
    assert contents == ["Real answer"]


# ===========================================================================
# event_stream — budget exhaustion / error handling
# ===========================================================================


def _freeze_clock_past_budget(monkeypatch):
    """First monotonic() seeds the start; the next exceeds any budget."""
    ticks = iter([0.0, 999.0])
    monkeypatch.setattr(les.time, "monotonic", lambda: next(ticks))


def test_budget_exhaustion_emits_terminal_error_and_done(env, monkeypatch):
    monkeypatch.setattr(les.settings, "MAX_TOOL_ITERATIONS", 2)
    _freeze_clock_past_budget(monkeypatch)

    parsed, llm = env.run([llm_msg(content="never reached")])
    assert llm.calls == []  # loop broke before ever calling the LLM

    terminal = parsed[-1]
    assert terminal == {"done": True}
    error_frames = [f for f in parsed if f.get("is_error")]
    assert len(error_frames) == 1
    assert "took too many processing steps" in error_frames[0]["content"]
    # The failing question itself was persisted before the loop.
    assert env.save.calls[0]["role"] == "user"
    assert env.invalidated == [7, 7]


def test_terminal_save_failure_falls_back_to_error_frame(env, monkeypatch):
    monkeypatch.setattr(les.settings, "MAX_TOOL_ITERATIONS", 2)
    _freeze_clock_past_budget(monkeypatch)
    env.save.fail_on_call = 2  # user save ok; terminal save fails

    parsed, _ = env.run([llm_msg(content="unused")])

    assert parsed[-1] == {"done": True}
    error_frame = parsed[-2]
    assert "error" in error_frame
    assert "took too many processing steps" in error_frame["error"]
    assert not any(f.get("is_error") for f in parsed)  # no message frame emitted
    assert env.invalidated == [7, 7]


def test_unexpected_exception_yields_friendly_error_and_done(env):
    parsed, _ = env.run([RuntimeError("kaboom")])

    assert parsed[-1] == {"done": True}
    error_messages = [
        f for f in parsed[:-1] if f.get("is_error") or "error" in f
    ]
    assert error_messages
    saved = env.save.calls[-1]
    assert saved["is_error"] is True
    assert env.invalidated == [7, 7]


def test_history_sanitized_into_llm_messages(env):
    captured = {}

    class Capture(ScriptedLLM):
        def __call__(self, client, messages, *, tools, max_tokens):
            captured["messages"] = [dict(m) for m in messages]
            return llm_msg(content="ok")

    monkeypatched = Capture([])
    import app.routers.llm_event_stream as _les

    original = _les._streamed_llm_message
    import contextlib

    @contextlib.contextmanager
    def swap():
        _les._streamed_llm_message = monkeypatched
        try:
            yield
        finally:
            _les._streamed_llm_message = original

    body = make_body(history=[{"role": "user", "content": "earlier"}])
    with swap():
        list(
            _les.event_stream(
                session=object(),
                dataset_id="dst_1",
                body=body,
                user_id=7,
                system_prompt="SYS",
            )
        )

    roles = [m["role"] for m in captured["messages"]]
    assert roles[0] == "system"
    assert {"role": "user", "content": "earlier"} in captured["messages"][1:-1]
