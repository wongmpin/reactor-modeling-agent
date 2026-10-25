from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from reactor_agent.domain.models import BackendCapabilities, ReactorModelSpec, ReactorType, StreamResult, SimulationResult
from reactor_agent.domain.units import convert_flow, convert_mass_flow, convert_temperature, convert_pressure


class HysysAdapterError(RuntimeError):
    def __init__(self, category: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class HysysComAdapter:
    """COM calls are confined to the calling STA thread and to this allowlisted adapter."""

    def __init__(self, progid: str | None = None, visible: bool | None = None):
        self.progid = progid or os.getenv("HYSYS_PROGID", "HYSYS.Application")
        self.visible = visible if visible is not None else os.getenv("HYSYS_VISIBLE", "false").lower() == "true"
        self._thread_id: int | None = None
        self._app: Any = None

    def _connect(self) -> Any:
        if platform.system() != "Windows":
            raise HysysAdapterError("backend_unavailable", "HYSYS COM automation requires Windows.")
        if self._thread_id is None:
            self._thread_id = threading.get_ident()
        elif self._thread_id != threading.get_ident():
            raise HysysAdapterError("thread_violation", "HYSYS COM calls must stay on the adapter STA thread.")
        if self._app is None:
            try:
                import pythoncom
                import win32com.client

                pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
                self._app = win32com.client.Dispatch(self.progid)
                self._app.Visible = self.visible
            except Exception as exc:
                raise HysysAdapterError("connection_failed", f"Could not connect to {self.progid}: {exc}", True) from exc
        return self._app

    def inspect(self) -> BackendCapabilities:
        try:
            app = self._connect()
            version = str(app.Version)
            case_count = int(app.SimulationCases.Count)
            return BackendCapabilities(
                backend="Aspen HYSYS COM",
                detected=True,
                version=version,
                supported_reactors=[ReactorType.CONVERSION, ReactorType.GIBBS] if "Version 15" in version else [],
                operations={
                    "connect": True,
                    "read_version": True,
                    "create_empty_case": True,
                    "save_case_copy": True,
                    "read_existing_material_stream": True,
                    "configure_feed": True,
                    "configure_reaction": True,
                    "configure_reactor": True,
                    "confirmed_solver_result": True,
                },
                limitations=[
                    f"Detected {case_count} currently open case(s).",
                    "V15 conversion and Gibbs automation verified locally on 2026-10-25; PR package only.",
                    "Equilibrium/CSTR/PFR remain unavailable; selection is never silently changed.",
                    "Conversion: specified phase and basis per reaction; thermal and pressure boundary required.",
                    "Gibbs: species restricted to specification; no solids/electrolytes validated.",
                    "Installed sample cases can be copied/read; they are not agent-generated simulation evidence.",
                ],
            )
        except HysysAdapterError:
            raise
        except Exception as exc:
            raise HysysAdapterError("connection_failed", f"HYSYS capability probe failed: {exc}", True) from exc

    def create_empty_case(self, destination: str) -> dict[str, Any]:
        """Creates and saves an empty HYSYS case; it does not claim a reactor model was built."""
        app = self._connect()
        path = Path(destination).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() != ".hsc":
            raise HysysAdapterError("invalid_input", "HYSYS case paths must use the .hsc extension.")
        try:
            case = app.SimulationCases.Add()
            case.SaveAs(str(path))
            title = str(case.Title)
            case.Close()
            return {"case_created": True, "case_path": str(path), "case_title": title, "model_built": False}
        except Exception as exc:
            raise HysysAdapterError("case_creation_failed", f"HYSYS could not create/save a case: {exc}", True) from exc

    def copy_case(self, source: str, destination: str) -> dict[str, Any]:
        src, dst = Path(source).resolve(), Path(destination).resolve()
        if not src.is_file() or src.suffix.lower() != ".hsc" or dst.suffix.lower() != ".hsc":
            raise HysysAdapterError("invalid_input", "Source and destination must be HYSYS .hsc files.")
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            raise HysysAdapterError("case_copy_failed", str(exc), True) from exc
        return {"case_created": True, "case_path": str(dst), "source_path": str(src), "model_built": False}

    def read_open_case_streams(self, case_index: int = 0) -> list[StreamResult]:
        app = self._connect()
        try:
            case = app.SimulationCases.Item(case_index)
            streams: list[StreamResult] = []
            for index in range(int(case.Flowsheet.MaterialStreams.Count)):
                stream = case.Flowsheet.MaterialStreams.Item(index)
                z_values = tuple(stream.ComponentMolarFractionValue)
                names = tuple(case.Flowsheet.FluidPackage.Components.Names)
                fractions = {name: float(value) for name, value in zip(names, z_values) if float(value) > 1e-12}
                streams.append(StreamResult(
                    name=str(stream.name),
                    temperature_k=float(stream.Temperature.GetValue("K")) if stream.Temperature.IsKnown else None,
                    pressure_kpa=float(stream.Pressure.GetValue("kPa")) if stream.Pressure.IsKnown else None,
                    molar_flow_kmol_h=float(stream.MolarFlow.GetValue("kgmole/h")) if stream.MolarFlow.IsKnown else None,
                    mole_fractions=fractions,
                ))
            return streams
        except Exception as exc:
            raise HysysAdapterError("result_read_failed", f"Could not read stream results: {exc}") from exc

    @staticmethod
    def _component_id(name: str) -> str:
        # Chemical aliases only: never scenario names or reactor selection rules.
        aliases = {"water": "H2O", "steam": "H2O", "水": "H2O", "carbon monoxide": "CO",
                   "carbon dioxide": "CO2", "h2": "Hydrogen", "ch4": "Methane",
                   "甲烷": "Methane", "氢气": "Hydrogen", "氢": "Hydrogen",
                   "一氧化碳": "CO", "二氧化碳": "CO2",
                   "toluene": "Toluene", "甲苯": "Toluene",
                   "benzene": "Benzene", "苯": "Benzene",
                   "o-xylene": "o-Xylene", "邻二甲苯": "o-Xylene", "邻二甲苯（o-xylene）": "o-Xylene",
                   "m-xylene": "m-Xylene", "间二甲苯": "m-Xylene", "间二甲苯（m-xylene）": "m-Xylene",
                   "p-xylene": "p-Xylene", "对二甲苯": "p-Xylene", "对二甲苯（p-xylene）": "p-Xylene"}
        if name.strip().casefold() in {"二甲苯", "xylenes", "xylene isomers"}:
            raise HysysAdapterError("ambiguous_component",
                "“二甲苯”是异构体总称，HYSYS需要邻二甲苯、间二甲苯、对二甲苯具体组分；请先确认选择性分布。")
        return aliases.get(name.strip().casefold(), name)

    def run_model(self, spec: ReactorModelSpec, destination: str) -> SimulationResult:
        from reactor_agent.domain.validation import validate_spec, validate_reactor_choice, validate_backend_spec
        report = validate_spec(spec)
        report.errors.extend(validate_reactor_choice(spec, {ReactorType.CONVERSION, ReactorType.GIBBS}).errors)
        report.errors.extend(validate_backend_spec(spec).errors)
        if not report.passed:
            raise HysysAdapterError("invalid_input", "; ".join(report.errors))
        app = self._connect()
        if "Version 15" not in str(app.Version):
            raise HysysAdapterError("unsupported_version", "Only HYSYS V15 is verified.")
        path = Path(destination).resolve()
        if path.suffix.lower() != ".hsc" or path.exists():
            raise HysysAdapterError("invalid_path", "Use a new, independent .hsc destination.")
        path.parent.mkdir(parents=True, exist_ok=True)
        run = SimulationResult(task_id=spec.task_id, spec_version=spec.version,
                               hysys_version=str(app.Version), case_path=str(path))
        case = None
        stage = "create_case"
        try:
            case = app.SimulationCases.Add()
            stage = "configure_basis"
            basis = case.BasisManager
            cl = basis.ComponentLists.Add("Agent Components")
            original_names = list(dict.fromkeys(
                [item.name for item in spec.feed.components] + [item.name for item in spec.allowed_species] +
                [term.component for reaction in spec.reactions for term in [*reaction.reactants, *reaction.products]]))
            ids = {name: self._component_id(name) for name in original_names}
            if len({item.casefold() for item in ids.values()}) != len(ids):
                raise HysysAdapterError("ambiguous_component", "Multiple input names identify the same HYSYS component.")
            for component_id in ids.values():
                cl.Components.Add(component_id)
            fp = basis.FluidPackages.Add("Agent PR")
            fp.ComponentList = cl
            fp.PropertyPackageName = "PengRob"
            from reactor_agent.domain.validation import parse_formula
            declared_formulas = {item.name: item.formula for item in spec.feed.components}
            declared_formulas.update({item.name: item.formula for item in spec.allowed_species})
            declared_formulas.update({term.component: term.formula for reaction in spec.reactions for term in [*reaction.reactants, *reaction.products]})
            actual_formulas = {}
            molecular_weights = {}
            for name, component_id in ids.items():
                actual = str(fp.Components.Item(component_id).Formula).strip()
                actual_formulas[name] = actual
                molecular_weights[name] = float(fp.Components.Item(component_id).MolecularWeightValue)
                if parse_formula(actual) != parse_formula(declared_formulas[name]):
                    raise HysysAdapterError("component_formula_mismatch", f"HYSYS formula for {name} is {actual}; declared {declared_formulas[name]}.")
            reaction_set = None
            if spec.reactor_type == ReactorType.CONVERSION:
                stage = "configure_reactions"
                rpm = basis.ReactionPackageManager
                reaction_set = rpm.ReactionSets.Add("Agent Reactions")
                for index, reaction in enumerate(spec.reactions):
                    rxn = rpm.Reactions.Add(f"Reaction {index + 1}", "ConversionRxn")
                    for sign, terms in ((-1, reaction.reactants), (1, reaction.products)):
                        for term in terms:
                            reactant = rxn.Reactants.Add(ids[term.component])
                            reactant.StoichiometricCoefficientValue = sign * term.coefficient
                    rxn.BaseComponent = cl.Components.Item(ids[reaction.basis_component])
                    rxn.ReactionPhase = 0  # V15 ptVapourPhase; only this phase has been verified.
                    conversion = reaction.conversion if reaction.conversion is not None else spec.known_parameters.conversion
                    rxn.Conversion = conversion * 100.0
                    reaction_set.ActiveReactions.Add(rxn)
                reaction_set.AssociateFluidPackage(fp)
            # A new case starts in basis mode. Leaving basis makes stream variables writable.
            basis.EndBasisChange()
            stage = "configure_streams"
            fs = case.Flowsheet
            feed = fs.MaterialStreams.Add("Feed")
            vap = fs.MaterialStreams.Add("Vapour Product")
            liq = fs.MaterialStreams.Add("Liquid Product")
            energy = fs.EnergyStreams.Add("Reactor Duty")
            actual_ids = list(fp.Components.Names)
            fraction_by_id = {ids[item.name]: item.value for item in spec.feed.components}
            fractions = [fraction_by_id.get(name, 0.0) for name in actual_ids]
            if spec.feed.composition_basis == "mass_fraction":
                feed.ComponentMassFractionValue = tuple(fractions)
            else:
                feed.ComponentMolarFractionValue = tuple(fractions)
            feed.Temperature.SetValue(convert_temperature(spec.feed.temperature, spec.feed.temperature_unit), "K")
            inlet_pressure = convert_pressure(spec.feed.pressure, spec.feed.pressure_unit)
            feed.Pressure.SetValue(inlet_pressure, "kPa")
            if spec.feed.flow is not None:
                if spec.feed.flow_basis == "mass":
                    feed.MassFlow.SetValue(convert_mass_flow(spec.feed.flow, spec.feed.flow_unit), "kg/h")
                else:
                    feed.MolarFlow.SetValue(convert_flow(spec.feed.flow, spec.feed.flow_unit), "kgmole/h")
            else:
                derived_totals = []
                composition = {item.name: item.value for item in spec.feed.components}
                for name, mass_rate in spec.feed.component_mass_flows_kg_h.items():
                    if spec.feed.composition_basis == "mole_fraction":
                        derived_totals.append(mass_rate / molecular_weights[name] / composition[name])
                    else:
                        derived_totals.append(mass_rate / composition[name])
                if any(abs(value - derived_totals[0]) > max(abs(value), 1) * 1e-5 for value in derived_totals):
                    raise HysysAdapterError("inconsistent_component_flows", "Component flow constraints disagree with feed composition.")
                variable, unit = ((feed.MolarFlow, "kgmole/h") if spec.feed.composition_basis == "mole_fraction"
                                  else (feed.MassFlow, "kg/h"))
                variable.SetValue(derived_totals[0], unit)
            stage = "configure_reactor"
            op_type = {ReactorType.CONVERSION: "ConversionReactorOp", ReactorType.GIBBS: "GibbsReactorOp"}[spec.reactor_type]
            op = fs.Operations.Add("Agent Reactor", op_type)
            op.Feeds.Add(feed)
            op.VapourProduct = vap
            op.LiquidProduct = liq
            op.EnergyStream = energy
            if reaction_set is not None:
                op.ReactionSet = reaction_set
                op.ActAsSeparatorWhenCannotSolve = False
            else:
                op.ReactorType = 3  # gr_GibbsRxnsOnly: minimize Gibbs energy over specified species.
            kp = spec.known_parameters
            outlet_pressure = convert_pressure(kp.target_pressure, kp.target_pressure_unit)
            op.PressureDrop.SetValue(inlet_pressure - outlet_pressure, "kPa")
            if kp.thermal_mode == "adiabatic":
                energy.HeatFlow.SetValue(0, "kW")
            else:
                vap.Temperature.SetValue(convert_temperature(kp.target_outlet_temperature, kp.target_outlet_temperature_unit), "K")
            stage = "solve"
            deadline = time.monotonic() + 60
            import pythoncom
            while case.Solver.IsSolving and time.monotonic() < deadline:
                pythoncom.PumpWaitingMessages()
                time.sleep(0.05)
            known_outputs = all(stream.MolarFlow.IsKnown and stream.Temperature.IsKnown and stream.Pressure.IsKnown
                                for stream in (vap, liq))
            inconsistent = any(variable.IsInconsistent for stream in (feed, vap, liq)
                               for variable in (stream.Temperature, stream.Pressure, stream.MolarFlow))
            run.solver_converged = int(op.CalcLevel) == 500 and known_outputs and not case.Solver.IsSolving and not inconsistent
            reverse_ids = {value: key for key, value in ids.items()}
            for stream in (feed, vap, liq):
                flow = float(stream.MolarFlow.GetValue("kgmole/h")) if stream.MolarFlow.IsKnown else None
                # Zero-flow phases have undefined fractions; preserve flow and omit their composition.
                z = tuple(stream.ComponentMolarFractionValue) if flow is not None and flow > 1e-10 else ()
                run.streams.append(StreamResult(
                    name=str(stream.name),
                    temperature_k=float(stream.Temperature.GetValue("K")) if stream.Temperature.IsKnown else None,
                    pressure_kpa=float(stream.Pressure.GetValue("kPa")) if stream.Pressure.IsKnown else None,
                    molar_flow_kmol_h=flow,
                    mole_fractions={reverse_ids[name]: float(value) for name, value in zip(actual_ids, z)},
                ))
            run.raw_summary = {"operation_type": str(op.TypeName), "calc_level": int(op.CalcLevel),
                "is_solving": bool(case.Solver.IsSolving), "outputs_known": known_outputs,
                "variables_inconsistent": inconsistent, "property_package": str(fp.PropertyPackageName),
                "component_mapping": ids, "hysys_component_formulas": actual_formulas, "hysys_molecular_weights_kg_kmol": molecular_weights, "feed_mass_flow_kg_h": float(feed.MassFlow.GetValue("kg/h")), "duty_kw": float(energy.HeatFlow.GetValue("kW")) if energy.HeatFlow.IsKnown else None,
                "thermodynamic_data_source": spec.thermo.thermodynamic_data_source}
            if spec.reactor_type == ReactorType.CONVERSION:
                run.raw_summary["hysys_conversion_percent"] = list(op.RxnPercentConversionValue)
            stage = "save_case"
            case.SaveAs(str(path))
            run.case_created = path.is_file() and path.stat().st_size > 0
            if not run.solver_converged:
                run.diagnostics.append("HYSYS did not satisfy calculated-level, known-output and consistency checks.")
        except Exception as exc:
            run.diagnostics.append(f"{stage}: {type(exc).__name__}: {exc}")
            if case is not None:
                try:
                    case.SaveAs(str(path))
                    run.case_created = path.is_file()
                except Exception:
                    pass
            run.raw_summary["failure_stage"] = stage
        finally:
            if case is not None:
                case.Close()
        return run

    def verify_saved_case(self, source: str) -> dict[str, Any]:
        app = self._connect()
        case = app.SimulationCases.Open(str(Path(source).resolve()))
        try:
            op = case.Flowsheet.Operations.Item("Agent Reactor")
            product = case.Flowsheet.MaterialStreams.Item("Vapour Product")
            return {"reopened": True, "calc_level": int(op.CalcLevel),
                    "product_flow_kmol_h": float(product.MolarFlow.GetValue("kgmole/h")),
                    "hysys_version": str(app.Version)}
        finally:
            case.Close()

    def close_thread(self) -> None:
        if self._app is not None:
            self._app = None
            import pythoncom
            pythoncom.CoUninitialize()
