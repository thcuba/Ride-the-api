"""Request pattern, response template, and field mapping ORM models."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models.base import Base


class RequestPattern(Base):
    """Learned request pattern for matching incoming requests."""

    __tablename__ = "request_patterns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    path_pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    required_headers: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    body_schema: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    query_param_keys: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_matched: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ResponseTemplate(Base):
    """Learned response template for local response building."""

    __tablename__ = "response_templates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    pattern_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    headers_template: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    body_template: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    field_mappings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    expected_variables: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FieldMapping(Base):
    """LLM-decoded field mapping between request and response."""

    __tablename__ = "field_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapping_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    request_field: Mapped[str] = mapped_column(String(128), nullable=False)
    request_type: Mapped[str] = mapped_column(String(32), nullable=False)
    response_field: Mapped[str] = mapped_column(String(128), nullable=False)
    response_type: Mapped[str] = mapped_column(String(32), nullable=False)
    transform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    formula: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    enum_values: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    intent: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
