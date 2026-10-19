"""SQLAlchemy 2.x models.  One cycle = one scheduling period.  People are identified by a
name and an unguessable token only; no email addresses are stored anywhere.

All timestamps are naive UTC (SQLite has no time zone type)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATES = ("draft", "collecting", "closed", "solved", "published")


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Cycle(Base):
    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    structure: Mapped[str] = mapped_column(String(16), default="three")
    start_date: Mapped[date] = mapped_column(Date)
    n_days: Mapped[int] = mapped_column(Integer, default=9)
    skip_dates: Mapped[Any] = mapped_column(JSON, default=list)       # ["2026-11-11", ...]
    max_per_day: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[str] = mapped_column(String(8), default="rank")       # rank | points
    min_ranks: Mapped[int] = mapped_column(Integer, default=6)
    state: Mapped[str] = mapped_column(String(16), default="draft")
    admin_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    public_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(500), default="")
    deadline_note: Mapped[str] = mapped_column(Text, default="")
    packages_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # organiser override
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="cycle", cascade="all, delete-orphan", order_by="Participant.id")
    runs: Mapped[list["Run"]] = relationship(back_populates="cycle", cascade="all, delete-orphan", order_by="Run.id")
    edits: Mapped[list["Edit"]] = relationship(cascade="all, delete-orphan", order_by="Edit.id")
    publications: Mapped[list["Publication"]] = relationship(cascade="all, delete-orphan", order_by="Publication.id")


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    budget: Mapped[float] = mapped_column(Float, default=100.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    cycle: Mapped["Cycle"] = relationship(back_populates="participants")
    submission: Mapped[Optional["Submission"]] = relationship(
        back_populates="participant", cascade="all, delete-orphan", uselist=False)


class Submission(Base):
    __tablename__ = "submissions"

    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True)
    cells: Mapped[Any] = mapped_column(JSON, default=dict)             # {slot_id: "3" | "X"}
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    participant: Mapped["Participant"] = relationship(back_populates="submission")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    seeds: Mapped[Any] = mapped_column(JSON, default=list)
    params: Mapped[Any] = mapped_column(JSON, default=dict)
    candidates: Mapped[Any] = mapped_column(JSON, default=list)        # list of result dicts
    chosen_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    excluded_names: Mapped[Any] = mapped_column(JSON, default=list)
    packages_used: Mapped[Any] = mapped_column(JSON, default=dict)

    cycle: Mapped["Cycle"] = relationship(back_populates="runs")
    edits: Mapped[list["Edit"]] = relationship(cascade="all, delete-orphan", order_by="Edit.id")


class Edit(Base):
    __tablename__ = "edits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    description: Mapped[str] = mapped_column(Text)
    owner_before: Mapped[Any] = mapped_column(JSON, default=list)
    owner_after: Mapped[Any] = mapped_column(JSON, default=list)


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    schedule_rows: Mapped[Any] = mapped_column(JSON, default=list)     # one dict per slot
    summary_rows: Mapped[Any] = mapped_column(JSON, default=list)      # organiser-only
    edits_log: Mapped[Any] = mapped_column(JSON, default=list)
