"""Deterministic gateway for tests and demos."""

from __future__ import annotations

from collections.abc import Sequence

from patchcage.domain import AgentAction
from patchcage.gateway.base import ModelHealth
from patchcage.harness.context import AgentContext


class ScriptExhaustedError(RuntimeError):
    """The scripted action list ran out — the test or demo script is too short."""


class ScriptedGateway:
    """Replays a fixed list of actions; records the contexts it was shown."""

    def __init__(self, actions: Sequence[AgentAction], *, healthy: bool = True) -> None:
        self._actions = list(actions)
        self._healthy = healthy
        self.seen_contexts: list[AgentContext] = []

    async def health(self) -> ModelHealth:
        if self._healthy:
            return ModelHealth(ok=True)
        return ModelHealth(ok=False, detail="scripted as unavailable")

    async def next_action(self, context: AgentContext) -> AgentAction:
        self.seen_contexts.append(context)
        if not self._actions:
            raise ScriptExhaustedError("scripted gateway ran out of actions")
        return self._actions.pop(0)
