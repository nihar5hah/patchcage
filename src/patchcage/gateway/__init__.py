"""Model gateways: the engine's only path to a model."""

from patchcage.gateway.base import (
    GatewayError,
    InvalidModelOutput,
    ModelGateway,
    ModelHealth,
    ModelUnavailable,
)
from patchcage.gateway.openai_compat import OpenAICompatGateway
from patchcage.gateway.scripted import ScriptedGateway, ScriptExhaustedError

__all__ = [
    "GatewayError",
    "InvalidModelOutput",
    "ModelGateway",
    "ModelHealth",
    "ModelUnavailable",
    "OpenAICompatGateway",
    "ScriptExhaustedError",
    "ScriptedGateway",
]
