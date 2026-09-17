"""Shared LLM helpers for the chat router: prompt assembly, message
serialization, history sanitization, persistence, and streamed completions.

These functions are used by both ``chat.py`` (WebSocket handler) and
``llm_event_stream.py`` (the event-loop core). They are intentionally
kept in a separate module so the chat router stays focused on
connection management.
"""

import json
import logging
from types import SimpleNamespace

from sqlalchemy.exc import OperationalError
from sqlmodel import Session

from app.core.settings import settings
from app.models.dataset import Dataset
from app.models.message import Message, MessageChartTypeEnum
from app.routers.llm_prompt import SYSTEM_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)


def _yield_json(data) -> str:
    """Serialize an event as a newline-terminated NDJSON line.

    Args:
        data: JSON-serializable dictionary.

    Returns:
        String with a trailing newline suitable for streaming.
    """
    return json.dumps(data, default=str) + "\n"


def _tool_call_dict(tc) -> dict:
    """Serialize an OpenAI SDK tool call for the conversation history.

    Gemini thinking models require the model's ``thought_signature``
    to be echoed back with the function call, or the next request is
    rejected. The ``extra_content`` attribute is forwarded when present.

    Args:
        tc: OpenAI SDK ``ToolCall`` object.

    Returns:
        Dictionary matching the ``tool_calls`` schema expected by the
        LLM API.
    """
    call = {
        "id": tc.id,
        "type": "function",
        "function": {
            "name": tc.function.name,
            "arguments": tc.function.arguments,
        },
    }
    extra = getattr(tc, "extra_content", None)
    if extra:
        call["extra_content"] = extra
    return call


def _sanitize_history(history: list[dict], limit: int = 10) -> list[dict]:
    """Rewrite assistant history messages so the model cannot replay past answers.

    Full prior answers (big tables of numbers, chart DSLs) tempt the
    model to copy them instead of re-querying the dataset fresh.
    Text is truncated and record content is replaced with a placeholder
    that is explicitly not authoritative.  User messages pass through
    unchanged.

    Args:
        history: List of previous conversation turns.
        limit: Maximum number of most-recent turns to retain.

    Returns:
        Sanitized list of messages safe for the LLM context window.
    """
    clean: list[dict] = []
    for m in history[-limit:]:
        role = m.get("role")
        content = m.get("content")
        if role == "user":
            clean.append({"role": "user", "content": content})
            continue
        if role == "assistant":
            if isinstance(content, dict) or (
                isinstance(content, str)
                and content.lstrip().startswith(("{", "["))
                and m.get("type") == "record"
            ):
                clean.append(
                    {
                        "role": "assistant",
                        "content": "[chart from a previous turn — not authoritative]",
                    }
                )
            else:
                text = content if isinstance(content, str) else json.dumps(content, default=str)
                if len(text) > 300:
                    text = text[:300] + "…[truncated]"
                clean.append({"role": "assistant", "content": text})
    return clean


def _tools_with_dataset_id() -> list[str]:
    """Tool names whose functions take a ``dataset_id`` input."""
    return ["get_profile", "validate_dsl_tool"]


def _save_message(
    session: Session,
    *,
    dataset_id: str,
    role: str,
    content: str | dict,
    type: str = "text",
    chart_type: str = MessageChartTypeEnum.none,
    is_error: bool = False,
    message_id: str = None
) -> Message:
    """Persist a chat message to the database.

    When a ``message_id`` is provided the saved record uses a
    compound key to avoid collisions with concurrent saves.  If the
    database connection has gone stale (common after long LLM waits),
    the operation is retried once after a rollback.

    Args:
        session: Active SQLAlchemy session.
        dataset_id: Dataset the message belongs to.
        role: ``user`` or ``assistant``.
        content: Text or structured JSONB payload.
        type: Content type hint (``text``, ``dsl``, etc.).
        chart_type: Visualization hint for the frontend.
        is_error: ``True`` if this is an error response.
        message_id: Optional client-provided ID for idempotency.

    Returns:
        The saved ``Message`` model instance.
    """
    import uuid
    message = None
    if message_id:
       message = Message(
        id=f"{uuid.uuid4().hex}_{message_id}",
        dataset_id=dataset_id,
        role=role,
        content=content,
        type=type,
        chart_type=chart_type,
        is_error=is_error,
    )
    else:
        message = Message(
            dataset_id=dataset_id,
            role=role,
            content=content,
            type=type,
            chart_type=chart_type,
            is_error=is_error,
        )
    try:
        session.add(message)
        session.commit()
        session.refresh(message)
    except OperationalError:
        logger.warning(
            "DB connection stale after long LLM wait; rolling back and retrying save"
        )
        session.rollback()
        session.add(message)
        session.commit()
        session.refresh(message)
    return message


