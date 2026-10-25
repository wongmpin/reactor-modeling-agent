from __future__ import annotations

import os
import platform

import pytest

from reactor_agent.hysys.worker import HysysWorker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_HYSYS_INTEGRATION") != "1" or platform.system() != "Windows",
    reason="Set RUN_HYSYS_INTEGRATION=1 on the HYSYS Windows workstation.",
)


def test_hysys_v15_sta_worker_connects_and_saves_empty_case(tmp_path):
    worker = HysysWorker(timeout_seconds=60)
    try:
        capabilities = worker.inspect()
        assert capabilities.detected
        assert capabilities.version and "Version 15" in capabilities.version
        assert set(capabilities.supported_reactors) == {"conversion", "gibbs"}

        case_path = tmp_path / "worker-integration.hsc"
        result = worker.create_empty_case(case_path)
        assert result["case_created"] is True
        assert result["model_built"] is False
        assert case_path.is_file() and case_path.stat().st_size > 0
    finally:
        worker.stop()


@pytest.mark.parametrize("filename,reactor_type", [
    ("hydrogenation.json", "conversion"),
    ("toluene-test-data.json", "conversion"),
    ("reforming-710-test-data.json", "gibbs"),
    ("reforming-600-test-data.json", "gibbs"),
])
def test_real_reactor_solves_validates_and_reopens(tmp_path, filename, reactor_type):
    from pathlib import Path
    from reactor_agent.domain.models import ReactorModelSpec, ReactorType
    from reactor_agent.domain.validation import validate_engineering
    spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[2] / "examples" / filename).read_text(encoding="utf-8"))
    # Adapter integration test fixes the type to isolate COM; live model selection is tested separately.
    spec.reactor_type = ReactorType(reactor_type)
    spec.reactor_rationale = "Adapter-only integration test with explicit, reviewed test data."
    worker = HysysWorker(timeout_seconds=90)
    try:
        result = worker.run_model(spec, tmp_path / "reactor.hsc")
        assert result.case_created, result.diagnostics
        assert result.solver_converged, result.diagnostics
        report = validate_engineering(spec, result)
        assert report.passed, report.errors
        reopen = worker.verify_saved_case(result.case_path)
        assert reopen["reopened"] and reopen["calc_level"] == 500
    finally:
        worker.stop()


def test_unknown_component_failure_never_claims_convergence(tmp_path):
    from pathlib import Path
    from reactor_agent.domain.models import ReactorModelSpec, ReactorType
    spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[2] / "examples/hydrogenation.json").read_text(encoding="utf-8"))
    spec.feed.components[0].name = "NotARealHysysComponent"
    spec.reactions[0].reactants[0].component = "NotARealHysysComponent"
    spec.reactions[0].basis_component = "NotARealHysysComponent"
    spec.reactor_type = ReactorType.CONVERSION
    spec.reactor_rationale = "Deliberate integration failure"
    worker = HysysWorker(timeout_seconds=90)
    try:
        result = worker.run_model(spec, tmp_path / "failed.hsc")
        assert not result.solver_converged
        assert not result.engineering_validation_passed
        assert result.raw_summary["failure_stage"] == "configure_basis"
    finally:
        worker.stop()


def test_gibbs_accepts_species_universe_without_reaction_equations(tmp_path):
    from pathlib import Path
    from reactor_agent.domain.models import ReactorModelSpec, ReactorType, ChemicalSpecies
    from reactor_agent.domain.validation import validate_engineering
    spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[2] / "examples/reforming-710-test-data.json").read_text(encoding="utf-8"))
    spec.allowed_species = [ChemicalSpecies(name=term.component, formula=term.formula)
                            for name, term in {term.component: term for rxn in spec.reactions
                            for term in [*rxn.reactants, *rxn.products]}.items()]
    spec.reactions = []
    spec.reactor_type = ReactorType.GIBBS
    spec.reactor_rationale = "Unknown product distribution with explicit species and databank"
    worker = HysysWorker(timeout_seconds=90)
    try:
        result = worker.run_model(spec, tmp_path / "gibbs-species.hsc")
        assert result.solver_converged, result.diagnostics
        assert validate_engineering(spec, result).passed
    finally:
        worker.stop()


def test_hysys_derives_total_feed_from_component_throughput(tmp_path):
    from pathlib import Path
    from reactor_agent.domain.models import ReactorModelSpec, ReactorType
    from reactor_agent.domain.validation import validate_engineering
    spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[2] / "examples/reforming-710-test-data.json").read_text(encoding="utf-8"))
    spec.reactor_type = ReactorType.GIBBS
    spec.reactor_rationale = "Adapter integration test for explicit component throughput"
    spec.feed.flow = None
    spec.feed.component_mass_flows_kg_h = {"Methane": 12500.0}
    worker = HysysWorker(timeout_seconds=90)
    try:
        result = worker.run_model(spec, tmp_path / "component-feed.hsc")
        assert result.solver_converged, result.diagnostics
        assert validate_engineering(spec, result).passed
        incoming = result.streams[0]
        methane_mass = incoming.molar_flow_kmol_h * incoming.mole_fractions["Methane"] * result.raw_summary["hysys_molecular_weights_kg_kmol"]["Methane"]
        assert methane_mass == pytest.approx(12500)
        assert incoming.molar_flow_kmol_h > 2800
    finally:
        worker.stop()


def test_hysys_resolves_chinese_toluene_and_confirmed_xylene_isomers(tmp_path):
    from pathlib import Path
    from reactor_agent.domain.models import (Assumption, ReactionTerm, ReactorModelSpec, ReactorType)
    from reactor_agent.service import ReactorAgentService
    from reactor_agent.domain.validation import validate_engineering, validate_spec

    spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[2] / "examples" / "toluene-test-data.json").read_text(encoding="utf-8"))
    spec.reactor_type = ReactorType.CONVERSION
    spec.reactor_rationale = "Adapter integration test for explicitly confirmed Chinese component names."
    spec.feed.components[0].name = "甲苯"
    spec.reactions[0].reactants[0].component = "甲苯"
    spec.reactions[0].basis_component = "甲苯"
    spec.reactions[0].products = [
        ReactionTerm(component="苯", formula="C6H6", coefficient=1),
        ReactionTerm(component="二甲苯", formula="C8H10", coefficient=1),
    ]
    spec.known_parameters.selectivity = {}
    spec.assumptions = [Assumption(key="二甲苯异构体分布", value="邻、间、对二甲苯等摩尔分配",
        rationale="Explicitly confirmed integration-test assumption.", confirmed_by_user=True)]
    spec = ReactorAgentService._apply_confirmed_xylene_distribution(spec)
    assert validate_spec(spec).passed

    worker = HysysWorker(timeout_seconds=90)
    try:
        result = worker.run_model(spec, tmp_path / "chinese-toluene.hsc")
        assert result.case_created and result.solver_converged, result.diagnostics
        assert validate_engineering(spec, result).passed
        product = next(stream for stream in result.streams if stream.name == "Vapour Product")
        assert set(product.mole_fractions) == {"甲苯", "苯", "邻二甲苯", "间二甲苯", "对二甲苯"}
        assert result.raw_summary["component_mapping"] == {
            "甲苯": "Toluene", "苯": "Benzene", "邻二甲苯": "o-Xylene",
            "间二甲苯": "m-Xylene", "对二甲苯": "p-Xylene"}
    finally:
        worker.stop()
