import json
import logging
import re
from time import perf_counter
from uuid import uuid4

from google import genai
from google.genai import types

from app.agent.types import AgentModel, ModelOutcome, ToolCall

logger = logging.getLogger("support_agent.gemini")

SYSTEM_PROMPT = """You are a concise, kind customer support agent for {company_name}. Public visitors can ask general questions without signing in; use search_manual for policies and troubleshooting. For personal orders, account, subscription, contract, billing, or case information, call request_login when that tool is available. When account tools are available, use them only for the currently signed-in customer; never ask for credentials in chat or disclose account data without using an account tool. Never invent customer/order data. For a personal return request, work sequentially: call lookup_customer to verify the signed-in customer and get their tier and contract status; call list_customer_orders to find the requested item; call search_manual with the customer's tier, contract status, and item details to check the applicable return policy. Do not call dependent tools before receiving the previous tool result. Follow the retrieved policy: every return/refund for an expired-contract customer requires escalation regardless of tier or amount. For active-contract customers, Plus refunds below $2,000 do not require escalation; Plus refunds of $2,000 or more, and non-Plus refunds of $1,000 or more, require an escalation ticket. Return the ticket reference after escalation. If the request is ambiguous, ask a clarifying question. Account closure requests also require an escalation ticket after sign-in. Never claim a ticket was created unless the tool confirms it. Ask for explicit confirmation before creating a support case. Respect the support manual and cite its title when using retrieved policy. When a signed-in customer asks to log out or sign out, call logout_session and do not only say that they are logged out. If no session is active, tell them they are already signed out. When the customer is done, call end_chat. Do not claim an action succeeded unless its tool result confirms it."""


def is_personal_return_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b(i want|i need|i'd like|i would like|please|can i|could i)\b.{0,80}\b(return|refund)\b|\b(return|refund)\b.{0,80}\b(my|order|item|product)\b",
            text,
            re.IGNORECASE,
        )
    )


def find_requested_order_item(
    orders: list[dict], prompt: str
) -> tuple[dict, dict] | None:
    ignored = {
        "i",
        "want",
        "need",
        "like",
        "please",
        "can",
        "could",
        "would",
        "to",
        "a",
        "an",
        "the",
        "return",
        "refund",
        "my",
        "order",
        "item",
        "product",
        "for",
        "from",
        "of",
        "is",
        "it",
    }
    prompt_terms = set(re.findall(r"[a-z0-9]+", prompt.lower())) - ignored
    candidates: list[tuple[int, dict, dict]] = []
    for order in orders:
        for item in order.get("items", []):
            item_terms = set(re.findall(r"[a-z0-9]+", item.get("item", "").lower()))
            score = len(prompt_terms & item_terms)
            if score:
                candidates.append((score, order, item))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return candidates[0][1], candidates[0][2]


