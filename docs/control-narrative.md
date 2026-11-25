# AHU-01 Control Narrative + I/O List (design doc — written before programming)

Real-BMS-style spec for the PLC program. Modeled on a standard constant-/variable-volume
air handling unit serving a commercial floor in a Dubai tower. Everything here is what the
program must implement; the "plant" is simulated in Python (Modbus TCP) at the other end of
the PLC I/O.

## Physical process being controlled

Outdoor air (OA) + return air (RA) mix in the mixing box → pass the filter → chilled-water
(CHW) cooling coil → supply fan (SF) → duct → space. Space air returns via return fan (RF)
+ exhaust damper (EXD). A CO2 sensor in the occupied space drives demand-controlled
ventilation (DCV). Cooling is modulated by a 0–10 V control valve on the CHW coil.
Goal: hold supply-air temperature (SAT) at setpoint (default 13 °C) and space CO2 below
setpoint (default 800 ppm) with safe equipment sequencing.

## I/O list

### Digital inputs (DI)
| # | Tag | Description | Notes |
|---|-----|-------------|-------|
| 1 | SF_RUN_CMD_FB | Supply fan run command feedback | mirror of command (simulated contactor) |
| 2 | SF_TRIP | Supply fan overload/trip | fault latch test path |
| 3 | RF_TRIP | Return fan overload/trip | |
| 4 | SMK_INTLK | Smoke detector interlock (N/C) | opens on smoke; forces shutdown |
| 5 | FS_INTLK | Freon/gas leak interlock | hard shutdown |
| 6 | FILT_DP_HS | Filter differential-pressure switch | alarm only, no trip |
| 7 | LO_OA_T | Lockout: OA temp too low for economizer | narrative completeness |
| 8 | MAN_AUTO | Hand/Auto selector (Auto=1) | bypass path for maintenance |

### Digital outputs (DO)
| # | Tag | Description | Notes |
|---|-----|-------------|-------|
| 1 | SF_CMD | Supply fan start/stop | via contactor (sim) |
| 2 | RF_CMD | Return fan start/stop | |
| 3 | EXD_CMD | Exhaust damper open | 1=open |
| 4 | FAD_CMD | Fresh-air damper open | 1=open (minimum pos in sim = full) |
| 5 | CHWV_CMD is analog — see AO | | |
| 6 | ALM_GEN | General alarm annunciator | |
| 7 | CHW_PUMP_CMD | CHW secondary pump enable | runs with any valve demand >2 % |

### Analog inputs (AI) — 0–10 V / 4–20 mA scaled
| # | Tag | Range | Function |
|---|-----|-------|----------|
| 1 | SAT | 0–40 °C | control PV: supply air temp |
| 2 | RAT | 0–40 °C | monitoring + economizer logic |
| 3 | OAT | 0–50 °C | monitoring, DCV + lockout |
| 4 | CO2 | 0–2000 ppm | DCV control PV |
| 5 | DP_SAT | 0–500 Pa | filter ΔP (analog variant) |

### Analog outputs (AO)
| # | Tag | Range | Function |
|---|-----|-------|----------|
| 1 | CHWV | 0–100 % | CHW coil valve (PI output) |

## Control narrative

### 1. Start/stop sequence (permissive + ordered start)
**Start (Auto, occupied hours, no trips):**
1. Prove SMK_INTLK closed, no SF/RF trip latch, MAN_AUTO=Auto.
2. Open FAD + EXD, prove 10 s (damper travel time in sim).
3. Start RF, prove 5 s.
4. Start SF, prove 5 s.
5. Enable PI loop (initialize from current valve demand).
**Stop (normal):** reverse — PI to 0, SF off → 10 s purge → RF off → dampers to minimum,
pump stops when valve < 2 %.
**Trip (SF_TRIP / RF_TRIP / SMK / FS):** everything off immediately (failsafe: dampers
EXD open for smoke purge only when SMK and fans stopped — real BMS nuance), latch trip,
require manual reset.

### 2. SAT control loop
PI on SAT (PV) vs 13 °C SP → CHWV %. Direct-acting (higher SAT → more valve). Output
clamped 0–100 %, anti-windup on clamp, 10 s loop period (real BMS pace, not 10 ms).

### 3. CO2 demand-controlled ventilation
CO2 > 800 ppm → FAD/EXD open (in a real VAV box: modulate OA damper 30–100 %; sim uses
open/closed + pump-down hysteresis of 100 ppm to avoid chatter).

### 4. Alarm logic
FILT_DP_HS or DP_SAT > 250 Pa → ALM_GEN + event log entry. Any trip → ALM_GEN.

### 5. "Occupied" schedule
Simulator publishes a schedule bit; unoccupied = everything off, loop disabled.

## Success tests (what the demo video will show)
T1 Cold start: sequence visibly ordered (dampers → RF → SF), SAT converges to 13 ±1 °C
in steady state.
T2 Load step: simulator bumps occupancy (CO2 gen) — DCV opens dampers, CO2 settles < 800.
T3 SF trip mid-run: everything drops failsafe, latch holds until reset.
T4 Smoke interlock: same + purge behavior.
T5 Filter clog ramp: alarm at threshold, control unaffected.

## PLC ↔ simulator interface (Modbus TCP)
OpenPLC exposes %I/%Q/%IW/%QW on coils/holding registers via Modbus. Python simulator:
reads DO/AO, integrates AHU physics 1 Hz, writes DI/AI. All tags above map 1:1 to the
OpenPLC address table (final mapping recorded after program compiles — see docs/
address-map.md when written).

## Project scope
Self-taught IEC 61131-3 Structured Text project with a simulated plant.

## As-built notes (25 Nov 2026)
The sections above describe the original design. These are the
places where the built system differs; docs/address-map.md has the full table.

- **Two inputs added.** The narrative needs an occupied schedule bit and a manual reset but
  listed no input for either: DI9 OCC_SCHED and DI10 TRIP_RESET were added.
- **Inputs arrive through an input image.** The statement above that `%I` is exposed on
  coils is wrong for this OpenPLC build: its Modbus server exposes `%IX` and `%IW` read-only.
  The simulator writes the input image to %MW100-114 (holding registers 1124-1138) and the
  program reads it in one mapping section.
- **State and alarm words added** (%QW1, %QW2) so the sequence and first-out cause are
  visible over Modbus. The runtime has no event journal; the event log in the test evidence
  comes from the simulator timestamping every observed change of these words.
- **Dampers are open/closed.** Closed is the minimum position (the model still leaks 15 %
  outdoor air); open is full. The start sequence proves damper travel, then ventilation
  control owns the dampers. "Dampers to minimum" on a stop is the closed command.
- **Ventilation lockout.** LO_OA_T stops ventilation control from opening the dampers; CO2
  above setpoint is then reported as a warning bit.
- **Supply-fan start prove** requires run feedback within 5 s; missing feedback is a latched
  trip. The return fan has no feedback input in this I/O list, so it is proven on time only.
- **Hand** releases every automatic output; returning to Auto restarts from IDLE.
- **PI loop:** 6 % valve per K, 60 s integral time, 10 s period, no integrating while the
  output is saturated. While disabled the integral carries the last output, so the loop
  starts from the valve's current position.
