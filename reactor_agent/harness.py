"""Deterministic execution policy; no LLM or COM access here."""
from __future__ import annotations
import hashlib
import json
from reactor_agent.domain.models import ReactorModelSpec
from reactor_agent.domain.validation import validate_backend_spec


class Harness:
    @staticmethod
    def fingerprint(spec: ReactorModelSpec) -> str:
        return hashlib.sha256(spec.model_dump_json().encode("utf-8")).hexdigest()

    def authorize_execution(self, spec, audit_events):
        report = validate_backend_spec(spec)
        decision_verified = False
        for event in audit_events:
            if event["event_type"] == "model_decision":
                try:
                    decision = json.loads(event["summary"])
                except (ValueError, TypeError):
                    continue
                if decision.get("spec_hash") == self.fingerprint(spec):
                    decision_verified = True
        if not decision_verified:
            report.errors.append("This exact specification version needs a model reactor decision before execution.")
        return report