class GeminiAgentModel(AgentModel):
    def __init__(
        self,
        api_key: str,
        model: str,
        company_name: str = "ErwinCo",
        input_usd_per_million: float = 0.30,
        output_usd_per_million: float = 2.50,
    ) -> None:
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.input_usd_per_million = input_usd_per_million
        self.output_usd_per_million = output_usd_per_million
        self.system_prompt = SYSTEM_PROMPT.format(company_name=company_name)

    async def respond(self, messages: list[dict], tools: list[dict]) -> ModelOutcome:
        declarations = [
            types.FunctionDeclaration.model_validate(decl) for decl in tools
        ]
        gemini_tools = [types.Tool(function_declarations=declarations)]
        contents = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_function_response(
                                name=message["name"], response=message["content"]
                            )
                        ],
                    )
                )
            elif role == "assistant" and message.get("provider_content") is not None:
                # Preserve Gemini 3 thought signatures and other provider metadata on tool turns.
                contents.append(message["provider_content"])
            elif role == "assistant" and message.get("tool_calls"):
                parts = [
                    types.Part.from_function_call(
                        name=call["name"], args=call["arguments"]
                    )
                    for call in message["tool_calls"]
                ]
                contents.append(types.Content(role="model", parts=parts))
            else:
                contents.append(
                    types.Content(
                        role="user" if role == "user" else "model",
                        parts=[types.Part(text=message["content"])],
                    )
                )
        started = perf_counter()
        logger.info(
            "gemini_request model=%s messages=%s tools=%s",
            self.model,
            len(contents),
            len(tools),
        )
        try:
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=self.system_prompt,
                    tools=gemini_tools,
                    temperature=0.2,
                ),
            )
        except Exception:
            logger.exception(
                "gemini_request_failed model=%s elapsed_ms=%s",
                self.model,
                round((perf_counter() - started) * 1000),
            )
            raise
        candidate = response.candidates[0] if response.candidates else None
        usage_metadata = response.usage_metadata
        input_tokens = (
            int(usage_metadata.prompt_token_count or 0) if usage_metadata else 0
        )
        output_tokens = (
            int(usage_metadata.candidates_token_count or 0) if usage_metadata else 0
        )
        input_cost = input_tokens * self.input_usd_per_million / 1_000_000
        output_cost = output_tokens * self.output_usd_per_million / 1_000_000
        usage = {
            "model": self.model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_cost_usd": round(input_cost, 10),
            "output_cost_usd": round(output_cost, 10),
            "estimated_cost_usd": round(input_cost + output_cost, 10),
            "input_usd_per_million": self.input_usd_per_million,
            "output_usd_per_million": self.output_usd_per_million,
        }
        calls = []
        text_parts = []
        if candidate and candidate.content:
            part_summary = [
                f"function_call:{part.function_call.name}"
                if part.function_call
                else f"text_chars:{len(part.text or '')}"
                for part in candidate.content.parts or []
            ]
            logger.debug(
                "gemini_candidate_parts model=%s parts=%s", self.model, part_summary
            )
        if candidate and candidate.content:
            for part in candidate.content.parts or []:
                if part.function_call:
                    fc = part.function_call
                    calls.append(
                        ToolCall(
                            id=f"call_{uuid4().hex[:12]}",
                            name=fc.name or "",
                            arguments=dict(fc.args or {}),
                        )
                    )
                elif part.text:
                    text_parts.append(part.text)
        finish_reason = str(candidate.finish_reason) if candidate else "no_candidate"
        logger.info(
            "gemini_response model=%s elapsed_ms=%s candidate=%s text_chars=%s tool_calls=%s tool_names=%s finish_reason=%s input_tokens=%s output_tokens=%s",
            self.model,
            round((perf_counter() - started) * 1000),
            bool(candidate),
            sum(len(part) for part in text_parts),
            len(calls),
            [call.name for call in calls],
            finish_reason,
            usage["input_tokens"],
            usage["output_tokens"],
        )
        if calls:
            logout_call = next(
                (call for call in calls if call.name == "logout_session"), None
            )
            if logout_call:
                return ModelOutcome(
                    "tool_use",
                    "".join(text_parts),
                    [logout_call],
                    usage,
                    candidate.content if candidate else None,
                )
            if any(call.name == "end_chat" for call in calls):
                return ModelOutcome(
                    "end_chat", "Thanks for contacting support. Goodbye!", usage=usage
                )
            return ModelOutcome(
                "tool_use",
                "".join(text_parts),
                calls,
                usage,
                candidate.content if candidate else None,
            )
        return ModelOutcome(
            "end_turn",
            "".join(text_parts).strip()
            or "I’m sorry, I couldn’t prepare a response. Could you rephrase that?",
            usage=usage,
        )


