import json
import logging
import asyncio
import re
from datetime import UTC, datetime, timedelta
from secrets import compare_digest
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.agent.loop import SupportAgent
from app.auth import SESSION_TTL_SECONDS, create_session_token, read_session_token
from app.agent.support_tools import SupportData
from app.agent.types import AgentContext
from app.config import get_settings
from app.database import store
from app.escalations import escalation_queue
from app.providers.gemini import DemoAgentModel, GeminiAgentModel, SYSTEM_PROMPT
from app.rag import manual_rag
from app.schemas import ChatRequest, MockLoginRequest, VoiceSessionRequest, VoiceToolRequest, VoiceTurnRequest

settings = get_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger().setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("support_agent.api")
support_data = SupportData()
app = FastAPI(title="Customer Support Agent", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=[origin.strip() for origin in settings.frontend_origins.split(",")], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


_started = False
_startup_lock = asyncio.Lock()


async def ensure_started() -> None:
    """Connect services once per process.

    Runs from the ASGI startup event locally, and from the first request on
    serverless platforms (Vercel) where startup events are not guaranteed.
    """
    global _started
    if _started:
        return
    async with _startup_lock:
        if _started:
            return
        await _initialize_services()
        _started = True


@app.middleware("http")
async def ensure_started_middleware(request, call_next):
    await ensure_started()
    return await call_next(request)


@app.on_event("startup")
async def startup() -> None:
    await ensure_started()


async def _initialize_services() -> None:
    logger.info("startup provider=%s agent_model=%s rag_model=%s max_steps=%s", "gemini" if settings.gemini_api_key else "demo", settings.gemini_agent_model, settings.gemini_rag_model, settings.max_agent_steps)
    await store.initialize()
    await escalation_queue.initialize()
    await manual_rag.initialize()
    logger.info("startup_ready database=%s rag=%s escalation_queue=%s", "postgres" if store.available else "memory-demo", "qdrant" if manual_rag.ready else "keyword-demo", "redis" if escalation_queue.ready else "unavailable")


@app.on_event("shutdown")
async def shutdown() -> None:
    await escalation_queue.close()


def create_model():
    return GeminiAgentModel(
        settings.gemini_api_key,
        settings.gemini_agent_model,
        settings.company_name,
        settings.gemini_agent_input_usd_per_million,
        settings.gemini_agent_output_usd_per_million,
    ) if settings.gemini_api_key else DemoAgentModel()


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "database": "postgres" if store.available else "memory-demo", "escalation_queue": "redis" if escalation_queue.ready else "unavailable", "provider": "gemini" if settings.gemini_api_key else "demo", "agent_model": settings.gemini_agent_model if settings.gemini_api_key else "demo-agent", "rag": "qdrant" if manual_rag.ready else "keyword-demo", "gemini_key_configured": bool(settings.gemini_api_key), "company_name": settings.company_name}


@app.post("/api/auth/mock-login")
async def mock_login(request: MockLoginRequest) -> dict:
    if not settings.session_secret:
        raise HTTPException(status_code=503, detail="Mock sign-in is disabled: SESSION_SECRET is not configured.")
    if store.available:
        email = await store.authenticate_customer(request.email, request.password.get_secret_value())
    else:
        email = support_data.authenticate_mock_customer(request.email, request.password.get_secret_value())
    if email is None:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    customer = await store.customer_by_email(email) if store.available else support_data.customers.get(email)
    customer_name = (customer or support_data.customers.get(email) or {}).get("name", "Customer")
    return {
        "access_token": create_session_token(email, settings.session_secret),
        "token_type": "bearer",
        "expires_in": SESSION_TTL_SECONDS,
        "email": email,
        "name": customer_name,
    }


