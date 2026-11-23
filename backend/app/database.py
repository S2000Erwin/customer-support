import asyncio
import logging
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    delete,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings
from app.demo_data import MOCK_CASES, MOCK_CUSTOMERS, MOCK_ORDERS
from app.security import verify_password

logger = logging.getLogger("support_agent.database")


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: uuid4().hex
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Trace(Base):
    __tablename__ = "trace_events"
    id: Mapped[str] = mapped_column(
        String(40), primary_key=True, default=lambda: uuid4().hex
    )
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    label: Mapped[str] = mapped_column(String(240))
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class CustomerRecord(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(240), unique=True, index=True)
    tier: Mapped[str] = mapped_column(String(40), default="Standard")
    contract_expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class SupportCaseRecord(Base):
    __tablename__ = "support_cases"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    subject: Mapped[str] = mapped_column(String(240))
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class OrderRecord(Base):
    __tablename__ = "orders"
    order_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), index=True
    )
    items: Mapped[list[dict]] = mapped_column(JSONB)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))


def async_database_url(raw_url: str) -> URL:
    """Accept hosted Postgres URLs as given (Neon, Vercel Postgres, Supabase).

    They use postgres:// or postgresql:// with ?sslmode=require; asyncpg needs
    the postgresql+asyncpg driver and ssl=require, and rejects channel_binding.
    """
    url = make_url(raw_url)
    if url.drivername in {
        "postgres",
        "postgresql",
        "postgresql+psycopg2",
        "postgresql+psycopg",
    }:
        url = url.set(drivername="postgresql+asyncpg")
    query = dict(url.query)
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    query.pop("channel_binding", None)
    return url.set(query=query)


