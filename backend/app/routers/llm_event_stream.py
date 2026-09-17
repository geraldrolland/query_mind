"""LLM event stream generator for the chat router.

Streams newline-delimited JSON. Only final assistant messages are streamed; tool
calls and tool results are executed server-side and not exposed:
    {"id", "role", "type", "chart_type", "content", "created_at"}
        assistant message (text or record content)
    {"progress": "..."}
        status update emitted while the tool loop runs
    {"done": true}
    {"error": "..."}
"""

import json
import logging
import time

from pydantic import ValidationError
from sqlmodel import Session

from app.core.settings import settings
from app.mcp.tool_registry import TOOL_REGISTRY, get_tool_llm_specs
from app.routers.llm_utils import (
    _friendly_llm_error,
    _save_message,
    _sanitize_history,
    _streamed_llm_message,
    _tool_call_dict,
    _tools_with_dataset_id,
    _yield_json,
)
from app.schemas.dataset import ChatRequestSchema
from app.schemas.llm import TextContentBlock
from middleware import invalidate_user_cache

logger = logging.getLogger(__name__)

_VALID_CHART_TYPES = {"barchart", "linechart", "tablechart", "metricchart", "piechart"}


def _extract_embedded_blocks(text: str):
    """Recover content blocks when the model inlines its generate_final_response
    payload as plain text instead of calling the tool.

    Returns (clean_text, blocks) when a parsable payload is found, else None.
    """
    stripped = text.strip()
    starts = {0}
    nl = stripped.rfind("\n{")
    if nl != -1:
        starts.add(nl + 1)
    last = stripped.rfind("{")
    if last > 0:
        starts.add(last)
    for start in sorted(starts, reverse=True):
        try:
            obj = json.loads(stripped[start:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        content = obj.get("content")
        if isinstance(content, list) and content:
            return stripped[:start].rstrip(), content
        # Single-block leak: the model emitted one content block object
        # (e.g. {"type": "record", "dsl_field": ..., "chart_type": ...})
        # instead of the full generate_final_response payload.
        btype = obj.get("type")
        if btype == "text" and isinstance(obj.get("text"), str) and obj["text"].strip():
            return stripped[:start].rstrip(), [obj]
        if btype == "record" and obj.get("dsl_field"):
            return stripped[:start].rstrip(), [obj]
    return None


def event_stream(
    *,
    session: Session,
    dataset_id: str,
    body: ChatRequestSchema,
    user_id: int,
    system_prompt: str,
):
    """Stream an AI answer for the dataset, using DeepSeek + the DSL query tool."""
    try:
        _save_message(
            session,
            dataset_id=dataset_id,
            role="user",
            content=body.message.content,
            message_id=body.message.id,
        )
        invalidate_user_cache(user_id)

        from openai import OpenAI

        client = OpenAI(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
            max_retries=0,
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )

        tools: list[dict] = get_tool_llm_specs()

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(_sanitize_history(body.history))
        messages.append({"role": "user", "content": body.message.content})

        queried = False
        answered = False
        tool_used = False
        iteration_start = time.monotonic()
        iteration_budget = settings.LLM_TIMEOUT_SECONDS * settings.MAX_TOOL_ITERATIONS
        for i in range(settings.MAX_TOOL_ITERATIONS):
            elapsed = time.monotonic() - iteration_start
            if elapsed > iteration_budget:
                logger.warning(
                    "LLM tool loop exceeded budget (%.0fs), stopping", elapsed
                )
                break

            yield _yield_json({"progress": "Thinking..."})

            logger.info("CALLING LLM")
            msg = _streamed_llm_message(client, messages, tools=tools, max_tokens=settings.LLM_MAX_TOKENS)
            tool_calls = list(msg.tool_calls or [])

            final_tool = (
                tool_calls[0]
                if len(tool_calls) == 1
                and getattr(tool_calls[0].function, "name", None)
                == "generate_final_response"
                else None
            )

            if final_tool:
                try:
                    llm_response = TOOL_REGISTRY["generate_final_response"]["function"](
                        **json.loads(final_tool.function.arguments or "{}")
                    )
                except (json.JSONDecodeError, ValidationError) as exc:
                    logger.warning("Invalid generate_final_response; retrying: %s", exc)
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.content or "",
                            "tool_calls": [_tool_call_dict(final_tool)],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": final_tool.id,
                            "content": (
                                "Your generate_final_response failed validation. "
                                "Return a valid structured response."
                            ),
                        }
                    )
                    continue

                has_record = any(
                        getattr(b, "chart_type", None) for b in llm_response.content
                    )
                if has_record and not queried:
                    logger.warning(
                        "generate_final_response record block without a "
                        "validate_dsl_tool this turn; bouncing"
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.content or "",
                            "tool_calls": [_tool_call_dict(final_tool)],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": final_tool.id,
                            "content": (
                                "A record block must reference a DSL you "
                                "validated with validate_dsl_tool in THIS turn. "
                                "Call validate_dsl_tool with your plan, then "
                                "re-issue generate_final_response with a record "
                                "block containing that exact DSL and a chart type."
                            ),
                        }
                    )

                    yield _yield_json({"progress": "Refining your query..."})
                    continue

                if queried and not has_record:
                    logger.warning(
                        "generate_final_response text-only after tool use; bouncing"
                    )
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.content or "",
                            "tool_calls": [_tool_call_dict(final_tool)],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": final_tool.id,
                            "content": (
                                "You used tools for this dataset question, so "
                                "your reply MUST include a record block "
                                "containing the exact validated DSL and a chart "
                                "type. Text-only replies are rejected. "
                                "Re-issue generate_final_response with a record "
                                "block."
                            ),
                        }
                    )
                    yield _yield_json({"progress": "Refining your query..."})
                    continue

                for block in llm_response.content:
                    if isinstance(block, TextContentBlock):
                        if not (block.text and block.text.strip()):
                            continue
                        message = _save_message(
                            session,
                            dataset_id=dataset_id,
                            role="assistant",
                            content=block.text,
                        )
                        yield _yield_json(message.model_dump())
                    else:
                        message = _save_message(
                            session,
                            dataset_id=dataset_id,
                            role="assistant",
                            content=block.dsl_field,
                            type="record",
                            chart_type=block.chart_type.value,
                        )
                        yield _yield_json(message.model_dump())
                answered = True
                break

            if tool_calls:
                tool_used = True
                if tool_calls:
                    messages.append(
                        {
                            "role": "assistant",
                            "content": msg.content or "",
                            "tool_calls": [_tool_call_dict(tc) for tc in tool_calls],
                        }
                    )

                    tool_results = []
                    for tc in tool_calls:
                        if getattr(tc.function, "name", None) == "generate_final_response":
                            content = json.dumps(
                                {
                                    "error": (
                                        "generate_final_response must be called alone, "
                                        "not together with other tools. Retry it in its "
                                        "own turn."
                                    )
                                }
                            )
                        else:
                            fn = TOOL_REGISTRY.get(tc.function.name, {}).get("function")
                            try:
                                args = json.loads(tc.function.arguments or "{}")
                            except json.JSONDecodeError:
                                args = {}
                            if fn is None:
                                content = json.dumps(
                                    {"error": f"Unknown tool: {tc.function.name}"}
                                )
                            else:
                                if tc.function.name == "validate_dsl_tool":
                                    yield _yield_json({"progress": "Querying your data..."})
                                if tc.function.name in _tools_with_dataset_id():
                                    incoming = args.get("dataset_id")
                                    if incoming != dataset_id:
                                        logger.warning(
                                            "Overriding dataset_id %r -> %r for %s",
                                            incoming,
                                            dataset_id,
                                            tc.function.name,
                                        )
                                        args["dataset_id"] = dataset_id
                                try:
                                    result = fn(**args)
                                    if (
                                        tc.function.name == "validate_dsl_tool"
                                        and isinstance(result, dict)
                                        and result.get("status") == "validated"
                                    ):
                                        queried = True
                                        yield _yield_json({"progress": "Analyzing your data..."})
                                    content = json.dumps(result, default=str)
                                except Exception as exc:
                                    if tc.function.name == "validate_dsl_tool":
                                        yield _yield_json({"progress": "Refining your query..."})
                                    logger.warning("Tool exec failed: %s", exc)
                                    content = json.dumps({"error": str(exc)})

                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": content,
                            }
                        )

                    messages.extend(tool_results)
                    continue

            if msg.content:
                recovered = _extract_embedded_blocks(msg.content)
                if (
                    recovered is None
                    and tool_used
                    and i < settings.MAX_TOOL_ITERATIONS - 1
                ):
                    logger.info(
                        "Bouncing text-only reply after tool use; forcing retry"
                    )
                    yield _yield_json({"progress": "Refining your query..."})
                    messages.append(
                        {"role": "assistant", "content": msg.content}
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Do not apologize or describe the problem. "
                                "Answer the original question: if your last "
                                "validate_dsl_tool failed, fix the DSL per the "
                                "validation error and call it again; then "
                                "produce the final answer via "
                                "generate_final_response."
                            ),
                        }
                    )
                    continue
                if recovered is not None:
                    clean_text, blocks = recovered
                    logger.info(
                        "Recovered %d content block(s) inlined as text", len(blocks)
                    )
                    saw_text = False
                    for block in blocks:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text_val = (block.get("text") or "").strip()
                            if not text_val:
                                continue
                            saw_text = True
                            message = _save_message(
                                session,
                                dataset_id=dataset_id,
                                role="assistant",
                                content=text_val,
                            )
                            yield _yield_json(message.model_dump())
                        elif btype == "record":
                            chart_type = block.get("chart_type")
                            dsl_field = block.get("dsl_field")
                            if chart_type not in _VALID_CHART_TYPES or not dsl_field:
                                continue
                            message = _save_message(
                                session,
                                dataset_id=dataset_id,
                                role="assistant",
                                content=dsl_field,
                                type="record",
                                chart_type=chart_type,
                            )
                            yield _yield_json(message.model_dump())
                    if not saw_text and clean_text:
                        message = _save_message(
                            session,
                            dataset_id=dataset_id,
                            role="assistant",
                            content=clean_text,
                        )
                        yield _yield_json(message.model_dump())
                    answered = True
                else:
                    message = _save_message(
                        session,
                        dataset_id=dataset_id,
                        role="assistant",
                        content=msg.content,
                    )
                    yield _yield_json(message.model_dump())
                    answered = True
            break

        if not answered:
            logger.warning(
                "Tool loop ended without a final answer; emitting terminal error"
            )
            friendly = (
                "This question took too many processing steps and could not be "
                "completed. Try narrowing it down or ask again."
            )
            try:
                message = _save_message(
                    session,
                    dataset_id=dataset_id,
                    role="assistant",
                    content=friendly,
                    is_error=True,
                )
                yield _yield_json(message.model_dump())
            except Exception as save_exc:
                logger.error("Failed to persist timeout message: %s", save_exc)
                yield _yield_json({"error": friendly})
    except Exception as exc:
        from openai import APIError

        if isinstance(exc, APIError):
            logger.warning("LLM API error in chat stream: %s", exc)
        else:
            logger.error("Chat stream failed: %s", exc, exc_info=True)

        friendly = _friendly_llm_error(exc)
        try:
            message = _save_message(
                session,
                dataset_id=dataset_id,
                role="assistant",
                content=friendly,
                is_error=True,
            )
            yield _yield_json(message.model_dump())
        except Exception as save_exc:
            logger.error("Failed to persist error message: %s", save_exc)
            yield _yield_json({"error": _friendly_llm_error(save_exc)})
            
    invalidate_user_cache(user_id)
    yield _yield_json({"done": True})