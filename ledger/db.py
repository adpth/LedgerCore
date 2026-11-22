import os
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Numeric,
    String,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("id", "currency"),
        CheckConstraint("kind IN ('wallet', 'clearing', 'reserve')"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(12))


class Balance(Base):
    __tablename__ = "balances"
    __table_args__ = (CheckConstraint("amount = trunc(amount)"),)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Numeric, server_default="0")


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (UniqueConstraint("id", "currency"),)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(160), default="")
    reversal_of: Mapped[int | None] = mapped_column(ForeignKey("entries.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    writer: Mapped[str] = mapped_column(String, server_default=text("pg_current_xact_id()::text"))


class Line(Base):
    __tablename__ = "lines"
    __table_args__ = (
        ForeignKeyConstraint(["entry_id", "currency"], ["entries.id", "entries.currency"]),
        ForeignKeyConstraint(["account_id", "currency"], ["accounts.id", "accounts.currency"]),
        CheckConstraint("amount <> 0"),
    )
    entry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[int] = mapped_column(BigInteger)


class Idempotency(Base):
    __tablename__ = "idempotency"
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), default="PENDING")
    response: Mapped[dict | None] = mapped_column(JSONB)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(ForeignKey("entries.id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(ForeignKey("entries.id"), primary_key=True)
    wallet: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    reserve: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    clearing: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    amount: Mapped[int] = mapped_column(BigInteger)
    destination: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    provider_status: Mapped[str | None] = mapped_column(String(12))
    final_entry: Mapped[int | None] = mapped_column(ForeignKey("entries.id"))
    retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5432/ledger")


engine = create_engine(
    database_url(),
    pool_pre_ping=True,
    hide_parameters=True,
    pool_timeout=5,
    connect_args={"connect_timeout": 5},
)
Session = sessionmaker(engine, expire_on_commit=False)
