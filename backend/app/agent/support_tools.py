import logging
import re
from datetime import UTC, date, datetime
from uuid import uuid4

from app.agent.tools import ToolError, ToolRegistry
from app.auth import SESSION_TTL_SECONDS
from app.database import store
from app.demo_data import MOCK_CASES, MOCK_CUSTOMERS, MOCK_ORDERS
from app.escalations import escalation_queue
from app.rag import manual_rag
from app.security import verify_password

logger = logging.getLogger("support_agent.support_tools")

ESCALATION_CATEGORIES = ("refund_approval", "account_closure")


def normalize_escalation_category(value: object) -> str | None:
    """Map the model's category wording onto the two supported categories.

    The tool schema lists the exact values, but models sometimes paraphrase
    ("return/refund", "Returns", "close account"); accept clear variants.
    """
    text = re.sub(r"[^a-z]+", " ", str(value).lower()).strip()
    if text.replace(" ", "_") in ESCALATION_CATEGORIES:
        return text.replace(" ", "_")
    words = set(text.split())
    if words & {
        "close",
        "closure",
        "closing",
        "cancel",
        "cancellation",
        "terminate",
        "termination",
        "delete",
        "deletion",
    }:
        return "account_closure"
    if words & {
        "refund",
        "refunds",
        "return",
        "returns",
        "exchange",
        "exchanges",
        "approval",
    }:
        return "refund_approval"
    return None


