# Plant model: what `sim/physics.py` assumes

The "field device" at the other end of the PLC's Modbus I/O. `sim/plant_sim.py` runs the model
at 1 Hz against the runtime: it reads the PLC outputs (fan, damper and valve commands), advances
the model one second and writes the PLC inputs (temperatures, CO2, filter pressure, fan run
feedback and the trip inputs). The model itself has no I/O, so it runs and unit-tests without
a PLC (`tests/test_physics.py`).

## Model (deliberately simple, defensible in an interview)

Every constant is a stated assumption, not measured data. They live in `AhuParams`, one
dataclass at the top of `sim/physics.py`.

- **Airflow**: 1.6 m3/s (about 5.8k m3/h) once the supply fan has been running for 2 s; none
  before that or with the fan off.
- **Mixing box**: T_mix = f * T_oa + (1 - f) * T_ra, where f is the fresh-air fraction: 0.15
  with the dampers closed (minimum position still leaks) and 0.90 open. Position travels
  between the two in 10 s.
- **Coil**: temperature drop = valve position * 24 K, applied only while the pump is commanded
  or the valve is above 2 % (a leakage allowance, the same threshold the PLC uses to start the
  pump). 24 K is sized to hold 13 °C supply air with the dampers open at 35 °C outdoor, the
  demand-ventilation condition. Supply-air temperature follows the coil outlet through a 20 s
  first-order lag (coil and sensor).
- **Space**: C * dT/dt = m_dot * cp * (T_sat - T_space) + Q_load with C = 2.5 MJ/K and
  Q_load = 21 kW occupied, 11 kW unoccupied. At design airflow and 13 °C supply this puts the
  zone near 24 °C. With no airflow the zone drifts on the unoccupied load against 30 W/K of
  envelope loss to outdoor.
- **Return air**: follows the space temperature through a 60 s lag.
- **CO2**: dC/dt = G_occ - ach * (C - C_oa). G_occ = 0.0052 ppm/s per person, C_oa = 420 ppm,
  ach = fresh-air changes per second (f * 1.6 / 600 m3). Steady state is 420 + 13 * occupants
  ppm with the dampers closed and 420 + 2.2 * occupants open; the time constant is 2500 s
  closed and 417 s open. CO2 does not change without airflow.
- **Filter**: 80 Pa at design airflow plus a clog offset that grows while the fan runs (1.0
  Pa/s in test T5, crossing the 250 Pa alarm about 170 s after the ramp starts). The clog only
  raises pressure: the control narrative makes it an alarm, not a trip, so it does not reduce
  airflow. Pressure is zero without airflow.

## What the simulator adds on top

- The supply-fan run-feedback input follows the start command after 1 s (contactor pull-in)
  and drops the instant an SF trip is injected.
- The filter pressure switch input closes above 250 Pa; the PLC also alarms on the analog value.
- Analogue values are written as 0-10000 counts (0-10 V). Trip and interlock inputs are
  injected by the test scenario, not by physics.
- Each cycle reads the PLC outputs first and writes the inputs last, so an input injected at
  second t is first reflected in the row logged at t+2.

First-order lumped model, not CFD.
