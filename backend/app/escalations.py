"""Redis Streams storage for support tickets.

Two topics share one Redis connection:
- support:escalations  human-review escalations (refunds, closures, low-confidence answers)
- support:tickets      support cases opened for signed-in customers
"""

from datetime import UTC, datetime
import hashlib
import time
import json
import logging
from uuid import uuid4

from redis.asyncio import Redis

from app.config import get_settings


logger = logging.getLogger("support_agent.escalations")


class EscalationQueue:
    stream_name = "support:escalations"
    support_stream_name = "support:tickets"

    def __init__(self) -> None:
        self.redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        self.ready = False
        self.local_revoked_sessions: dict[str, float] = {}

    async def initialize(self) -> None:
        try:
            await self.redis.ping()
            self.ready = True
            logger.info("redis_escalation_queue_ready stream=%s", self.stream_name)
        except Exception as exc:
            self.ready = False
            logger.warning("redis_escalation_queue_unavailable error_type=%s", type(exc).__name__)

    async def create_ticket(self, *, reason: str, category: str, conversation_id: str,
                            customer_id: str | None, customer_email: str | None,
                            amount: float | None = None, order_id: str | None = None) -> dict:
        if not self.ready:
            raise RuntimeError("The escalation queue is unavailable. Start Redis and restart the backend.")
        reference = f"ESC-{datetime.now(UTC):%y%m%d}-{uuid4().hex[:6].upper()}"
        ticket = {
            "reference": reference,
            "created_at": datetime.now(UTC).isoformat(),
            "category": category,
            "reason": reason[:1000],
            "conversation_id": conversation_id,
            "customer_id": customer_id or "",
            "customer_email": customer_email or "",
            "amount": "" if amount is None else str(amount),
            "order_id": order_id or "",
            "status": "pending",
        }
        try:
            stream_id = await self.redis.xadd(self.stream_name, {key: json.dumps(value) if isinstance(value, (dict, list)) else str(value) for key, value in ticket.items()})
        except Exception as exc:
            logger.exception("redis_escalation_ticket_failed reference=%s", reference)
            raise RuntimeError("The escalation queue is unavailable. The ticket was not created; please try again or contact support.") from exc
        logger.info("escalation_ticket_created reference=%s category=%s stream_id=%s", reference, category, stream_id)
        return {"ok": True, "reference": reference, "status": "pending", "message": f"I’ve sent this to our support team. Your reference number is {reference}."}

    async def create_support_ticket(self, *, case_id: str, subject: str, conversation_id: str,
                                    customer_id: str | None, customer_email: str | None) -> dict:
        if not self.ready:
            raise RuntimeError("The support ticket queue is unavailable. Start Redis and restart the backend.")
        reference = f"SUP-{datetime.now(UTC):%y%m%d}-{uuid4().hex[:6].upper()}"
        ticket = {
            "reference": reference,
            "created_at": datetime.now(UTC).isoformat(),
            "case_id": case_id,
            "subject": subject[:1000],
            "conversation_id": conversation_id,
            "customer_id": customer_id or "",
            "customer_email": customer_email or "",
            "status": "open",
        }
        try:
            stream_id = await self.redis.xadd(self.support_stream_name, ticket)
        except Exception as exc:
            logger.exception("redis_support_ticket_failed reference=%s", reference)
            raise RuntimeError("The support ticket queue is unavailable.") from exc
        logger.info("support_ticket_created reference=%s case_id=%s stream_id=%s", reference, case_id, stream_id)
        return {"ok": True, "reference": reference, "status": "open"}

    async def list_tickets(self, limit: int = 100, stream: str | None = None) -> dict:
        stream = stream or self.stream_name
        if not self.ready:
            raise RuntimeError("Redis ticket queue is unavailable")
        rows = await self.redis.xrevrange(stream, max="+", min="-", count=limit)
        tickets = [{"id": stream_id, **fields} for stream_id, fields in rows]
        return {"tickets": tickets, "count": await self.redis.xlen(stream)}

    async def clear_tickets(self, stream: str | None = None) -> int:
        stream = stream or self.stream_name
        if not self.ready:
            raise RuntimeError("Redis ticket queue is unavailable")
        count = await self.redis.xlen(stream)
        await self.redis.delete(stream)
        logger.warning("redis_ticket_stream_cleared stream=%s deleted=%s", stream, count)
        return count

    async def revoke_session_token(self, token: str, ttl_seconds: int) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        ttl = max(1, ttl_seconds)
        self.local_revoked_sessions[digest] = time.time() + ttl
        if self.ready:
            try:
                await self.redis.set(f"support:revoked_sessions:{digest}", "1", ex=ttl)
                return "shared"
            except Exception as exc:
                logger.warning("session_revocation_redis_failed error_type=%s", type(exc).__name__)
        return "instance"

    async def is_session_token_revoked(self, token: str) -> bool:
        now = time.time()
        expired = [digest for digest, expires_at in self.local_revoked_sessions.items() if expires_at <= now]
        for digest in expired:
            self.local_revoked_sessions.pop(digest, None)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if self.local_revoked_sessions.get(digest, 0) > now:
            return True
        if self.ready:
            try:
                return bool(await self.redis.exists(f"support:revoked_sessions:{digest}"))
            except Exception as exc:
                logger.warning("session_revocation_check_failed error_type=%s", type(exc).__name__)
        return False

    async def close(self) -> None:
        await self.redis.aclose()


escalation_queue = EscalationQueue()