def _friendly_llm_error(exc: Exception) -> str:
    """Map LLM API errors to user-friendly messages.

    Args:
        exc: Exception raised by the LLM client.

    Returns:
        Human-readable error string suitable for display in the UI.
    """
    message = str(exc)
    lowered = message.lower()
    if "invalid api key" in lowered or "authentication" in lowered or "unauthorized" in lowered or "api key" in lowered and "invalid" in lowered:
        return (
            "The AI assistant is unavailable: the API key is invalid or missing. "
            "Set LLM_API_KEY in the backend .env and restart the backend."
        )
    if "rate limit" in lowered or "429" in lowered or "quota" in lowered or "resource exhausted" in lowered:
        return (
            "The AI assistant is busy (rate limited). Wait a moment and try again."
        )
    if "overload" in lowered or "529" in lowered or "busy" in lowered or "try again later" in lowered:
        return (
            "The AI assistant is busy right now (the model provider is "
            "overloaded). Wait a moment and try again."
        )
    if "insufficient balance" in lowered or "payment required" in lowered or "402" in lowered:
        return (
            "The AI assistant is unavailable: the DeepSeek account balance is "
            "exhausted. Top up at https://platform.deepseek.com/top_up and retry."
        )
    if "model" in lowered and ("not found" in lowered or "not supported" in lowered or "404" in lowered):
        return (
            "The AI assistant is unavailable: the model is not found. Check "
            "LLM_MODEL in the backend .env."
        )
    if "connect" in lowered or "connection" in lowered or "refused" in lowered:
        return (
            "The AI assistant is unreachable. Check your internet connection and "
            "that LLM_BASE_URL is correct."
        )
    if "timed out" in lowered or "timeout" in lowered or "timedout" in lowered:
        return (
            "The AI assistant took too long to respond. Try again in a moment."
        )
    return (
        "The AI assistant hit an unexpected error. Please try again in a moment."
    )


def _build_system_prompt(dataset: Dataset) -> str:
    """Render the system prompt template with the dataset's schema.

    The schema is formatted as a human-readable list of
    ``field_name: type`` entries when the schema is a dictionary, or
    as raw JSON when it is not.

    Args:
        dataset: The target dataset with its ``dataset_schema``.

    Returns:
        Rendered system prompt string.
    """
    schema = dataset.dataset_schema or {}
    if isinstance(schema, dict):
        schema_text = "\n".join(
            f"- {field}: {rule.get('type', 'unknown')}"
            for field, rule in schema.items()
        )
    else:
        schema_text = json.dumps(schema, default=str)
    return SYSTEM_PROMPT_TEMPLATE.format(
        DATASET_ID=dataset.id, SCHEMA=schema_text
    )


def _streamed_llm_message(client, messages, *, tools: list[dict], max_tokens: int) -> SimpleNamespace:
    """Run a streamed chat completion, accumulating the full assistant message.

    Both text content and tool calls are accumulated incrementally as
    chunks arrive, then returned as a single ``SimpleNamespace``
    matching the OpenAI SDK ``ChatCompletion`` shape.

    Args:
        client: OpenAI-compatible client instance.
        messages: Full conversation message list.
        tools: Tool definitions to pass to the model.
        max_tokens: Maximum tokens in the completion.

    Returns:
        ``SimpleNamespace`` with ``content`` and ``tool_calls``
        attributes.
    """
    stream = client.chat.completions.create(
        model=settings.LLM_MODEL,
        max_tokens=max_tokens,
        messages=messages,
        tools=tools,
        stream=True,
        temperature=settings.LLM_TEMPERATURE,
        timeout=settings.LLM_TIMEOUT_SECONDS,
    )
    content_parts: list[str] = []
    tool_entries: dict[int, dict] = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            content_parts.append(delta.content)
        for tc in delta.tool_calls or []:
            entry = tool_entries.setdefault(
                tc.index,
                {
                    "_idx": tc.index,
                    "id": "",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if tc.id:
                entry["id"] = tc.id
            if tc.function:
                if tc.function.name:
                    entry["function"]["name"] = tc.function.name
                if tc.function.arguments:
                    entry["function"]["arguments"] += tc.function.arguments
    tool_calls = [
        SimpleNamespace(
            id=entry["id"],
            type="function",
            function=SimpleNamespace(
                name=entry["function"]["name"],
                arguments=entry["function"]["arguments"],
            ),
        )
        for entry in sorted(tool_entries.values(), key=lambda e: e["_idx"])
    ]
    return SimpleNamespace(
        content="".join(content_parts) or None,
        tool_calls=tool_calls or None,
    )
