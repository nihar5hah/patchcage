"""Model gateway boundary — the only place the engine talks to a model."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from patchcage.domain import AgentAction
from patchcage.domain.models import StrictModel
from patchcage.harness.context import AgentContext


class ModelHealth(StrictModel):
    """Preflight liveness result. Never raises; `ok=False` means do not run."""

    ok: bool
    detail: str = Field(default="", max_length=500)


class GatewayError(RuntimeError):
    """Base class for gateway failures. Messages must never contain secrets."""


class ModelUnavailable(GatewayError):
    """The endpoint cannot be reached, authenticated, or used right now."""


class InvalidModelOutput(GatewayError):
    """The model's reply could not be parsed into a valid AgentAction."""


class ModelGateway(Protocol):
    """The narrow seam between the harness and any model backend."""

    async def health(self) -> ModelHealth: ...

    async def next_action(self, context: AgentContext) -> AgentAction: ...