class SupportData:
    """Demo account fixtures and provider-neutral support tool handlers."""

    def __init__(self) -> None:
        # Passwords are stored only as salted hashes in these local fixtures.
        self.customers = {
            email: dict(profile) for email, profile in MOCK_CUSTOMERS.items()
        }
        self.cases: list[dict] = [dict(case) for case in MOCK_CASES]
        self.orders: list[dict] = [dict(order) for order in MOCK_ORDERS]

    def authenticate_mock_customer(self, email: str, password: str) -> str | None:
        normalized_email = email.strip().lower()
        profile = self.customers.get(normalized_email)
        if not profile or not verify_password(password, profile["password_hash"]):
            return None
        return normalized_email

    async def authenticated_customer(self, context: object) -> dict:
        email = getattr(context, "authenticated_customer_email", None)
        if not email:
            raise ToolError(
                {
                    "ok": False,
                    "error": "Sign in is required for account-specific support.",
                }
            )
        mock_profile = self.customers.get(email.lower())
        if mock_profile is None:
            raise ToolError(
                {
                    "ok": False,
                    "error": "No mock customer profile is available for this signed-in account.",
                }
            )
        database_customer = await store.customer_by_email(email)
        customer = {
            **{
                key: value
                for key, value in mock_profile.items()
                if key != "password_hash"
            },
            **(database_customer or {}),
            "identity_verified": True,
            "contract_status": (database_customer or {}).get(
                "contract_status",
                {
                    "expiry_date": mock_profile["contract_expiry_date"],
                    "is_expired": date.fromisoformat(
                        mock_profile["contract_expiry_date"]
                    )
                    < datetime.now(UTC).date(),
                },
            ),
        }
        return customer

    def registry(self, authenticated_customer_email: str | None = None) -> ToolRegistry:
        registry = ToolRegistry()

        async def search_manual(args: dict, context: object) -> dict:
            query = args.get("query", "")
            result = await manual_rag.search(query)
            if result.get("low_confidence"):
                result["escalation_required"] = True
                email = getattr(context, "authenticated_customer_email", None)
                profile = self.customers.get(email.lower()) if email else None
                database_customer = (
                    await store.customer_by_email(email) if email else None
                )
                try:
                    ticket = await escalation_queue.create_ticket(
                        reason=f"Low-confidence support-manual search: {query[:500]}",
                        category="low_confidence",
                        conversation_id=getattr(context, "conversation_id", ""),
                        customer_id=(database_customer or profile or {}).get("id"),
                        customer_email=email,
                    )
                    result["escalation"] = {
                        "ok": True,
                        "reference": ticket["reference"],
                    }
                except RuntimeError as exc:
                    result["escalation"] = {"ok": False, "error": str(exc)}
            return result

        async def end_chat(_args: dict, _context: object) -> dict:
            return {"ok": True, "message": "Conversation closed."}

        registry.register(
            {
                "name": "search_manual",
                "description": "Search support policy and troubleshooting guides. For a signed-in customer's return request, include the customer's tier and the item details learned from prior account tools in the query.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            search_manual,
        )
        registry.register(
            {
                "name": "end_chat",
                "description": "End the conversation when the customer clearly says goodbye or asks to end the chat.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            end_chat,
        )

        if authenticated_customer_email is None:

            async def request_login(_args: dict, _context: object) -> dict:
                return {
                    "ok": True,
                    "login_required": True,
                    "message": "Sign in to continue with account-specific support.",
                }

            registry.register(
                {
                    "name": "request_login",
                    "description": "Request sign-in when the customer asks for personal account, order, subscription, contract, case, billing, or profile information. Do not ask for credentials in chat.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
                request_login,
            )
        else:

            async def logout_session(_args: dict, context: object) -> dict:
                token = getattr(context, "session_token", None)
                if not token:
                    return {
                        "ok": False,
                        "session_revoked": False,
                        "message": "No active session token was provided.",
                    }
                scope = await escalation_queue.revoke_session_token(
                    token, SESSION_TTL_SECONDS
                )
                return {
                    "ok": True,
                    "session_revoked": True,
                    "revocation_scope": scope,
                    "message": "Signed out of this device.",
                }

            registry.register(
                {
                    "name": "logout_session",
                    "description": "End the currently signed-in customer's session when they ask to log out or sign out. Use this tool instead of only replying in text.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
                logout_session,
            )

            async def lookup_customer(_args: dict, context: object) -> dict:
                customer = await self.authenticated_customer(context)
                return {"ok": True, "customer": customer}

            async def list_cases(_args: dict, context: object) -> dict:
                customer = await self.authenticated_customer(context)
                db_cases = await store.cases_for_customer(customer["id"])
                if db_cases:
                    return {"ok": True, "cases": db_cases}
                return {
                    "ok": True,
                    "cases": [
                        case
                        for case in self.cases
                        if case["customer_id"] == customer["id"]
                    ],
                }

            async def list_orders(_args: dict, context: object) -> dict:
                customer = await self.authenticated_customer(context)
                try:
                    orders = await store.orders_for_customer(customer["id"])
                except RuntimeError as exc:
                    return {"ok": False, "error": str(exc)}
                if orders:
                    return {"ok": True, "orders": orders}
                return {
                    "ok": True,
                    "orders": [
                        order
                        for order in self.orders
                        if order["customer_id"] == customer["id"]
                    ],
                }

            async def create_escalation(args: dict, context: object) -> dict:
                category = normalize_escalation_category(args["category"])
                if category is None:
                    return {
                        "ok": False,
                        "error": f"Unsupported escalation category {args['category']!r}. Use 'refund_approval' for any return or refund, or 'account_closure' to close an account.",
                    }
                amount = args.get("amount")
                customer = await self.authenticated_customer(context)
                if category == "refund_approval":
                    contract_status = customer.get("contract_status", {})
                    is_expired = isinstance(contract_status, dict) and bool(
                        contract_status.get("is_expired")
                    )
                    threshold = (
                        0
                        if is_expired
                        else 2000
                        if customer["tier"].lower() == "plus"
                        else 1000
                    )
                    if amount is None or amount < threshold:
                        return {
                            "ok": False,
                            "error": f"This customer's contract status and tier do not require escalation for refunds below ${threshold:,}. Follow the return policy.",
                        }
                try:
                    return await escalation_queue.create_ticket(
                        reason=args["reason"],
                        category=category,
                        conversation_id=getattr(context, "conversation_id", ""),
                        customer_id=customer["id"],
                        customer_email=customer["email"],
                        amount=amount,
                        order_id=args.get("order_id"),
                    )
                except RuntimeError as exc:
                    return {"ok": False, "error": str(exc)}

            async def create_case(args: dict, context: object) -> dict:
                latest_assistant = next(
                    (
                        message.get("content", "")
                        for message in reversed(getattr(context, "history", []))
                        if message.get("role") == "assistant"
                    ),
                    "",
                )
                affirmative = bool(
                    re.match(
                        r"^\s*(yes|yeah|yep|please do|go ahead|confirm|that's right)\b",
                        getattr(context, "user_message", ""),
                        re.IGNORECASE,
                    )
                )
                asked_confirmation = bool(
                    re.search(
                        r"\b(confirm|should I|would you like me to|shall I)\b",
                        latest_assistant,
                        re.IGNORECASE,
                    )
                )
                if not (affirmative and asked_confirmation):
                    return {
                        "ok": False,
                        "error": "Before creating a case, ask the customer and wait for their explicit confirmation.",
                    }
                customer = await self.authenticated_customer(context)
                db_case = await store.create_case(customer["id"], args["subject"])
                if db_case:
                    case = db_case
                else:
                    case = {
                        "id": f"case_{uuid4().hex[:6]}",
                        "customer_id": customer["id"],
                        "subject": args["subject"],
                        "status": "open",
                        "updated_at": datetime.now(UTC).date().isoformat(),
                    }
                    self.cases.append(case)
                result = {
                    "ok": True,
                    "case": case,
                    "conversation_id": getattr(context, "conversation_id", None),
                }
                # Publish to the support-ticket topic. The case is already saved,
                # so a Redis failure is reported but does not undo it.
                try:
                    ticket = await escalation_queue.create_support_ticket(
                        case_id=str(case["id"]),
                        subject=args["subject"],
                        conversation_id=getattr(context, "conversation_id", "") or "",
                        customer_id=customer.get("id"),
                        customer_email=customer.get("email"),
                    )
                    result["ticket_reference"] = ticket["reference"]
                except RuntimeError as exc:
                    logger.warning(
                        "support_ticket_not_published case_id=%s error=%s",
                        case.get("id"),
                        exc,
                    )
                    result["ticket"] = {"ok": False, "error": str(exc)}
                return result

            registry.register(
                {
                    "name": "lookup_customer",
                    "description": "Look up the currently signed-in customer and return their subscription tier and contract status. Do not accept or request an email in tool arguments.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
                lookup_customer,
            )
            registry.register(
                {
                    "name": "list_customer_cases",
                    "description": "List support cases for the currently signed-in customer.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
                list_cases,
            )
            registry.register(
                {
                    "name": "list_customer_orders",
                    "description": "List orders belonging to the currently signed-in customer, including order date, items, prices, quantities, and total. Never accept a customer ID or email as an argument.",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
                list_orders,
            )
            registry.register(
                {
                    "name": "create_escalation_ticket",
                    "description": "Create a pending human support ticket and return its reference number. Use for any return/refund from a customer with an expired contract; account closure; Standard/other non-Plus refunds of $1,000 or more; or Plus refunds of $2,000 or more. The customer must be signed in. For refund_approval, amount must be the requested refund amount.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "enum": list(ESCALATION_CATEGORIES),
                                "description": "refund_approval for any return or refund request; account_closure to close an account.",
                            },
                            "reason": {"type": "string"},
                            "amount": {"type": "number"},
                            "order_id": {"type": "string"},
                        },
                        "required": ["category", "reason"],
                    },
                },
                create_escalation,
            )
            registry.register(
                {
                    "name": "create_support_case",
                    "description": "Create a support case for the currently signed-in customer after a prior assistant confirmation question and explicit customer approval.",
                    "parameters": {
                        "type": "object",
                        "properties": {"subject": {"type": "string"}},
                        "required": ["subject"],
                    },
                },
                create_case,
            )

        return registry

    def voice_declarations(self) -> list[dict]:
        """Every tool a Live voice session may call, signed in or not.

        A Gemini Live session's tool list is fixed at setup, but the same session
        continues after the customer signs in. FastAPI checks sign-in on every call.
        """
        declarations: dict[str, dict] = {}
        for registry in (self.registry(None), self.registry("voice-schema@invalid")):
            for item in registry.declarations:
                declarations.setdefault(
                    item["name"],
                    {
                        "name": item["name"],
                        "description": item["description"],
                        "parameters": item["parameters"],
                    },
                )
        return list(declarations.values())

    def account_tool_names(self) -> set[str]:
        return {
            item["name"] for item in self.registry("voice-schema@invalid").declarations
        } - {item["name"] for item in self.registry(None).declarations}
