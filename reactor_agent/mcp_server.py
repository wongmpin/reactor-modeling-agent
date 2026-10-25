from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from reactor_agent.domain.models import ReactorModelSpec, BackendCapabilities, SimulationResult
from reactor_agent.domain.validation import validate_spec
from reactor_agent.domain.tools import ValidationResponse, QueueResponse, CaseResponse, WorkflowResponse, AuditEvent
from reactor_agent.hysys.worker import HysysWorker
from reactor_agent.service import ReactorAgentService


def create_mcp_server(service: ReactorAgentService | None = None) -> FastMCP:
    service = service or ReactorAgentService()
    if os.getenv("MCP_HOST", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This application supports local loopback MCP access only.")
    server = FastMCP("reactor-modeling-domain", host=os.getenv("MCP_HOST", "127.0.0.1"), port=int(os.getenv("MCP_PORT", "8765")))

    @server.tool()
    def list_backend_capabilities() -> BackendCapabilities:
        """Probe the local simulator and return only verified capabilities."""
        worker = HysysWorker()
        try:
            return worker.inspect()
        except Exception as exc:
            return BackendCapabilities(backend="Aspen HYSYS COM", detected=False, limitations=[str(exc)])
        finally:
            worker.stop()

    @server.tool()
    def validate_model(spec: ReactorModelSpec) -> ValidationResponse:
        """Validate a ReactorModelSpec JSON document without modifying HYSYS."""
        try:
            report = validate_spec(spec)
            return ValidationResponse(passed=report.passed, errors=report.errors, warnings=report.warnings)
        except Exception as exc:
            return ValidationResponse(passed=False, errors=[str(exc)])

    @server.tool()
    def create_case(task_id: str, case_name: str) -> CaseResponse:
        """Create and save an empty HYSYS case for diagnosis; does not configure a model."""
        if PathLike(case_name).suffix.lower() != ".hsc":
            return CaseResponse(ok=False, error="case_name must be a safe .hsc filename")
        service.database.get_spec(task_id)
        destination = service.data_dir / "cases" / case_name
        if destination.exists():
            return CaseResponse(ok=False, error="Destination already exists; choose an independent case name.")
        worker = HysysWorker()
        try:
            result = worker.create_empty_case(destination)
            service.database.audit(task_id, "empty_case_created", str(result["case_path"]))
            return CaseResponse(ok=True, **result)
        except Exception as exc:
            return CaseResponse(ok=False, error=str(exc))
        finally:
            worker.stop()

    @server.tool()
    def queue_model_run(task_id: str) -> QueueResponse:
        """Queue a validated model specification for the local execution worker."""
        try:
            queue_id = service.queue_execution(task_id)
            return QueueResponse(queued=True, queue_id=queue_id)
        except Exception as exc:
            return QueueResponse(queued=False, error=str(exc))

    @server.tool()
    def get_task_audit(task_id: str) -> list[AuditEvent]:
        """Read structured task audit events; conversation text is not the source of truth."""
        return [AuditEvent.model_validate(event) for event in service.database.audit_events(task_id)]

    @server.tool()
    def interpret_request(request: str, task_id: str | None = None) -> WorkflowResponse:
        """Parse a new task or resume an existing task, with model-driven reactor selection."""
        if task_id is None:
            task_id = service.create_task(request).task_id
            output = service.process_request(task_id, request=request)
        else:
            current = service.database.get_spec(task_id)
            output = service.process_request(task_id, request=request, spec_json=current.model_dump_json())
        return WorkflowResponse.model_validate({key: output[key] for key in WorkflowResponse.model_fields if key in output})

    @server.tool()
    def get_model_spec(task_id: str, version: int | None = None) -> ReactorModelSpec:
        """Read an immutable version of the confirmed/partially confirmed task specification."""
        return service.database.get_spec(task_id, version)

    @server.tool()
    def get_simulation_results(task_id: str) -> list[SimulationResult]:
        """Read persisted HYSYS results; creation, convergence and validation are distinct flags."""
        return [SimulationResult.model_validate(row["payload"]) for row in service.database.list_runs(task_id)]

    return server


class PathLike:
    """Small path-safe validator so MCP input never selects a directory outside case storage."""

    def __init__(self, value: str):
        from pathlib import PurePath
        self.path = PurePath(value)
        self.suffix = self.path.suffix
        if (self.path.name != value or value in {".", ".."}
                or any(ch in value for ch in '<>:"/\\|?*') or value.rstrip(" .") != value):
            self.suffix = ""


def main() -> None:
    create_mcp_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
