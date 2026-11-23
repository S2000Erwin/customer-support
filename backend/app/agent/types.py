from dataclasses import dataclass, field
from typing import Any, Literal

OutcomeKind = Literal["tool_use", "end_turn", "end_chat"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelOutcome:
    kind: OutcomeKind
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    provider_content: Any | None = None


@dataclass
class AgentEvent:
    type: str
    data: dict[str, Any]


@dataclass
class AgentContext:
    conversation_id: str
    customer_id: str | None = None
    authenticated_customer_email: str | None = None
    session_token: str | None = None
    user_message: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)


class AgentModel:
    async def respond(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelOutcome:
        raise NotImplementedError
