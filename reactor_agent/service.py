from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from reactor_agent.agent.graph import build_workflow, invoke_workflow
from reactor_agent.domain.models import ChemicalSpecies, ReactorModelSpec, SimulationResult, ReactionTerm
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

    def delete_task(self, task_id: str) -> None:
        self.database.delete_task(task_id)

    @staticmethod
    def _normalize_feed_constraints(spec: ReactorModelSpec) -> ReactorModelSpec:
        """Align deterministic feed constraints with the specification's component names."""
        if spec.feed is None or not spec.feed.component_mass_flows_kg_h:
            return spec
        by_name = {component.name.casefold(): component.name for component in spec.feed.components}
        by_formula = {component.formula.casefold(): component.name for component in spec.feed.components}
        aliases = {"ch4": "甲烷", "methane": "甲烷", "h2o": "水", "water": "水", "steam": "水"}
        normalized: dict[str, float] = {}
        changed = False
        for key, flow in spec.feed.component_mass_flows_kg_h.items():
            alias = aliases.get(key.casefold())
            canonical = (by_name.get(key.casefold()) or by_formula.get(key.casefold()) or
                         (by_name.get(alias.casefold()) if alias else None) or key)
            normalized[canonical] = normalized.get(canonical, 0.0) + flow
            changed = changed or canonical != key
        if not changed:
            return spec
        feed = spec.feed.model_copy(update={"component_mass_flows_kg_h": normalized})
        return spec.model_copy(update={"feed": feed})

    @staticmethod
    def _apply_explicit_feed_pressure(spec: ReactorModelSpec, request: str) -> ReactorModelSpec:
        """Persist a pressure only when the user explicitly states/ confirms it."""
        if spec.feed is None or spec.feed.pressure is not None or not request:
            return spec
        text = request.casefold()
        pressure_context = any(token in text for token in ("进料压力", "feed pressure", "入口压力", "inlet pressure"))
        confirmation_context = any(token in text for token in ("确认", "采用", "设为", "设置为", "取", "等于", "忽略压降", "无压降"))
        if not (pressure_context and confirmation_context):
            return spec
        match = re.search(r"(?:进料|入口|feed|inlet)?\s*压力[^0-9]{0,20}(\d+(?:\.\d+)?)\s*(?:bar|bara|kpa|千帕|兆帕|mpa)?", text, re.I)
        if not match:
            return spec
        value = float(match.group(1))
        unit_match = re.search(r"\d+(?:\.\d+)?\s*(bar|bara|kpa|千帕|兆帕|mpa)", text, re.I)
        unit = (unit_match.group(1).lower() if unit_match else spec.feed.pressure_unit)
        unit = {"bara": "bar", "kpa": "kPa", "千帕": "kPa", "mpa": "MPa", "兆帕": "MPa"}.get(unit, unit)
        feed = spec.feed.model_copy(update={"pressure": value, "pressure_unit": unit})
        assumptions = list(spec.assumptions)
        existing = next((item for item in assumptions if item.key in {"feed_pressure", "进料压力"}), None)
        value_text = f"进料压力取 {value:g} {unit}；按用户确认的压降条件执行。"
        if existing:
            existing.value = value_text
            existing.confirmed_by_user = True
        else:
            from reactor_agent.domain.models import Assumption
            assumptions.append(Assumption(key="feed_pressure", value=value_text,
                                          rationale="用户在补充消息中明确确认进料压力。",
                                          confirmed_by_user=True,
                                          impact="用于设置 HYSYS 进料边界；出口压力仍按各工况边界执行。"))
        return spec.model_copy(update={"feed": feed, "assumptions": assumptions})

    @staticmethod
    def _apply_confirmed_xylene_distribution(spec: ReactorModelSpec) -> ReactorModelSpec:
        """Expand a lumped xylene product only when the user confirmed an equal split."""
        confirmed_equal_split = any(
            assumption.confirmed_by_user
            and "二甲苯" in assumption.key
            and any(marker in assumption.value for marker in ("等摩尔", "1:1:1", "各占1/3"))
            for assumption in spec.assumptions
        )
        if not confirmed_equal_split:
            return spec

        isomers = (("邻二甲苯", "C8H10"), ("间二甲苯", "C8H10"), ("对二甲苯", "C8H10"))
        changed = False
        for reaction in spec.reactions:
            products: list[ReactionTerm] = []
            xylene_total = 0.0
            for product in reaction.products:
                if product.component.strip().casefold() == "二甲苯" and product.formula == "C8H10":
                    xylene_total += product.coefficient
                    changed = True
                elif product.formula == "C8H10" and product.component in {name for name, _ in isomers}:
                    xylene_total += product.coefficient
                else:
                    products.append(product)
            if xylene_total:
                products.extend(ReactionTerm(component=name, formula=formula,
                                             coefficient=xylene_total / 3)
                                for name, formula in isomers)
                changed = True
            reaction.products = products
        if not changed:
            return spec
        # Keep the explicit Gibbs/adapter species universe consistent with the
        # reaction products; the lumped xylene name is not a valid HYSYS id.
        allowed_species: list[ChemicalSpecies] = []
        for species in spec.allowed_species:
            if species.formula == "C8H10" and species.name.strip().casefold() in {"二甲苯", "xylenes", "xylene isomers"}:
                allowed_species.extend(ChemicalSpecies(name=name, formula=formula) for name, formula in isomers)
            elif not (species.formula == "C8H10" and species.name in {name for name, _ in isomers}):
                allowed_species.append(species)
        spec.known_parameters.selectivity.update({name: 1 / 3 for name, _ in isomers})
        spec.allowed_species = allowed_species
        return spec

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
            # Persist the deterministic component-name normalization so the saved
            # specification, queue payload, and HYSYS adapter use one vocabulary.
            spec = self._normalize_feed_constraints(spec)
            spec = self._apply_explicit_feed_pressure(spec, request)
            spec = self._apply_confirmed_xylene_distribution(spec)
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
        normalized = self._normalize_feed_constraints(spec)
        questions = [item.model_dump() for item in spec.missing_information if item.blocking]
        if normalized.feed and normalized.feed.pressure is None:
            case_pressures = {case.pressure for case in normalized.operating_cases if case.pressure is not None}
            case_feed_pressures_complete = bool(normalized.operating_cases) and all(
                case.feed_pressure is not None for case in normalized.operating_cases)
            if (len(case_pressures) == 1 and not case_feed_pressures_complete
                    and not any(item["field"] == "feed.pressure" for item in questions)):
                pressure = next(iter(case_pressures))
                questions.append({"field": "feed.pressure", "reason": "进料压力未明确给出。",
                                  "question": f"请确认进料压力是否采用 {pressure:g} bar，并确认忽略压降？", "blocking": True})
        errors = list(dict.fromkeys([
            *validate_spec(normalized).errors,
            *validate_reactor_choice(spec).errors,
            *Harness().authorize_execution(spec, self.database.audit_events(task_id)).errors,
        ]))
        return {"ready": not errors, "errors": errors,
                "questions": questions,
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
        from reactor_agent.hysys.worker import HysysWorker
        worker = HysysWorker(timeout_seconds=30)
        try:
            capabilities = worker.inspect()
        finally:
            worker.stop()
        cases = spec.operating_cases or [None]
        base_backend_report = validate_reactor_choice(spec, set(capabilities.supported_reactors))
        if not base_backend_report.passed:
            raise ValueError("Backend capability gate: " + "; ".join(base_backend_report.errors))
        base_backend_spec = Harness().authorize_execution(spec, self.database.audit_events(task_id))
        if not base_backend_spec.passed:
            raise ValueError("Backend input gate: " + "; ".join(base_backend_spec.errors))
        queue_id = None
        for index, case in enumerate(cases):
            case_spec = self._normalize_feed_constraints(spec.model_copy(deep=True))
            case_name = None
            if case is not None:
                case_name = case.name
                updates = {}
                feed_updates = {}
                if case.feed_temperature is not None:
                    feed_updates["temperature"] = case.feed_temperature
                    feed_updates["temperature_unit"] = case.feed_temperature_unit
                if case.feed_pressure is not None:
                    feed_updates["pressure"] = case.feed_pressure
                    feed_updates["pressure_unit"] = case.feed_pressure_unit
                if feed_updates:
                    case_spec.feed = case_spec.feed.model_copy(update=feed_updates)
                if case.outlet_temperature is not None:
                    updates["target_outlet_temperature"] = case.outlet_temperature
                    updates["target_outlet_temperature_unit"] = case.outlet_temperature_unit
                if case.pressure is not None:
                    updates["target_pressure"] = case.pressure
                    updates["target_pressure_unit"] = case.pressure_unit
                case_spec.known_parameters = case_spec.known_parameters.model_copy(update=updates)
                case_spec.operating_cases = []
            validation = validate_spec(case_spec)
            if not validation.passed:
                raise ValueError(f"Model specification failed validation ({case_name or 'default'}): " + "; ".join(validation.errors))
            backend_report = validate_reactor_choice(case_spec, set(capabilities.supported_reactors))
            if not backend_report.passed:
                raise ValueError("Backend capability gate: " + "; ".join(backend_report.errors))
            payload = {"spec_version": spec.version, "case_name": case_name,
                       "case_index": index, "case_count": len(cases),
                       "spec": json.loads(case_spec.model_dump_json())}
            current_queue_id = self.database.enqueue(task_id, payload)
            queue_id = queue_id or current_queue_id
        self.database.set_status(task_id, "queued")
        return queue_id or ""

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
            case_name=payload.get("case_name"),
            case_index=payload.get("case_index"),
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
            run.case_name = payload.get("case_name")
            run.case_index = payload.get("case_index")
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
