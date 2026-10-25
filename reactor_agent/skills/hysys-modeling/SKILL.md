---
name: hysys-reactor-modeling
version: 1.0.0
---

# HYSYS Reactor Modeling Skill

1. Convert the user's request to `ReactorModelSpec`; preserve units and composition basis.
2. Ask for missing feed composition, flow, state, pressure, reaction equations, and model data. Never invent equilibrium constants, kinetics, selectivity, isomer ratios, or simulation values.
3. Ask the model to choose a reactor from reaction characteristics, available data, and the domain rules. Record its rationale. Deterministic validation may reject a choice but must never silently replace it.
4. Validate elemental balance, mole fractions, units, required model data, assumptions, and backend capability before queueing work.
5. Require user confirmation for any proposed assumption. A user edit creates a new specification version; a new task begins with a clean specification.
6. Use only allowlisted MCP domain tools. Never request arbitrary COM objects, Python execution, or unrestricted HYSYS scripts.
7. Report case creation, solver convergence, and engineering validation as separate states. Only report values read from the current HYSYS case and linked specification version.
8. On failure, preserve diagnostics and the case path when available. Retry only classified retryable operations and stop at the configured bound.



## Verified V15 backend restrictions (2026-10-25)
- The current restricted adapter supports vapor-phase conversion reactors and Gibbs reactors with Peng-Robinson.
- These are backend capabilities, not rules forcing a reactor choice. Select from reaction physics and available data; unsupported choices must be diagnosed.
- Every execution requires explicit target pressure and thermal_mode (specified_temperature with target_outlet_temperature, or adiabatic). Never silently assume no pressure drop or isothermal operation.
- Each conversion reaction needs phase and basis_component; separate conversions are required for multiple reactions.
- Gibbs may use the HYSYS V15 pure-component thermodynamic databank instead of user-provided equilibrium constants, provided the user explicitly confirms the source and complete species universe. Record thermo.thermodynamic_data_source. Declare exclusion of solids or other possible products; do not claim universal equilibrium validity.
- For aromatic isomers, product stoichiometric coefficients and declared selectivities must agree. Missing selectivity must trigger clarification. Example-file ratios are test data and not a user-approved process assumption.
- Propose an annual methane throughput and hours only as an unconfirmed assumption, calculate flow consistently, and ask for confirmation.

- Gibbs inputs may provide allowed_species (name + formula) with no reactions. Do not demand reaction equations when the species universe and thermodynamic source are already complete and confirmed.
- Multiple conversion reactions are outside the verified adapter capability. Diagnose rather than silently choosing reaction order or extending single-reaction conversion semantics.

- If a user's confirmed throughput refers to one component, preserve that branch rate in feed.component_mass_flows_kg_h. Set feed.flow=null when total mixed flow should be derived from the component constraint and composition by HYSYS molecular weights. Never put a component's molar flow into the total mixed-feed flow field. Example principle: annual component mass / operating hours constrains that component's hourly mass rate, not the total mixture.
- Use Chinese clarification questions for Chinese requests. Once the user confirms an assumption, populate the corresponding numeric fields and remove resolved missing_information; keep unresolved questions blocking. A specified outlet temperature is an imposed outlet-temperature boundary, not proof that inlet and outlet are at the same temperature.
