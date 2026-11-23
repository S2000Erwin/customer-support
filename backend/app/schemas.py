from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)
    resume_login: bool = False


class MockLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=240)
    password: SecretStr = Field(min_length=1, max_length=256)


class VoiceToolRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=40)
    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    current_user_message: str = Field(default="", max_length=4000)


class VoiceSessionRequest(BaseModel):
    has_history: bool = False


class VoiceTurnRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=40)
    user_text: str = Field(min_length=1, max_length=4000)
    assistant_text: str = Field(default="", max_length=8000)
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=50)


class ChatEvent(BaseModel):
    type: Literal["status", "message", "tool_call", "tool_result", "citation", "login_required", "escalation_required", "session_logout", "error", "done"]
    data: dict[str, Any] = Field(default_factory=dict)


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    duration_ms: int
    status: str


class TraceEvent(BaseModel):
    id: str
    parent_id: str | None
    type: str
    label: str
    detail: str | None = None
    started_at: str
    duration_ms: int
    metadata: dict[str, Any] = Field(default_factory=dict)
