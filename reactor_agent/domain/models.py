from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class ReactorType(StrEnum):
    CONVERSION = "conversion"
    EQUILIBRIUM = "equilibrium"
    GIBBS = "gibbs"
    CSTR = "cstr"
    PFR = "pfr"


class RunStatus(StrEnum):
    NEEDS_INPUT = "needs_input"
    SPEC_VALIDATED = "spec_validated"
    READY = "ready"
    MODEL_UNAVAILABLE = "model_unavailable"
    CASE_CREATED = "case_created"
    SOLVED = "solved"
    VALIDATION_FAILED = "validation_failed"
    FAILED = "failed"


class ChemicalSpecies(StrictModel):
    name: str = Field(min_length=1)
    formula: str = Field(min_length=1, description="Element formula, e.g. C2H6O")


class ComponentFraction(ChemicalSpecies):
    value: float = Field(ge=0, le=1, description="Normalized composition fraction, not a component flow rate")


class FeedStream(StrictModel):
    name: str = "Feed"
    components: list[ComponentFraction] = Field(min_length=1)
    composition_basis: Literal["mole_fraction", "mass_fraction"] = "mole_fraction"
    flow: float | None = Field(default=None, gt=0)
    flow_unit: str = "kmol/h"
    flow_basis: Literal["molar", "mass"] = "molar"
    component_mass_flows_kg_h: dict[str, float] = Field(default_factory=dict,
        description="Explicit component feed-rate constraints, e.g. annual throughput / operating hours; total flow may be derived from these and composition using simulator molecular weights")
    temperature: float | None = None
    temperature_unit: str = "degC"
    pressure: float | None = Field(default=None, gt=0)
    pressure_unit: str = "bar"

    @field_validator("component_mass_flows_kg_h")
    @classmethod
    def positive_component_flows(cls, value: dict[str, float]) -> dict[str, float]:
        import math
        if any(not math.isfinite(flow) or flow <= 0 for flow in value.values()):
            raise ValueError("Component mass-flow constraints must be finite and positive")
        return value

class ReactionTerm(StrictModel):
    component: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    coefficient: float = Field(gt=0)


class ReactionSpec(StrictModel):
    name: str | None = None
    reactants: list[ReactionTerm] = Field(min_length=1)
    products: list[ReactionTerm] = Field(min_length=1)
    phase: str | None = Field(default=None, description="Reaction phase; use vapor for an explicitly stated gas phase")
    basis_component: str | None = None
    conversion: float | None = Field(default=None, ge=0, le=1)

    @field_validator("phase", mode="before")
    @classmethod
    def normalize_explicit_phase(cls, value):
        if isinstance(value, str) and value.strip().casefold() in {"气相", "气体", "vapor", "vapour", "gas"}:
            return "vapor"
        return value


class KnownParameters(StrictModel):
    thermal_mode: str | None = Field(default=None, pattern="^(specified_temperature|adiabatic)$")
    conversion: float | None = Field(default=None, ge=0, le=1)
    selectivity: dict[str, float] = Field(default_factory=dict)
    equilibrium_constant: float | None = Field(default=None, gt=0)
    gibbs_energy_kj_per_mol: float | None = None
    kinetic_model: dict[str, Any] | None = None
    reactor_volume_m3: float | None = Field(default=None, gt=0)
    target_outlet_temperature: float | None = None
    target_outlet_temperature_unit: str = "degC"
    target_pressure: float | None = Field(default=None, gt=0)
    target_pressure_unit: str = "bar"

    @field_validator("selectivity")
    @classmethod
    def selectivity_range(cls, value: dict[str, float]) -> dict[str, float]:
        if any(fraction < 0 or fraction > 1 for fraction in value.values()):
            raise ValueError("selectivity values must be between 0 and 1")
        return value


class ThermoSpec(StrictModel):
    property_package: str | None = None
    rationale: str | None = None
    thermodynamic_data_source: str | None = None


class Assumption(StrictModel):
    key: str
    value: str
    rationale: str
    confirmed_by_user: bool = False
    impact: str | None = None


class MissingInformation(StrictModel):
    field: str
    reason: str
    question: str
    blocking: bool = True


class OperatingCase(StrictModel):
    """One steady-state operating point sharing the parent model specification."""

    name: str = Field(min_length=1)
    feed_temperature: float | None = None
    feed_temperature_unit: str = "degC"
    feed_pressure: float | None = Field(default=None, gt=0)
    feed_pressure_unit: str = "bar"
    outlet_temperature: float | None = None
    outlet_temperature_unit: str = "degC"
    pressure: float | None = Field(default=None, gt=0)
    pressure_unit: str = "bar"


class ReactorModelSpec(StrictModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    version: int = Field(default=1, ge=1)
    user_request: str = ""
    feed: FeedStream | None = None
    reactions: list[ReactionSpec] = Field(default_factory=list)
    allowed_species: list[ChemicalSpecies] = Field(default_factory=list, description="Explicit Gibbs species universe; no reaction equations required")
    reactor_type: ReactorType | None = None
    reactor_rationale: str | None = None
    thermo: ThermoSpec = Field(default_factory=ThermoSpec)
    known_parameters: KnownParameters = Field(default_factory=KnownParameters)
    operating_cases: list[OperatingCase] = Field(default_factory=list,
        description="Multiple steady-state cases to run sequentially with shared model inputs")
    assumptions: list[Assumption] = Field(default_factory=list)
    missing_information: list[MissingInformation] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    parent_version: int | None = None

    def next_version(self, **changes: Any) -> ReactorModelSpec:
        payload = self.model_dump()
        payload.update(changes)
        payload["version"] = self.version + 1
        payload["parent_version"] = self.version
        return ReactorModelSpec.model_validate(payload)


class BackendCapabilities(StrictModel):
    backend: str
    detected: bool
    version: str | None = None
    supported_reactors: list[ReactorType] = Field(default_factory=list)
    operations: dict[str, bool] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class StreamResult(StrictModel):
    name: str
    temperature_k: float | None = None
    pressure_kpa: float | None = None
    molar_flow_kmol_h: float | None = None
    mole_fractions: dict[str, float] = Field(default_factory=dict)


class SimulationResult(StrictModel):
    task_id: str
    spec_version: int
    case_name: str | None = None
    case_index: int | None = None
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    case_created: bool = False
    solver_converged: bool = False
    engineering_validation_passed: bool = False
    hysys_version: str | None = None
    case_path: str | None = None
    streams: list[StreamResult] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    raw_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolError(StrictModel):
    category: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