async def authenticated_email(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token.strip():
        raise HTTPException(status_code=401, detail="A valid bearer session is required.")
    token = token.strip()
    email = read_session_token(token, settings.session_secret)
    if email is None:
        raise HTTPException(status_code=401, detail="Your sign-in session is invalid or expired. Please sign in again.")
    if await escalation_queue.is_session_token_revoked(token):
        raise HTTPException(status_code=401, detail="Your sign-in session has ended. Please sign in again.")
    return email


@app.post("/api/voice/session")
async def create_voice_session(request: VoiceSessionRequest, authorization: str | None = Header(default=None)) -> dict:
    """Issue a short-lived, single-use Gemini Live token; the API key stays server-side."""
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="Voice is unavailable until GEMINI_API_KEY is configured.")
    email = await authenticated_email(authorization)
    # The session survives sign-in, so it declares account tools up front;
    # /api/voice/tool enforces authentication per call.
    declarations = support_data.voice_declarations()
    system_instruction = SYSTEM_PROMPT.format(company_name=settings.company_name) + " Speak in concise, natural sentences suitable for audio. Always follow policy and required tools. When a tool returns login_required, tell the customer: 'Please sign in to continue with account-specific support.' Then wait for them to sign in. The customer may sign in during this conversation; after that, account tools work without asking them to sign in again."
    # The same locked settings are sent to Gemini when the browser opens its socket.
    client_config = {
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
            "temperature": 0.2,
        },
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "disabled": False,
                "prefixPaddingMs": 100,
                "silenceDurationMs": 700,
            },
            "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
        },
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "tools": [{"functionDeclarations": declarations}],
    }
    if request.has_history:
        client_config["historyConfig"] = {"initialHistoryInClientContent": True}
    now = datetime.now(UTC)
    token_config = {
        "uses": 1,
        "expire_time": now + timedelta(minutes=30),
        "new_session_expire_time": now + timedelta(minutes=1),
        "live_connect_constraints": {
            "model": settings.gemini_live_model,
            "config": {
                "response_modalities": ["AUDIO"],
                "input_audio_transcription": {},
                "output_audio_transcription": {},
                "realtime_input_config": {
                    "automatic_activity_detection": {
                        "disabled": False,
                        "prefix_padding_ms": 100,
                        "silence_duration_ms": 700,
                    },
                    "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
                },
                "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": "Kore"}}},
                "system_instruction": {"parts": [{"text": system_instruction}]},
                "tools": [{"function_declarations": declarations}],
                "temperature": 0.2,
            },
        },
    }
    if request.has_history:
        token_config["live_connect_constraints"]["config"]["history_config"] = {"initial_history_in_client_content": True}

    def issue_token() -> str:
        from google import genai

        client = genai.Client(api_key=settings.gemini_api_key, http_options={"api_version": "v1beta"})
        try:
            return client.auth_tokens.create(config=token_config).name
        finally:
            client.close()

    try:
        token_name = await asyncio.to_thread(issue_token)
    except Exception as exc:
        logger.exception("gemini_live_token_failed model=%s", settings.gemini_live_model)
        raise HTTPException(status_code=503, detail="Could not start a Gemini Live session. Check the Live model and API access.") from exc
    logger.info("gemini_live_token_issued model=%s authenticated=%s tools=%s", settings.gemini_live_model, bool(email), len(declarations))
    return {"token": token_name, "model": settings.gemini_live_model, "config": client_config}


@app.post("/api/voice/tool")
async def run_voice_tool(request: VoiceToolRequest, authorization: str | None = Header(default=None)) -> dict:
    email = await authenticated_email(authorization)
    registry = support_data.registry(email)
    if request.tool_name not in {item["name"] for item in registry.declarations}:
        if not email and request.tool_name in support_data.account_tool_names():
            return {"name": request.tool_name, "result": {"ok": True, "login_required": True, "message": "Sign in to continue with account-specific support.", "duration_ms": 0}}
        if email and request.tool_name == "request_login":
            return {"name": request.tool_name, "result": {"ok": True, "already_signed_in": True, "message": "The customer is already signed in. Continue with their request using the account tools.", "duration_ms": 0}}
        raise HTTPException(status_code=403, detail="This tool is not available in the current session.")
    history = await store.get_history(request.conversation_id)
    context = AgentContext(
        conversation_id=request.conversation_id,
        user_message=request.current_user_message,
        history=history,
        authenticated_customer_email=email,
        session_token=authorization.partition(" ")[2].strip() if email and authorization else None,
    )
    started = perf_counter()
    result = await registry.execute(request.tool_name, request.arguments, context)
    result["duration_ms"] = round((perf_counter() - started) * 1000)
    logger.info("voice_tool_completed conversation_id=%s tool=%s ok=%s duration_ms=%s", request.conversation_id, request.tool_name, result.get("ok"), result["duration_ms"])
    return {"name": request.tool_name, "result": result}