class DemoAgentModel(AgentModel):
    """Predictable offline provider used until a Gemini key is configured."""

    async def respond(self, messages: list[dict], tools: list[dict]) -> ModelOutcome:
        if messages[-1].get("role") == "tool":
            result = messages[-1].get("content", {})
            tool_name = messages[-1].get("name")
            if (
                isinstance(result, dict)
                and result.get("reference")
                and result.get("message")
            ):
                return ModelOutcome("end_turn", str(result["message"]))
            if isinstance(result, dict) and result.get("ok") is False:
                return ModelOutcome(
                    "end_turn",
                    f"I couldn’t complete that request: {result.get('error', 'Please contact support for help.')}",
                )
            previous_user = next(
                (
                    message.get("content", "")
                    for message in reversed(messages[:-1])
                    if message.get("role") == "user"
                ),
                "",
            )
            return_request = is_personal_return_request(str(previous_user))
            available_tools = {tool["name"] for tool in tools}

            if (
                tool_name == "lookup_customer"
                and return_request
                and result.get("customer")
                and "list_customer_orders" in available_tools
            ):
                return ModelOutcome(
                    "tool_use",
                    tool_calls=[
                        ToolCall(f"call_{uuid4().hex[:8]}", "list_customer_orders", {})
                    ],
                )

            if (
                tool_name == "list_customer_orders"
                and return_request
                and result.get("orders")
            ):
                selected = find_requested_order_item(
                    result["orders"], str(previous_user)
                )
                if selected is None:
                    return ModelOutcome(
                        "end_turn",
                        "I couldn’t identify a unique matching item in your orders. Please share the product name or order number.",
                    )
                order, item = selected
                lookup = next(
                    (
                        message.get("content", {})
                        for message in reversed(messages[:-1])
                        if message.get("role") == "tool"
                        and message.get("name") == "lookup_customer"
                    ),
                    {},
                )
                customer = (
                    lookup.get("customer", {}) if isinstance(lookup, dict) else {}
                )
                tier = customer.get("tier", "Standard")
                contract_status = customer.get("contract_status", {})
                contract_label = (
                    "expired contract"
                    if isinstance(contract_status, dict)
                    and contract_status.get("is_expired")
                    else "active contract"
                )
                item_amount = float(item["price"]) * int(item.get("quantity", 1))
                query = f"{tier} tier {contract_label} return eligibility for {item['item']} from order {order['order_id']} item price ${item_amount:.2f}; delivery window condition refund approval threshold"
                return ModelOutcome(
                    "tool_use",
                    tool_calls=[
                        ToolCall(
                            f"call_{uuid4().hex[:8]}", "search_manual", {"query": query}
                        )
                    ],
                )

            if tool_name == "search_manual" and return_request:
                order_result = next(
                    (
                        message.get("content", {})
                        for message in reversed(messages[:-1])
                        if message.get("role") == "tool"
                        and message.get("name") == "list_customer_orders"
                    ),
                    {},
                )
                orders = (
                    order_result.get("orders", [])
                    if isinstance(order_result, dict)
                    else []
                )
                selected = find_requested_order_item(orders, str(previous_user))
                if selected:
                    order, item = selected
                    item_amount = float(item["price"]) * int(item.get("quantity", 1))
                    lookup = next(
                        (
                            message.get("content", {})
                            for message in reversed(messages[:-1])
                            if message.get("role") == "tool"
                            and message.get("name") == "lookup_customer"
                        ),
                        {},
                    )
                    customer = (
                        lookup.get("customer", {}) if isinstance(lookup, dict) else {}
                    )
                    tier = customer.get("tier", "Standard")
                    contract_status = customer.get("contract_status", {})
                    is_expired = isinstance(contract_status, dict) and bool(
                        contract_status.get("is_expired")
                    )
                    threshold = (
                        0
                        if is_expired
                        else 2000
                        if str(tier).lower() == "plus"
                        else 1000
                    )
                    if (
                        item_amount >= threshold
                        and "create_escalation_ticket" in available_tools
                    ):
                        reason = f"Return request for {item['item']} from {order['order_id']}; item refund value ${item_amount:.2f}. Customer request: {previous_user}"
                        return ModelOutcome(
                            "tool_use",
                            tool_calls=[
                                ToolCall(
                                    f"call_{uuid4().hex[:8]}",
                                    "create_escalation_ticket",
                                    {
                                        "category": "refund_approval",
                                        "reason": reason,
                                        "amount": item_amount,
                                        "order_id": order["order_id"],
                                    },
                                )
                            ],
                        )
                    matches = (
                        result.get("matches", []) if isinstance(result, dict) else []
                    )
                    policy = (
                        matches[0].get(
                            "text", "Please verify return eligibility with support."
                        )
                        if matches
                        else "Please verify return eligibility with support."
                    )
                    return ModelOutcome(
                        "end_turn",
                        f"I found {item['item']} on order {order['order_id']} at ${item_amount:.2f}. {policy}",
                    )
            data = (
                result.get("customer")
                or result.get("orders")
                or result.get("cases")
                or result.get("matches")
                or result
                if isinstance(result, dict)
                else result
            )
            return ModelOutcome(
                "end_turn",
                f"I checked that for you. {json.dumps(data, ensure_ascii=False)}",
            )
        text = messages[-1].get("content", "")
        normalized = text.lower()
        available_tools = {tool["name"] for tool in tools}
        escalation_available = "create_escalation_ticket" in available_tools
        logout_request = bool(
            re.search(
                r"\b(?:log|sign)\s+(?:me\s+)?out\b|\b(?:logout|signout)\b", normalized
            )
        )
        if logout_request:
            if "logout_session" in available_tools:
                return ModelOutcome(
                    "tool_use",
                    tool_calls=[
                        ToolCall(f"call_{uuid4().hex[:8]}", "logout_session", {})
                    ],
                )
            return ModelOutcome("end_turn", "You’re already signed out.")
        account_closure = any(
            word in normalized for word in ("close", "cancel", "terminate")
        ) and any(word in normalized for word in ("account", "subscription"))
        return_request = is_personal_return_request(text)
        if return_request and "request_login" in available_tools:
            return ModelOutcome(
                "tool_use",
                tool_calls=[ToolCall(f"call_{uuid4().hex[:8]}", "request_login", {})],
            )
        if return_request and "lookup_customer" in available_tools:
            return ModelOutcome(
                "tool_use",
                tool_calls=[ToolCall(f"call_{uuid4().hex[:8]}", "lookup_customer", {})],
            )
        refund_request = (
            "refund" in normalized
            and "policy" not in normalized
            and bool(
                re.search(r"\b(i|me|my|please|want|need|request|order)\b", normalized)
            )
        )
        if (account_closure or refund_request) and "request_login" in available_tools:
            return ModelOutcome(
                "tool_use",
                tool_calls=[ToolCall(f"call_{uuid4().hex[:8]}", "request_login", {})],
            )
        if escalation_available and account_closure:
            return ModelOutcome(
                "tool_use",
                tool_calls=[
                    ToolCall(
                        f"call_{uuid4().hex[:8]}",
                        "create_escalation_ticket",
                        {"category": "account_closure", "reason": text},
                    )
                ],
            )
        refund_amount = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text) or re.search(
            r"\b(?:refund(?: of)?|amount(?:ing)? to)\s+([\d,]+(?:\.\d{1,2})?)\b",
            text,
            re.IGNORECASE,
        )
        if (
            escalation_available
            and refund_request
            and refund_amount
            and float(refund_amount.group(1).replace(",", "")) >= 1000
        ):
            amount = float(refund_amount.group(1).replace(",", ""))
            return ModelOutcome(
                "tool_use",
                tool_calls=[
                    ToolCall(
                        f"call_{uuid4().hex[:8]}",
                        "create_escalation_ticket",
                        {
                            "category": "refund_approval",
                            "reason": text,
                            "amount": amount,
                        },
                    )
                ],
            )
        if (
            escalation_available
            and refund_request
            and not refund_amount
            and "list_customer_orders" in available_tools
        ):
            return ModelOutcome(
                "tool_use",
                tool_calls=[
                    ToolCall(f"call_{uuid4().hex[:8]}", "list_customer_orders", {})
                ],
            )
        account_request = bool(
            re.search(
                r"\b(my|mine|me|i)\b.{0,50}\b(order|account|subscription|tier|plan|contract|case|billing|payment|profile|address|invoice|shipment|tracking)\b",
                normalized,
            )
        )
        if any(
            word in normalized for word in ("bye", "goodbye", "end chat", "that's all")
        ):
            return ModelOutcome("end_chat", "Thanks for contacting support. Goodbye!")
        if account_request:
            if "request_login" in available_tools:
                return ModelOutcome(
                    "tool_use",
                    tool_calls=[
                        ToolCall(f"call_{uuid4().hex[:8]}", "request_login", {})
                    ],
                )
            if "list_customer_cases" in available_tools and any(
                word in normalized for word in ("case", "ticket")
            ):
                name = "list_customer_cases"
            elif "lookup_customer" in available_tools:
                name = "lookup_customer"
            else:
                return ModelOutcome(
                    "end_turn",
                    "I can’t access that account information right now. Please contact support.",
                )
            return ModelOutcome(
                "tool_use", tool_calls=[ToolCall(f"call_{uuid4().hex[:8]}", name, {})]
            )
        if any(
            word in normalized
            for word in ("return", "refund", "policy", "manual", "shipping", "delayed")
        ):
            return ModelOutcome(
                "tool_use",
                tool_calls=[
                    ToolCall(
                        f"call_{uuid4().hex[:8]}", "search_manual", {"query": text}
                    )
                ],
            )
        return ModelOutcome(
            "end_turn",
            "Hi! I can help with shipping, returns, account questions, and support cases. What can I help you with?",
        )