class SupportStore:
    def __init__(self) -> None:
        self.engine = create_async_engine(
            async_database_url(get_settings().database_url), pool_pre_ping=True
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.available = False
        self.memory_conversations: dict[str, dict] = {}

    async def initialize(self) -> None:
        """Single setup path: create missing tables, then add any missing demo rows.

        The SQLAlchemy models plus app/demo_data.py are the whole schema and seed;
        there are no separate migration files. Existing rows are never modified.
        """
        # Serverless platforms can start several instances at once against a
        # fresh database, so every step must tolerate a concurrent twin.
        try:
            await self._create_tables()
        except Exception as exc:  # noqa: BLE001
            self.available = False
            logger.warning(
                "postgres_unavailable fallback=memory-demo error_type=%s error=%s",
                type(exc).__name__,
                self._safe_error(exc),
            )
            return
        self.available = True
        try:
            seeded = await self._seed_demo_data()
            logger.info("postgres_ready seeded=%s", seeded)
        except Exception as exc:  # noqa: BLE001
            # The database works; only the demo seed failed. Keep using it.
            logger.warning(
                "postgres_seed_failed error_type=%s error=%s",
                type(exc).__name__,
                self._safe_error(exc),
            )

    async def _create_tables(self) -> None:
        for attempt in range(3):
            try:
                async with self.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                return
            except (IntegrityError, ProgrammingError):
                # Another instance created the same table at the same moment;
                # retrying finds it and skips it.
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5)

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Error text for logs, with any connection-string password removed."""
        message = str(exc).splitlines()[0][:300] if str(exc) else ""
        return re.sub(r"://[^@\s/]+@", "://***@", message)

    def _customer_values(self, profile: dict) -> dict:
        return {
            "id": profile["id"],
            "name": profile["name"],
            "email": profile["email"],
            "tier": profile["tier"],
            "contract_expiry_date": date.fromisoformat(profile["contract_expiry_date"]),
            "password_hash": profile["password_hash"],
            "created_at": datetime.now(UTC),
        }

    def _customer_record(self, profile: dict) -> "CustomerRecord":
        values = self._customer_values(profile)
        values.pop("created_at")
        return CustomerRecord(**values)

    async def _seed_demo_data(self) -> dict:
        """Insert missing demo customers, cases and orders.

        INSERT ... ON CONFLICT DO NOTHING makes this safe to run from several
        instances at once and never modifies existing rows.
        """
        async with self.sessions() as session:
            customers = await session.execute(
                pg_insert(CustomerRecord)
                .values([self._customer_values(p) for p in MOCK_CUSTOMERS.values()])
                .on_conflict_do_nothing()
                .returning(CustomerRecord.id)
            )
            new_customers = len(customers.all())
            known_customers = set(
                (await session.scalars(select(CustomerRecord.id))).all()
            )
            now = datetime.now(UTC)
            case_rows = [
                {
                    "id": c["id"],
                    "customer_id": c["customer_id"],
                    "subject": c["subject"],
                    "status": c["status"],
                    "created_at": now,
                }
                for c in MOCK_CASES
                if c["customer_id"] in known_customers
            ]
            order_rows = [
                {
                    "order_id": o["order_id"],
                    "ordered_at": datetime.fromisoformat(o["ordered_at"]),
                    "customer_id": o["customer_id"],
                    "items": o["items"],
                    "total_amount": Decimal(str(o["total_amount"])),
                }
                for o in MOCK_ORDERS
                if o["customer_id"] in known_customers
            ]
            new_cases = new_orders = 0
            if case_rows:
                result = await session.execute(
                    pg_insert(SupportCaseRecord)
                    .values(case_rows)
                    .on_conflict_do_nothing()
                    .returning(SupportCaseRecord.id)
                )
                new_cases = len(result.all())
            if order_rows:
                result = await session.execute(
                    pg_insert(OrderRecord)
                    .values(order_rows)
                    .on_conflict_do_nothing()
                    .returning(OrderRecord.order_id)
                )
                new_orders = len(result.all())
            await session.commit()
        return {
            "customers": new_customers,
            "support_cases": new_cases,
            "orders": new_orders,
        }

    @staticmethod
    def _order_record(order: dict) -> OrderRecord:
        return OrderRecord(
            order_id=order["order_id"],
            ordered_at=datetime.fromisoformat(order["ordered_at"]),
            customer_id=order["customer_id"],
            items=order["items"],
            total_amount=Decimal(str(order["total_amount"])),
        )

    async def reset_customer_db(self) -> dict:
        if not self.available:
            raise RuntimeError("PostgreSQL is unavailable")
        async with self.sessions() as session:
            await session.execute(delete(OrderRecord))
            await session.execute(delete(SupportCaseRecord))
            await session.execute(delete(CustomerRecord))
            session.add_all(
                [self._customer_record(profile) for profile in MOCK_CUSTOMERS.values()]
            )
            await session.flush()
            session.add_all(
                [
                    SupportCaseRecord(
                        id=case["id"],
                        customer_id=case["customer_id"],
                        subject=case["subject"],
                        status=case["status"],
                    )
                    for case in MOCK_CASES
                ]
            )
            session.add_all(self._order_record(order) for order in MOCK_ORDERS)
            await session.commit()
        return {
            "customers": len(MOCK_CUSTOMERS),
            "support_cases": len(MOCK_CASES),
            "orders": len(MOCK_ORDERS),
        }

    async def authenticate_customer(self, email: str, password: str) -> str | None:
        if not self.available:
            return None
        async with self.sessions() as session:
            row = await session.scalar(
                select(CustomerRecord).where(
                    CustomerRecord.email == email.strip().lower()
                )
            )
            if row is None or not verify_password(password, row.password_hash):
                return None
            return row.email

    async def customer_by_email(self, email: str) -> dict | None:
        if not self.available:
            return None
        async with self.sessions() as session:
            row = await session.scalar(
                select(CustomerRecord).where(CustomerRecord.email == email.lower())
            )
            return (
                {
                    "id": row.id,
                    "name": row.name,
                    "email": row.email,
                    "tier": row.tier,
                    "contract_expiry_date": row.contract_expiry_date.isoformat(),
                    "contract_status": {
                        "expiry_date": row.contract_expiry_date.isoformat(),
                        "is_expired": row.contract_expiry_date < date.today(),
                    },
                }
                if row
                else None
            )

    async def cases_for_customer(self, customer_id: str) -> list[dict]:
        if not self.available:
            return []
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(SupportCaseRecord)
                    .where(SupportCaseRecord.customer_id == customer_id)
                    .order_by(SupportCaseRecord.created_at.desc())
                )
            ).all()
            return [
                {
                    "id": row.id,
                    "customer_id": row.customer_id,
                    "subject": row.subject,
                    "status": row.status,
                    "updated_at": row.created_at.date().isoformat(),
                }
                for row in rows
            ]

    async def orders_for_customer(self, customer_id: str) -> list[dict]:
        if not self.available:
            return []
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(OrderRecord)
                    .where(OrderRecord.customer_id == customer_id)
                    .order_by(OrderRecord.ordered_at.desc())
                )
            ).all()
            return [
                {
                    "order_id": row.order_id,
                    "ordered_at": row.ordered_at.isoformat(),
                    "customer_id": row.customer_id,
                    "items": row.items,
                    "total_amount": float(row.total_amount),
                }
                for row in rows
            ]

    async def create_case(self, customer_id: str, subject: str) -> dict | None:
        if not self.available:
            return None
        case = SupportCaseRecord(
            id=f"case_{uuid4().hex[:8]}",
            customer_id=customer_id,
            subject=subject,
            status="open",
        )
        async with self.sessions() as session:
            session.add(case)
            await session.commit()
            await session.refresh(case)
        return {
            "id": case.id,
            "customer_id": case.customer_id,
            "subject": case.subject,
            "status": case.status,
            "updated_at": case.created_at.date().isoformat(),
        }

    async def add_message(self, conversation_id: str, role: str, content: str) -> None:
        now = datetime.now(UTC)
        if self.available:
            async with self.sessions() as session:
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None:
                    conversation = Conversation(
                        id=conversation_id,
                        title=content[:64],
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(conversation)
                conversation.updated_at = now
                session.add(
                    Message(
                        conversation_id=conversation_id,
                        role=role,
                        content=content,
                        created_at=now,
                    )
                )
                await session.commit()
        else:
            entry = self.memory_conversations.setdefault(
                conversation_id,
                {
                    "id": conversation_id,
                    "title": content[:64],
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                    "messages": [],
                    "events": [],
                },
            )
            entry["updated_at"] = now
            entry["messages"].append(
                {"role": role, "content": content, "created_at": now}
            )

    async def add_trace(
        self,
        conversation_id: str,
        kind: str,
        label: str,
        detail: str | None,
        duration_ms: int,
        payload: dict,
        parent_id: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        if self.available:
            async with self.sessions() as session:
                if await session.get(Conversation, conversation_id) is None:
                    session.add(
                        Conversation(
                            id=conversation_id,
                            title="New conversation",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    await session.flush()
                trace = Trace(
                    conversation_id=conversation_id,
                    parent_id=parent_id,
                    kind=kind,
                    label=label,
                    detail=detail,
                    duration_ms=duration_ms,
                    payload=payload,
                    created_at=now,
                )
                session.add(trace)
                await session.flush()
                await session.commit()
                return trace.id
        else:
            entry = self.memory_conversations.setdefault(
                conversation_id,
                {
                    "id": conversation_id,
                    "title": label,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                    "messages": [],
                    "events": [],
                },
            )
            trace_id = uuid4().hex[:12]
            entry["events"].append(
                {
                    "id": trace_id,
                    "parent_id": parent_id,
                    "type": kind,
                    "label": label,
                    "detail": detail,
                    "duration_ms": duration_ms,
                    "metadata": payload,
                    "started_at": now.isoformat(),
                }
            )
            return trace_id

    async def latest_prompt_trace_id(
        self, conversation_id: str, content: str
    ) -> str | None:
        if self.available:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(Trace)
                        .where(
                            Trace.conversation_id == conversation_id,
                            Trace.kind == "user_prompt",
                        )
                        .order_by(Trace.created_at.desc())
                        .limit(100)
                    )
                ).all()
                return next(
                    (
                        row.id
                        for row in rows
                        if (row.payload or {}).get("content") == content
                    ),
                    None,
                )
        events = self.memory_conversations.get(conversation_id, {}).get("events", [])
        return next(
            (
                event["id"]
                for event in reversed(events)
                if event["type"] == "user_prompt"
                and event.get("metadata", {}).get("content") == content
            ),
            None,
        )

    async def get_history(self, conversation_id: str) -> list[dict]:
        if self.available:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.created_at)
                    )
                ).all()
                return [{"role": row.role, "content": row.content} for row in rows]
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.memory_conversations.get(conversation_id, {}).get(
                "messages", []
            )
        ]

    async def dashboard(self) -> dict:
        if self.available:
            async with self.sessions() as session:
                conversations = (
                    await session.scalars(
                        select(Conversation)
                        .order_by(Conversation.updated_at.desc())
                        .limit(100)
                    )
                ).all()
                traces = (
                    await session.scalars(
                        select(Trace).order_by(Trace.created_at.desc()).limit(500)
                    )
                ).all()
                msgs = (
                    await session.scalars(
                        select(Message).order_by(Message.created_at.desc()).limit(500)
                    )
                ).all()
                {c.id: c for c in conversations}
                summaries = []
                for c in conversations:
                    ct = [t for t in traces if t.conversation_id == c.id]
                    cm = [m for m in msgs if m.conversation_id == c.id]
                    ct.sort(key=lambda item: item.created_at)
                    cm.sort(key=lambda item: item.created_at)
                    duration = sum(t.duration_ms for t in ct)
                    summaries.append(
                        {
                            "id": c.id,
                            "title": c.title,
                            "created_at": c.created_at.isoformat(),
                            "updated_at": c.updated_at.isoformat(),
                            "message_count": len(cm),
                            "duration_ms": duration,
                            "status": c.status,
                            "events": [
                                {
                                    "id": t.id,
                                    "parent_id": t.parent_id,
                                    "type": t.kind,
                                    "label": t.label,
                                    "detail": t.detail,
                                    "duration_ms": t.duration_ms,
                                    "metadata": t.payload,
                                    "started_at": t.created_at.isoformat(),
                                }
                                for t in ct
                            ],
                            "messages": [
                                {
                                    "role": m.role,
                                    "content": m.content,
                                    "created_at": m.created_at.isoformat(),
                                }
                                for m in cm
                            ],
                        }
                    )
                input_tokens = sum(
                    int((t.payload or {}).get("input_tokens", 0)) for t in traces
                )
                output_tokens = sum(
                    int((t.payload or {}).get("output_tokens", 0)) for t in traces
                )
                return {
                    "conversations": summaries,
                    "usage": {
                        "conversations": len(summaries),
                        "messages": len(msgs),
                        "tool_calls": sum(t.kind == "tool_call" for t in traces),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "avg_latency_ms": round(
                            sum(t.duration_ms for t in traces) / max(len(traces), 1)
                        ),
                    },
                }
        conversations = list(self.memory_conversations.values())
        events = [e for c in conversations for e in c["events"]]
        messages = [m for c in conversations for m in c["messages"]]
        summaries = []
        for c in reversed(conversations):
            summaries.append(
                {
                    **{
                        k: c[k].isoformat() if isinstance(c[k], datetime) else c[k]
                        for k in ("id", "title", "created_at", "updated_at", "status")
                    },
                    "message_count": len(c["messages"]),
                    "duration_ms": sum(e["duration_ms"] for e in c["events"]),
                    "events": c["events"],
                    "messages": c["messages"],
                }
            )
        input_tokens = sum(int(e["metadata"].get("input_tokens", 0)) for e in events)
        output_tokens = sum(int(e["metadata"].get("output_tokens", 0)) for e in events)
        return {
            "conversations": summaries,
            "usage": {
                "conversations": len(conversations),
                "messages": len(messages),
                "tool_calls": sum(e["type"] == "tool_call" for e in events),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "avg_latency_ms": round(
                    sum(e["duration_ms"] for e in events) / max(len(events), 1)
                ),
            },
        }


store = SupportStore()
