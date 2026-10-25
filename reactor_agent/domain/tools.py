"""Explicit MCP output contracts; inputs use domain models and typed arguments."""
from typing import Any
from pydantic import Field
from reactor_agent.domain.models import StrictModel, ReactorModelSpec


class ValidationResponse(StrictModel):
    passed: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class QueueResponse(StrictModel):
    queued: bool
    queue_id: str | None = None
    error: str | None = None


class CaseResponse(StrictModel):
    ok: bool
    case_created: bool = False
    model_built: bool = False
    case_path: str | None = None
    case_title: str | None = None
    error: str | None = None


class WorkflowResponse(StrictModel):
    status: str
    spec: ReactorModelSpec | None = None
    diagnostics: list[str] = Field(default_factory=list)
    model_rationale: str | None = None


class AuditEvent(StrictModel):
    event_type: str
    summary: str
    created_at: str
