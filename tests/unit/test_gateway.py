"""Gateway tests. No live endpoints: httpx.MockTransport only."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from patchcage.domain import (
    Finding,
    FindingSource,
    PatchAction,
    RunLimits,
    RunPhase,
    ToolAction,
)
from patchcage.gateway import (
    InvalidModelOutput,
    ModelUnavailable,
    OpenAICompatGateway,
    ScriptedGateway,
    ScriptExhaustedError,
)
from patchcage.harness.budgets import BudgetTracker
from patchcage.harness.context import AgentContext, build_context

BASE_URL = "http://model.test/v1/"  # trailing slash on purpose
KEY = "test-key-123"


def _context() -> AgentContext:
    finding = Finding(
        id="finding-1",
        source=FindingSource.MANUAL,
        title="SQL injection",
        description="User input reaches SQL.",
        severity="high",
        file_path="src/app.py",
        verification_recipe="sql_injection_oracle",
    )
    return build_context(
        finding=finding,
        phase=RunPhase.INVESTIGATING,
        budgets=BudgetTracker(limits=RunLimits()).snapshot(),
        available_tools=("read_file", "search_code"),
    )


def _tool_payload() -> str:
    return json.dumps(
        {
            "type": "tool",
            "tool": "read_file",
            "arguments": {"path": "src/app.py"},
            "summary": "Inspect the query.",
        }
    )


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


def _gateway(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAICompatGateway:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatGateway(BASE_URL, "test-model", client=client)


async def test_scripted_gateway_replays_actions_in_order() -> None:
    actions = [
        ToolAction(type="tool", tool="read_file", arguments={"path": "a.py"}, summary="Read."),
        PatchAction(type="patch", diff="diff --git a/a.py b/a.py\n", summary="Fix."),
    ]
    gateway = ScriptedGateway(actions)

    first = await gateway.next_action(_context())
    second = await gateway.next_action(_context())

    assert first == actions[0]
    assert second == actions[1]
    assert len(gateway.seen_contexts) == 2


async def test_scripted_gateway_raises_when_exhausted() -> None:
    gateway = ScriptedGateway([])
    with pytest.raises(ScriptExhaustedError):
        await gateway.next_action(_context())


async def test_scripted_gateway_health_flag() -> None:
    assert (await ScriptedGateway([]).health()).ok
    assert not (await ScriptedGateway([], healthy=False).health()).ok


async def test_openai_compat_parses_valid_action(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _completion(_tool_payload())

    action = await _gateway(handler).next_action(_context())

    assert isinstance(action, ToolAction)
    assert len(seen) == 1
    request = seen[0]
    assert request.url == httpx.URL("http://model.test/v1/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 1200
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"


async def test_openai_compat_retries_without_json_object_on_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "unknown field response_format"})
        return _completion(_tool_payload())

    action = await _gateway(handler).next_action(_context())

    assert isinstance(action, ToolAction)
    assert len(bodies) == 2
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in bodies[1]


async def test_openai_compat_retries_once_after_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)
    calls: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return _completion("```json\n definitely not the schema")
        return _completion(_tool_payload())

    action = await _gateway(handler).next_action(_context())

    assert isinstance(action, ToolAction)
    assert len(calls) == 2
    retry_messages = calls[1]["messages"]
    assert [m["role"] for m in retry_messages] == ["system", "user", "assistant", "user"]


async def test_openai_compat_raises_after_repeated_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return _completion("not json at all")

    with pytest.raises(InvalidModelOutput):
        await _gateway(handler).next_action(_context())


async def test_openai_compat_missing_api_key_still_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PATCHCAGE_MODEL_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _completion(_tool_payload())

    action = await _gateway(handler).next_action(_context())

    assert isinstance(action, ToolAction)
    assert "Authorization" not in seen[0].headers


async def test_openai_compat_timeout_maps_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ModelUnavailable, match="timed out"):
        await _gateway(handler).next_action(_context())


async def test_openai_compat_error_redacts_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"bad key {KEY}"})

    with pytest.raises(ModelUnavailable) as excinfo:
        await _gateway(handler).next_action(_context())
    assert KEY not in str(excinfo.value)
    assert "401" in str(excinfo.value)


async def test_openai_compat_response_without_content_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {}}]})

    with pytest.raises(InvalidModelOutput):
        await _gateway(handler).next_action(_context())


async def test_openai_compat_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHCAGE_MODEL_API_KEY", KEY)

    def up(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": []})

    assert (await _gateway(up).health()).ok

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    health = await _gateway(down).health()
    assert not health.ok
    assert "unreachable" in health.detail


async def test_openai_compat_health_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PATCHCAGE_MODEL_API_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    assert (await _gateway(handler).health()).ok
    assert seen[0].url.path == "/v1/models"
    assert "Authorization" not in seen[0].headers
