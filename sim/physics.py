"""AHU lumped-parameter physics. Pure Python, no I/O — unit-testable without a PLC.

State + model constants in one dataclass; step() advances one dt second.
Modeling assumptions are documented in docs/design-notes.md — every constant is
a stated assumption, not measured data.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AhuParams:
    # Zone / airflow (lumped single zone)
    zone_volume_m3: float = 600.0
    supply_flow_m3s: float = 1.6         # ~5.8k m3/h design airflow
    air_rho_kgm3: float = 1.2
    cp_air_jkgk: float = 1005.0
    # Coil
    coil_drop_k_full: float = 24.0       # K at 100 % valve, design flow (sized for 90 % outdoor air at 35 C)
    coil_lag_s: float = 20.0             # coil + sensor time constant
    # Zone thermal mass (incl. fabric/furniture)
    zone_capacity_jk: float = 2.5e6
    design_load_kw_occupied: float = 21.0      # about 24 C zone at design airflow with 13 C supply
    design_load_kw_unoccupied: float = 11.0
    envelope_loss_wk: float = 30.0       # W/K to outdoor
    ra_lag_s: float = 60.0
    # Dampers
    f_fresh_closed: float = 0.15         # fresh-air fraction at dampers closed
    f_fresh_open: float = 0.90
    damper_travel_s: float = 10.0
    # Fans
    fan_runup_s: float = 2.0
    # CO2
    co2_outdoor_ppm: float = 420.0
    co2_gen_ppm_s_per_person: float = 0.0052  # per-person generation scaled to zone volume
    # Filter
    dp_base_pa: float = 80.0
    dp_clog_rate_pas: float = 0.0        # set >0 to run the clog test


@dataclass
class AhuInputs:  # PLC outputs, read by the simulator
    sf_cmd: bool = False                 # supply fan
    rf_cmd: bool = False                 # return fan
    fad_open: bool = False               # fresh-air damper
    exd_open: bool = False               # exhaust damper
    chwv_pct: float = 0.0                # CHW valve, 0-100
    chw_pump_cmd: bool = False


@dataclass
class AhuState:
    t_space_c: float = 24.0
    t_mix_c: float = 24.0
    t_sat_c: float = 24.0                # supply-air temp after coil (sensor)
    t_ra_c: float = 24.0
    co2_ppm: float = 450.0
    dp_filter_pa: float = 0.0
    dp_clog_pa: float = 0.0
    damper_pos: float = 0.0              # 0 closed .. 1 open
    fan_on: bool = False
    _coil_lag_c: float = 24.0
    _ra_lag_c: float = 24.0
    _co2_lag_ppm: float = 450.0
    history: list = field(default_factory=list)


class AhuPlant:
    def __init__(self, params: AhuParams = AhuParams(),
                 occupancy: int = 0, outdoor_c: float = 35.0):
        self.p = params
        self.s = AhuState()
        self.occ = occupancy
        self.outdoor_c = outdoor_c
        self._fan_timer_s = 0.0

    def step(self, u: AhuInputs, dt: float = 1.0,
             occupancy: int | None = None, outdoor_c: float | None = None) -> AhuState:
        p, s = self.p, self.s
        if occupancy is not None:
            self.occ = occupancy
        if outdoor_c is not None:
            self.outdoor_c = outdoor_c

        # Fan: commanded, then run-up delay before air moves
        self._fan_timer_s = self._fan_timer_s + dt if u.sf_cmd else 0.0
        s.fan_on = u.sf_cmd and self._fan_timer_s >= p.fan_runup_s
        m_dot_kgs = p.air_rho_kgm3 * p.supply_flow_m3s * s.fan_on

        # Damper travels toward command at fixed rate
        target = 1.0 if u.fad_open else 0.0
        step = dt / p.damper_travel_s
        if target > s.damper_pos:
            s.damper_pos = min(target, s.damper_pos + step)
        elif target < s.damper_pos:
            s.damper_pos = max(target, s.damper_pos - step)
        f_fresh = p.f_fresh_closed + (p.f_fresh_open - p.f_fresh_closed) * s.damper_pos

        if not s.fan_on:
            self._step_no_flow(dt)
        else:
            self._step_flow(u, m_dot_kgs, f_fresh, dt)

        # Filter differential pressure only exists while air is moving
        s.dp_clog_pa += p.dp_clog_rate_pas * dt * s.fan_on
        s.dp_filter_pa = p.dp_base_pa + s.dp_clog_pa if s.fan_on else 0.0

        self._record()
        return s

    # -- no airflow: zone floats, coil sensor decays, CO2 frozen (space unoccupied)
    def _step_no_flow(self, dt: float):
        p, s = self.p, self.s
        load_k = p.design_load_kw_unoccupied
        dtdt = (load_k * 1e3 - p.envelope_loss_wk * (s.t_space_c - self.outdoor_c)) / p.zone_capacity_jk
        s.t_space_c += dtdt * dt
        s._coil_lag_c += (s.t_space_c - s._coil_lag_c) * dt / (2.0 * p.coil_lag_s)
        s.t_sat_c = s._coil_lag_c
        s.t_mix_c = s._coil_lag_c
        s.t_ra_c = s._ra_lag_c

    # -- normal flow path
    def _step_flow(self, u: AhuInputs, m_dot_kgs: float, f_fresh: float, dt: float):
        p, s = self.p, self.s

        # Mixing box
        s.t_mix_c = f_fresh * self.outdoor_c + (1.0 - f_fresh) * s._ra_lag_c

        # Coil (valve only effective with pump or >2 % leakage allowance)
        cooling = p.coil_drop_k_full * (u.chwv_pct / 100.0) if (u.chw_pump_cmd or u.chwv_pct > 2.0) else 0.0
        t_coil_out = s.t_mix_c - cooling
        s._coil_lag_c += (t_coil_out - s._coil_lag_c) * dt / p.coil_lag_s
        s.t_sat_c = s._coil_lag_c

        # Zone balance
        load_k = p.design_load_kw_occupied if self.occ else p.design_load_kw_unoccupied
        dtdt = (m_dot_kgs * p.cp_air_jkgk * (s.t_sat_c - s.t_space_c)) / p.zone_capacity_jk \
            + load_k * 1e3 / p.zone_capacity_jk
        s.t_space_c += dtdt * dt

        # Return air
        s._ra_lag_c += (s.t_space_c - s._ra_lag_c) * dt / p.ra_lag_s
        s.t_ra_c = s._ra_lag_c

        # CO2: occupant generation vs fresh-air dilution
        gen = p.co2_gen_ppm_s_per_person * self.occ
        ach = f_fresh * p.supply_flow_m3s / p.zone_volume_m3   # fresh air changes / s
        vent = ach * (p.co2_outdoor_ppm - s._co2_lag_ppm)
        s._co2_lag_ppm = max(p.co2_outdoor_ppm - 50.0, s._co2_lag_ppm + (gen + vent) * dt)
        s.co2_ppm = s._co2_lag_ppm

    def _record(self):
        s = self.s
        s.history.append((round(s.t_sat_c, 2), round(s.t_space_c, 2),
                          round(s.co2_ppm, 0), round(s.dp_filter_pa, 1), s.fan_on))
        if len(s.history) > 3600:
            s.history.pop(0)

    def clog_filter(self, rate_pas: float):
        """Enable the filter-clog ramp (test T5)."""
        self.p.dp_clog_rate_pas = rate_pas