@app.post("/api/voice/turn")
async def save_voice_turn(request: VoiceTurnRequest) -> dict:
    """Persist Live API transcripts so later text turns share the same memory."""
    await store.add_message(request.conversation_id, "user", request.user_text)
    prompt_id = await store.add_trace(
        request.conversation_id, "user_prompt", "Voice prompt", request.user_text, 0,
        {"content": request.user_text, "input_mode": "voice"},
    )
    for step in request.steps:
        kind = str(step.get("type", "voice_step"))[:40]
        detail = step.get("detail", {})
        if not isinstance(detail, dict):
            detail = {"detail": str(detail)}
        await store.add_trace(
            request.conversation_id, kind, str(step.get("label", kind))[:240],
            json.dumps(detail, ensure_ascii=False)[:2000], int(step.get("duration_ms", 0)), detail, parent_id=prompt_id,
        )
    if request.assistant_text.strip():
        await store.add_message(request.conversation_id, "assistant", request.assistant_text)
        await store.add_trace(
            request.conversation_id, "assistant_response", "Assistant voice response", request.assistant_text, 0,
            {"content": request.assistant_text, "output_mode": "voice"}, parent_id=prompt_id,
        )
    return {"ok": True, "prompt_trace_id": prompt_id}


@app.post("/api/chat")
async def chat(request: ChatRequest, authorization: str | None = Header(default=None)) -> StreamingResponse:
    signed_in_email = await authenticated_email(authorization)
    session_token = authorization.partition(" ")[2].strip() if signed_in_email and authorization else None
    conversation_id = request.conversation_id or f"conv_{uuid4().hex[:12]}"
    history = await store.get_history(conversation_id)
    resumed_login = bool(
        request.resume_login
        and history
        and history[-1].get("role") == "user"
        and history[-1].get("content") == request.message
    )
    resumed_voice_login = bool(
        request.resume_login
        and len(history) >= 2
        and history[-2].get("role") == "user"
        and history[-2].get("content") == request.message
        and history[-1].get("role") == "assistant"
        and re.search(r"\b(sign in|log in)\b", history[-1].get("content", ""), re.IGNORECASE)
    )
    if resumed_login:
        # The unauthenticated request already persisted this prompt before asking for sign-in.
        # Replay it once with the authenticated tool registry without duplicating it in history.
        history = history[:-1]
        logger.info("chat_login_resumed conversation_id=%s", conversation_id)
    elif resumed_voice_login:
        # Live voice persisted the prompt and spoken sign-in notice as a complete turn.
        # Replay the original prompt with the authenticated tool registry without duplicating either message.
        history = history[:-2]
        logger.info("chat_voice_login_resumed conversation_id=%s", conversation_id)
    logger.info("chat_started conversation_id=%s provider=%s message_chars=%s history_messages=%s", conversation_id, "gemini" if settings.gemini_api_key else "demo", len(request.message), len(history))
    if not (resumed_login or resumed_voice_login):
        await store.add_message(conversation_id, "user", request.message)
        prompt_trace_id = await store.add_trace(conversation_id, "user_prompt", "Customer prompt", request.message, 0, {"content": request.message})
    else:
        prompt_trace_id = await store.latest_prompt_trace_id(conversation_id, request.message)

    async def event_stream():
        def encode(kind: str, data: dict) -> str:
            return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield encode("conversation", {"conversation_id": conversation_id})
        agent = SupportAgent(create_model(), support_data.registry(signed_in_email), settings.max_agent_steps)
        context = AgentContext(conversation_id=conversation_id, user_message=request.message, history=history, authenticated_customer_email=signed_in_email, session_token=session_token)
        answer_parts = []
        try:
            async for event in agent.run(history, request.message, context):
                duration = int(event.data.get("duration_ms", 0))
                logger.debug("agent_stream_event conversation_id=%s type=%s fields=%s", conversation_id, event.type, sorted(event.data.keys()))
                if event.type == "message":
                    answer_parts.append(event.data.get("text", ""))
                label = event.data.get("name") or event.data.get("text") or event.type.replace("_", " ").title()
                await store.add_trace(conversation_id, event.type, str(label)[:240], json.dumps(event.data, ensure_ascii=False)[:2000], duration, event.data, parent_id=prompt_trace_id)
                if event.type == "model_call":
                    logger.info("model_step conversation_id=%s step=%s duration_ms=%s input_tokens=%s output_tokens=%s", conversation_id, event.data.get("step"), duration, event.data.get("input_tokens", 0), event.data.get("output_tokens", 0))
                elif event.type == "tool_call":
                    logger.info("tool_requested conversation_id=%s tool=%s argument_keys=%s", conversation_id, event.data.get("name"), sorted((event.data.get("arguments") or {}).keys()))
                elif event.type == "login_required":
                    logger.info("login_required conversation_id=%s", conversation_id)
                elif event.type == "escalation_required":
                    logger.warning("human_escalation_required conversation_id=%s reason=%s ticket_created=%s reference=%s", conversation_id, event.data.get("reason"), event.data.get("ticket_created"), event.data.get("reference"))
                elif event.type == "tool_result":
                    result = event.data.get("result") or {}
                    logger.info("tool_completed conversation_id=%s tool=%s ok=%s duration_ms=%s", conversation_id, event.data.get("name"), result.get("ok"), duration)
                elif event.type in ("message", "done"):
                    logger.info("agent_event conversation_id=%s type=%s keys=%s", conversation_id, event.type, sorted(event.data.keys()))
                yield encode(event.type, event.data)
        except Exception:
            logger.exception("chat_failed conversation_id=%s", conversation_id)
            await store.add_trace(conversation_id, "error", "Agent request failed", "Provider or tool execution failed.", 0, {}, parent_id=prompt_trace_id)
            yield encode("error", {"message": "The support assistant is temporarily unavailable. Please try again."})
            return
        if answer_parts:
            answer = "\n".join(answer_parts)
            await store.add_message(conversation_id, "assistant", answer)
            await store.add_trace(conversation_id, "assistant_response", "Assistant response", answer, 0, {"content": answer}, parent_id=prompt_trace_id)
            logger.info("chat_completed conversation_id=%s answer_chars=%s", conversation_id, len(answer))

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/admin/reset-db")
@app.post("/api/admin/reset-customer-db", include_in_schema=False)
async def reset_db(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> dict:
    require_admin_key(x_admin_key)
    if not store.available:
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable; the database was not reset.")
    if not escalation_queue.ready:
        raise HTTPException(status_code=503, detail="Redis is unavailable; the database and escalation queue were not reset.")
    result = await store.reset_customer_db()
    try:
        redis_tickets_cleared = await escalation_queue.clear_tickets()
        redis_tickets_cleared += await escalation_queue.clear_tickets(escalation_queue.support_stream_name)
    except Exception as exc:
        logger.exception("reset_db_partial_failure database_reset=true redis_reset=false")
        raise HTTPException(status_code=503, detail="Customer, case, and order data were reset, but Redis tickets could not be cleared. Retry the reset after Redis is available.") from exc
    logger.warning("support_db_reset customers=%s support_cases=%s orders=%s redis_tickets_cleared=%s", result["customers"], result["support_cases"], result["orders"], redis_tickets_cleared)
    return {"ok": True, **result, "redis_tickets_cleared": redis_tickets_cleared}


def require_admin_key(x_admin_key: str | None) -> None:
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="Admin access is disabled: ADMIN_API_KEY is not configured.")
    if not x_admin_key or not compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="A valid admin credential is required.")


@app.get("/api/admin/escalations")
async def list_escalations(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    require_admin_key(x_admin_key)
    try:
        return await escalation_queue.list_tickets(limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/admin/escalations")
async def clear_escalations(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> dict:
    require_admin_key(x_admin_key)
    try:
        deleted = await escalation_queue.clear_tickets()
        return {"ok": True, "deleted": deleted}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/admin/support-tickets")
async def list_support_tickets(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    require_admin_key(x_admin_key)
    try:
        return await escalation_queue.list_tickets(limit, escalation_queue.support_stream_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/admin/support-tickets")
async def clear_support_tickets(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> dict:
    require_admin_key(x_admin_key)
    try:
        deleted = await escalation_queue.clear_tickets(escalation_queue.support_stream_name)
        return {"ok": True, "deleted": deleted}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/admin/overview")
async def admin_overview() -> dict:
    return await store.dashboard()


@app.get("/api/admin/conversations/{conversation_id}")
async def admin_conversation(conversation_id: str) -> dict:
    data = await store.dashboard()
    conversation = next((item for item in data["conversations"] if item["id"] == conversation_id), None)
    return conversation or {"id": conversation_id, "events": [], "messages": []}
