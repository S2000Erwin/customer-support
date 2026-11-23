from collections.abc import AsyncIterator
import logging
from time import perf_counter

from app.agent.tools import ToolRegistry
from app.agent.types import AgentContext, AgentEvent, AgentModel, ModelOutcome

logger = logging.getLogger("support_agent.loop")


class SupportAgent:
    def __init__(self, model: AgentModel, tools: ToolRegistry, max_steps: int = 6) -> None:
        self.model = model
        self.tools = tools
        self.max_steps = max_steps

    async def run(self, history: list[dict], user_text: str, context: AgentContext) -> AsyncIterator[AgentEvent]:
        messages = [*history, {"role": "user", "content": user_text}]
        logger.info("agent_started conversation_id=%s history_messages=%s max_steps=%s", context.conversation_id, len(history), self.max_steps)
        yield AgentEvent("status", {"text": "Thinking"})
        for step in range(self.max_steps):
            model_started = perf_counter()
            outcome: ModelOutcome = await self.model.respond(messages, self.tools.declarations)
            yield AgentEvent("model_call", {"step": step + 1, "duration_ms": round((perf_counter() - model_started) * 1000), **outcome.usage})
            if outcome.kind in ("end_turn", "end_chat"):
                logger.info("agent_finished conversation_id=%s outcome=%s step=%s", context.conversation_id, outcome.kind, step + 1)
                yield AgentEvent("message", {"text": outcome.text})
                yield AgentEvent("done", {"end_chat": outcome.kind == "end_chat", "steps": step + 1})
                return

            assistant_calls = []
            results = []
            for call in outcome.tool_calls:
                yield AgentEvent("tool_call", {"id": call.id, "name": call.name, "arguments": call.arguments})
                tool_started = perf_counter()
                result = await self.tools.execute(call.name, call.arguments, context)
                result["duration_ms"] = round((perf_counter() - tool_started) * 1000)
                results.append(result)
                yield AgentEvent("tool_result", {"id": call.id, "name": call.name, "duration_ms": result["duration_ms"], "result": result})
                if call.name == "logout_session":
                    logout_ok = bool(result.get("ok") and result.get("session_revoked"))
                    yield AgentEvent("session_logout", {
                        "ok": logout_ok,
                        "revocation_scope": result.get("revocation_scope"),
                    })
                    message = result.get("message", "I couldn't end the sign-in session. Please use the sign-out control.")
                    yield AgentEvent("message", {"text": message})
                    yield AgentEvent("done", {"end_chat": logout_ok, "logout": logout_ok, "steps": step + 1})
                    return
                if call.name == "search_manual" and result.get("low_confidence"):
                    escalation = result.get("escalation") or {}
                    reference = escalation.get("reference")
                    yield AgentEvent("escalation_required", {
                        "reason": "low_confidence_retrieval",
                        "confidence_score": result.get("confidence_score", 0),
                        "confidence_threshold": result.get("confidence_threshold"),
                        "ticket_created": bool(reference),
                        "reference": reference,
                    })
                    if reference:
                        answer = f"I couldn’t find a reliable answer in the support manual, so I’ve sent your question to a support specialist. Your reference number is {reference}."
                    else:
                        answer = "I couldn’t find a reliable answer in the support manual. A support specialist should review this, but the ticket queue is unavailable right now. Please contact support directly."
                    yield AgentEvent("message", {"text": answer})
                    yield AgentEvent("done", {"end_chat": False, "requires_human": True, "steps": step + 1})
                    return
                if call.name == "request_login" and result.get("login_required"):
                    yield AgentEvent("login_required", {"message": result.get("message", "Sign in to continue with account-specific support.")})
                    yield AgentEvent("done", {"end_chat": False, "requires_login": True, "steps": step + 1})
                    return
                if call.name == "search_manual" and result.get("sources"):
                    context.sources.extend(result["sources"])
                    for source in result["sources"]:
                        yield AgentEvent("citation", source)
                assistant_calls.append({"id": call.id, "name": call.name, "arguments": call.arguments})
            messages.append({"role": "assistant", "tool_calls": assistant_calls, "provider_content": outcome.provider_content})
            for call, result in zip(outcome.tool_calls, results):
                messages.append({"role": "tool", "name": call.name, "tool_call_id": call.id, "content": result})
        yield AgentEvent("message", {"text": "I’m having trouble completing that request right now. Please try again or contact a support representative."})
        yield AgentEvent("done", {"end_chat": False, "steps": self.max_steps, "limit_reached": True})
