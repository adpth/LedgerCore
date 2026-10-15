"""SQLAlchemy 2 typed models. Amounts are integer minor units (BIGINT); never floats.

Sign convention: ``amount_minor`` is signed, debit positive / credit negative, and
``balances.balance_minor`` is the plain SUM of an account's line amounts.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_NOW = func.now()


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # Target of the composite FKs that pin line/balance currency to the account's.
        UniqueConstraint("id", "currency", name="uq_accounts_id_currency"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="ck_accounts_currency_format"),
        # Migration 0002: per-currency external/settlement ("system") accounts.
        CheckConstraint(
            "NOT is_system OR allow_negative", name="ck_accounts_system_allows_negative"
        ),
        # Migration 0004: one system account per (currency, kind).
        CheckConstraint(
            "system_kind IN ('external', 'holds', 'clearing')",
            name="ck_accounts_system_kind_values",
        ),
        CheckConstraint(
            "is_system = (system_kind IS NOT NULL)", name="ck_accounts_system_kind_matches_flag"
        ),
        Index(
            "uq_accounts_system_currency_kind",
            "currency",
            "system_kind",
            unique=True,
            postgresql_where=text("is_system"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    allow_negative: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    system_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entry_type: Mapped[str] = mapped_column(Text, nullable=False)
    reverses_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id"), nullable=True, unique=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


class EntryLine(Base):
    __tablename__ = "entry_lines"
    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="ck_entry_lines_nonzero"),
        ForeignKeyConstraint(
            ["account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_entry_lines_account_currency",
        ),
        Index("ix_entry_lines_entry_id", "entry_id"),
        Index("ix_entry_lines_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


class Balance(Base):
    __tablename__ = "balances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_balances_account_currency",
        ),
    )

    account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'COMPLETED')", name="ck_idempotency_status"),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutboxEvent(Base):
    """Transactional outbox row. Only ``published_at`` may change (DB trigger, migration 0003)."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index(
            "ix_outbox_events_unpublished",
            "seq",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PayoutRow(Base):
    """One external-payout saga (migration 0004). A DB trigger guards status transitions."""

    __tablename__ = "payouts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_payouts_account_currency",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # also the provider idempotency key
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    hold_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id"), nullable=True
    )
    final_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id"), nullable=True
    )
    release_entry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("journal_entries.id"), nullable=True
    )
    provider_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_NOW
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
