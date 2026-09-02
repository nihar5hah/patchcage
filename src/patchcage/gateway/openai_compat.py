"""OpenAI-compatible Chat Completions gateway.

One implementation covers Ollama, llama.cpp, vLLM, and hosted
OpenAI-compatible endpoints. An API key is optional: local servers (Ollama,
llama.cpp) omit it; hosted endpoints read it from PATCHCAGE_MODEL_API_KEY.
The key is never stored, logged, or included in error messages.
"""

from __future__ import annotations

import json
import os

import httpx
from pydantic import ValidationError

from patchcage.domain import AGENT_ACTION_ADAPTER, AgentAction
from patchcage.gateway.base import InvalidModelOutput, ModelHealth, ModelUnavailable
from patchcage.harness.context import AgentContext

DEFAULT_API_KEY_ENV = "PATCHCAGE_MODEL_API_KEY"
MAX_ACTION_TOKENS = 1_200
MAX_RESPONSE_CHARS = 1_000_000
_ERROR_BODY_CAP = 4_096
_RESPONSE_FORMAT_HINTS = ("response_format", "json_object")


def _rejects_response_format(response: httpx.Response) -> bool:
    """True when a 400 looks like the server rejected json_object.

    llama.cpp / vLLM / some Ollama builds reject ``response_format``. Other
    400s (unknown model, context overflow) must fail closed without a retry
    that drops the JSON envelope. Inspect a capped body; never log it.
    """
    chunk = response.content[:_ERROR_BODY_CAP].decode("utf-8", errors="replace").lower()
    return any(hint in chunk for hint in _RESPONSE_FORMAT_HINTS)


_SYSTEM_PROMPT = """\
You are the investigation model inside PatchCage, a least-privilege
vulnerability-remediation harness. You propose exactly one action per turn;
the host executes it and shows you the result on the next turn.

Reply with a single JSON object and nothing else — no prose, no markdown
fences. Exactly one of:

{"type": "tool", "tool": "<name>", "arguments": {...}, "summary": "<why>"} — call a workspace tool
{"type": "patch", "diff": "<unified diff>", "summary": "<why>"} — propose the fix
{"type": "complete", "summary": "<why>", "evidence_ids": []} — ask the host to verify and finish

The user message is a JSON snapshot of the run: the finding, current phase,
available tool names, recent turns, check results, and remaining budgets.

Rules:
- Use only tools listed under "available_tools".
- Prefer small, reversible tool calls. Patch only when you know the fix.
- Every patch is a complete unified diff against the original files; a new
  patch replaces your previous one rather than stacking on it.
- You cannot run checks yourself; the host verifies every patch.
- A failed baseline scanner or security check means the finding was reproduced,
  not that the harness is broken.
"""

_RETRY_MESSAGE = (
    "Your previous reply was not a single valid action JSON object. "
    "Reply with exactly one JSON object matching the schema — no prose, no markdown fences."
)


def _render_context(context: AgentContext) -> str:
    return "Current run state:\n" + json.dumps(
        context.model_dump(mode="json"), separators=(",", ":")
    )


class OpenAICompatGateway:
    """Drives an OpenAI-compatible chat endpoint under a strict JSON envelope."""

    def __init__(
        self,
        base_url: str,
        model_id: str,
        *,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        timeout_seconds: float = 60.0,
        max_tokens: int = MAX_ACTION_TOKENS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must be non-empty")
        if not model_id.strip():
            raise ValueError("model_id must be non-empty")
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._api_key_env = api_key_env
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            return {}
        return {"Authorization": f"Bearer {api_key}"}

    async def health(self) -> ModelHealth:
        try:
            response = await self._client.get(
                f"{self._base_url}/models",
                headers=self._headers(),
            )
        except (httpx.HTTPError, httpx.InvalidURL):
            return ModelHealth(ok=False, detail=f"endpoint unreachable: {self._base_url}")
        if response.status_code >= 400:
            return ModelHealth(ok=False, detail=f"endpoint returned HTTP {response.status_code}")
        return ModelHealth(ok=True)

    async def next_action(self, context: AgentContext) -> AgentAction:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _render_context(context)},
        ]
        last_reason = "no reply"
        for attempt in range(2):
            content = await self._post(messages)
            try:
                return AGENT_ACTION_ADAPTER.validate_python(json.loads(content))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_reason = str(exc)[:500]
                if attempt == 0:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": _RETRY_MESSAGE})
        raise InvalidModelOutput(
            f"model output was not a valid action after one correction: {last_reason}"
        )

    async def _post(
        self, messages: list[dict[str, str]], *, json_object: bool = True
    ) -> str:
        payload: dict[str, object] = {
            "model": self._model_id,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ModelUnavailable(f"model endpoint timed out: {self._base_url}") from exc
        except httpx.HTTPStatusError as exc:
            if (
                json_object
                and exc.response.status_code == 400
                and _rejects_response_format(exc.response)
            ):
                return await self._post(messages, json_object=False)
            raise ModelUnavailable(
                f"model endpoint returned HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise ModelUnavailable(f"model endpoint unreachable: {self._base_url}") from exc
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidModelOutput("endpoint response had no message content") from exc
        if not isinstance(content, str) or not content.strip():
            raise InvalidModelOutput("endpoint returned empty message content")
        if len(content) > MAX_RESPONSE_CHARS:
            raise InvalidModelOutput("endpoint response exceeded the size cap")
        return content
