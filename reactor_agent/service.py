from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from reactor_agent.agent.graph import build_workflow, invoke_workflow
from reactor_agent.domain.models import ReactorModelSpec, SimulationResult
from reactor_agent.domain.validation import validate_reactor_choice, validate_spec, validate_backend_spec, validate_engineering
from reactor_agent.storage.db import Database
from reactor_agent.harness import Harness


class ReactorAgentService:
    def __init__(self, database: Database | None = None, data_dir: str | Path | None = None, provider: Any | None = None):
        self.provider = provider
        self.data_dir = Path(data_dir or os.getenv("REACTOR_AGENT_DATA_DIR", "./data")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = database or Database(os.getenv("REACTOR_AGENT_DB", str(self.data_dir / "reactor_agent.sqlite3")))

    def create_task(self, user_request: str, spec: ReactorModelSpec | None = None) -> ReactorModelSpec:
        spec = spec or ReactorModelSpec(user_request=user_request)
        spec = spec.model_copy(update={"user_request": user_request or spec.user_request})
        self.database.create_task(spec)
        return spec

    def revise_spec(self, task_id: str, changes: dict[str, Any]) -> ReactorModelSpec:
        prior = self.database.get_spec(task_id)
        revised = prior.next_version(**changes)
        self.database.save_spec(revised)
        return revised

    def process_request(self, task_id: str, request: str = "", spec_json: str = "") -> dict[str, Any]:
        if not spec_json:
            current = self.database.get_spec(task_id)
            if current.version > 1:
                spec_json = current.model_dump_json()
        # SqliteSaver serializes access but LangGraph writes checkpoints on its executor thread.
        conn = self.database.connect(check_same_thread=False)
        try:
            graph = build_workflow(provider=self.provider, connection=conn)
            output = invoke_workflow(graph, request, spec_json, thread_id=task_id)
        finally:
            conn.close()
        if output.get("spec"):
            spec = ReactorModelSpec.model_validate(output["spec"])
            try:
                prior = self.database.get_spec(task_id)
                spec = spec.model_copy(update={
                    "task_id": task_id,
                    "version": prior.version + 1,
                    "parent_version": prior.version,
                })
                self.database.save_spec(spec, output.get("status", "needs_input"))
            except KeyError:
                spec = spec.model_copy(update={"task_id": task_id, "version": 1, "parent_version": None})
                self.database.create_task(spec)
                self.database.set_status(task_id, output.get("status", "needs_input"))
            output["spec"] = spec.model_dump(mode="json")
        else:
            self.database.set_status(task_id, output.get("status", "model_unavailable"))
        if output.get("spec"):
            self.database.audit(task_id, "model_decision", json.dumps({"model": getattr(self.provider, "model", os.getenv("DEEPSEEK_MODEL", "deepseek-flash")),
                "spec_hash": Harness.fingerprint(spec), "version": spec.version, "reactor": output["spec"].get("reactor_type"), "rationale": output["spec"].get("reactor_rationale")}, ensure_ascii=False))
        self.database.audit(task_id, "workflow_finished", f"Workflow status: {output.get('status', 'unknown')}")
        return output

    def validate_spec_json(self, spec_json: str) -> dict[str, Any]:
        spec = ReactorModelSpec.model_validate_json(spec_json)
        report = validate_spec(spec)
        return {"passed": report.passed, "errors": report.errors, "warnings": report.warnings}

    def execution_readiness(self, task_id: str) -> dict[str, Any]:
        """Read-only preflight for UI; never connects COM or confirms assumptions."""
        spec = self.database.get_spec(task_id)
        errors = list(dict.fromkeys([
            *validate_spec(spec).errors,
            *validate_reactor_choice(spec).errors,
            *Harness().authorize_execution(spec, self.database.audit_events(task_id)).errors,
        ]))
        return {"ready": not errors, "errors": errors,
                "questions": [item.model_dump() for item in spec.missing_information if item.blocking],
                "unconfirmed_assumptions": [item.model_dump() for item in spec.assumptions if not item.confirmed_by_user]}

    def resolve_input(self, task_id: str, clarification: str, confirmed_keys: list[str],
                      spec_json: str = "") -> dict[str, Any]:
        current = self.database.get_spec(task_id)
        spec = ReactorModelSpec.model_validate_json(spec_json) if spec_json else current.model_copy(deep=True)
        if confirmed_keys and (spec.task_id != current.task_id or spec.version != current.version):
            raise ValueError("Task version changed; reload the current assumptions before confirming.")
        known = {item.key: item for item in spec.assumptions}
        prior_assumptions = {item.key: item for item in current.assumptions}
        for key in confirmed_keys:
            if key in known and (key not in prior_assumptions or known[key].value != prior_assumptions[key].value):
                raise ValueError("Assumption value changed in the editor; save and review it before confirming.")
        if set(confirmed_keys) - set(known):
            raise ValueError("Unknown assumption confirmation key.")
        for key in confirmed_keys:
            known[key].confirmed_by_user = True
        # A confirmed isomer-distribution assumption and the explicitly split
        # reaction stoichiometry are the source of truth for selectivity. Keep
        # the redundant structured field synchronized for validation/audit.
        distribution_confirmed = any(
            item.confirmed_by_user and any(token in item.key.casefold() for token in ("isomer", "distribution", "selectivity"))
            for item in spec.assumptions
        )
        if distribution_confirmed:
            selectivity = dict(spec.known_parameters.selectivity)
            for reaction in spec.reactions:
                groups: dict[str, list] = {}
                for product in reaction.products:
                    groups.setdefault(product.formula, []).append(product)
                for products in groups.values():
                    if len(products) > 1:
                        total = sum(product.coefficient for product in products)
                        if total > 0:
                            selectivity.update({product.component: product.coefficient / total for product in products})
            spec.known_parameters.selectivity = selectivity
        confirmations = [{"key": key, "value": known[key].value} for key in confirmed_keys]
        if confirmations:
            self.database.audit(task_id, "user_assumptions_confirmed", json.dumps(confirmations, ensure_ascii=False))
        request = clarification.strip()
        if confirmations:
            request += "\n用户已通过界面逐项明确确认以下假设和值：" + json.dumps(confirmations, ensure_ascii=False)
        if request:
            request += ("\n请将确认条件落实到结构化数值字段（包括由年处理量和小时数推算实际进料流量），"
                        "复核并更新missing_information，删除已解决项；保留仍缺少的条件，勿添加未获确认的新假设。"
                        "allowed_species应与已有组分采用相同名称，避免同一物种重复命名。")
        return self.process_request(task_id, request=request, spec_json=spec.model_dump_json())

    def queue_execution(self, task_id: str) -> str:
        spec = self.database.get_spec(task_id)
        validation = validate_spec(spec)
        if not validation.passed:
            raise ValueError("Model specification failed validation: " + "; ".join(validation.errors))
        from reactor_agent.hysys.worker import HysysWorker
        worker = HysysWorker(timeout_seconds=30)
        try:
            capabilities = worker.inspect()
        finally:
            worker.stop()
        backend_report = validate_reactor_choice(spec, set(capabilities.supported_reactors))
        if not backend_report.passed:
            raise ValueError("Backend capability gate: " + "; ".join(backend_report.errors))
        backend_spec = Harness().authorize_execution(spec, self.database.audit_events(task_id))
        if not backend_spec.passed:
            raise ValueError("Backend input gate: " + "; ".join(backend_spec.errors))
        queue_id = self.database.enqueue(task_id, {"spec_version": spec.version, "spec": json.loads(spec.model_dump_json())})
        self.database.set_status(task_id, "queued")
        return queue_id

    def backend_capabilities(self) -> dict[str, Any]:
        from reactor_agent.hysys.worker import HysysWorker
        worker = HysysWorker(timeout_seconds=30)
        try:
            return worker.inspect().model_dump(mode="json")
        finally:
            worker.stop()

    def run_one_queued(self, queue_id: str | None = None) -> dict[str, Any] | None:
        item = self.database.claim_next(queue_id)
        if item is None:
            return None
        heartbeat_stop = threading.Event()
        def heartbeat():
            while not heartbeat_stop.wait(10):
                self.database.heartbeat_queue_item(item["queue_id"])
        heartbeat_thread = threading.Thread(target=heartbeat, name="queue-lease", daemon=True)
        heartbeat_thread.start()
        payload = item["payload"]
        spec = ReactorModelSpec.model_validate(payload["spec"])
        run = SimulationResult(
            task_id=item["task_id"],
            spec_version=int(payload["spec_version"]),
            diagnostics=[],
        )
        worker = None
        if self.database.get_spec(spec.task_id).version == spec.version:
            self.database.set_status(spec.task_id, "running")
        try:
            from reactor_agent.hysys.worker import HysysWorker
            worker = HysysWorker()
            capabilities = worker.inspect()
            gate = validate_reactor_choice(spec, set(capabilities.supported_reactors))
            if not gate.passed:
                raise RuntimeError("Backend capability gate: " + "; ".join(gate.errors))
            def execute_model(queued_spec, attempt):
                self.database.audit(spec.task_id, "hysys_tool_call", f"run_model version={spec.version} attempt={attempt + 1}")
                return worker.run_model(queued_spec, self.data_dir / "cases" / f"{run.run_id}-attempt{attempt + 1}.hsc")

            conn = self.database.connect(check_same_thread=False)
            try:
                graph = build_workflow(provider=self.provider, connection=conn, executor=execute_model)
                outcome = graph.invoke({"execution_requested": True, "spec": spec.model_dump(mode="json"), "retry_count": 0},
                                       config={"configurable": {"thread_id": "run-" + item["queue_id"]}})
            finally:
                conn.close()
            if not outcome.get("result"):
                raise RuntimeError("; ".join(outcome.get("diagnostics", ["Execution graph returned no result."])))
            run = SimulationResult.model_validate(outcome["result"])
            status = outcome["status"]
            self.database.complete_queue_item(item["queue_id"], error=None if run.engineering_validation_passed else "; ".join(run.diagnostics))
            self.database.save_run(run.run_id, run.task_id, run.spec_version, status, run.model_dump(mode="json"))
            # A completed old version must not overwrite a newer specification's status.
            if self.database.get_spec(run.task_id).version == run.spec_version:
                self.database.set_status(run.task_id, status)
            artifact_dir = self.data_dir / "runs"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / f"{run.run_id}.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
            return {"status": status, "run": run.model_dump(mode="json")}
        except Exception as exc:
            run.diagnostics.append(str(exc))
            if self.database.get_spec(item["task_id"]).version == run.spec_version:
                self.database.set_status(item["task_id"], "failed")
            self.database.complete_queue_item(item["queue_id"], error=str(exc))
            self.database.save_run(run.run_id, run.task_id, run.spec_version, "failed", run.model_dump(mode="json"))
            return {"status": "failed", "run": run.model_dump(mode="json")}
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            if worker:
                worker.stop()
