"""LLM profiles and context buffer ORM models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class LLMProfile(Base):
    """User-saved LLM decipher profiles/templates.

    Declared at module level (not nested inside ``ModelRegistry``) so schema
    introspection and migration tooling discover it via ``Base.metadata``.
    """

    __tablename__ = "llm_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_template: Mapped[str] = mapped_column(String(16384), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LLMContextBuffer(Base):
    """Sliding-window context buffer for LLM batch analysis."""

    __tablename__ = "llm_context_buffer"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    correlated_pair: Mapped[dict] = mapped_column(JSON, nullable=False)
    estimated_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    flushed: Mapped[bool] = mapped_column(default=False, nullable=False)
    captured_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    flushed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
