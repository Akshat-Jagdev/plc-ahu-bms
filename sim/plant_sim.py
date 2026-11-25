"""AHU plant simulator: the field side of the PLC's Modbus I/O.

Once a second it reads the PLC outputs (coils 0-5, holding registers 0-2), advances the
lumped AHU model and writes the input image back (holding registers 1124-1138). Every cycle
is logged to CSV and every state or alarm change is logged as an event.

From the project root, with the control program deployed and the runtime running:

    .venv/bin/python -m sim.plant_sim --test all                              # tests T1-T5
    .venv/bin/python -m sim.plant_sim --duration 120                          # free run
    .venv/bin/python -m sim.plant_sim --duration 300 --events "60:occ=150;120:sf_trip=1"
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException

from .physics import AhuInputs, AhuPlant

HOST, PORT, UNIT = "127.0.0.1", 502, 1
COILS = ("sf_cmd", "rf_cmd", "exd_cmd", "fad_cmd", "alm_gen", "pump_cmd")
REGISTERS = 3                        # holding 0-2: valve, state, alarm word
H_IMAGE = 1124                       # first holding register of the input image (%MW100)
COUNTS = 10000                       # 0-10 V signals as raw counts
AI = (("sat_c", 40.0), ("rat_c", 40.0), ("oat_c", 50.0), ("co2_ppm", 2000.0), ("dp_pa", 500.0))
DP_SWITCH_PA = 250.0                 # filter dP switch wired to FILT_DP_HS
CONTACTOR_S = 1.0                    # supply-fan contactor pull-in time
STATES = {0: "IDLE", 1: "DAMPERS", 2: "RF", 3: "SF", 4: "RUN", 5: "PURGE", 6: "TRIPPED", 7: "HAND"}
COLUMNS = ["t", "state", "state_name", "alarm", *COILS, "chwv_pct", "sat_c", "rat_c", "oat_c", "space_c",
           "co2_ppm", "dp_pa", "damper_pos", "occupancy", "sf_run_fb", "sf_trip", "rf_trip", "smk_intlk",
           "fs_intlk", "filt_dp_hs", "lo_oa_t", "man_auto", "occ_sched", "trip_reset"]


@dataclass
class FieldInputs:
    """Digital inputs as wired on the panel (1 = healthy for the N/C interlock loops)."""
    sf_trip: bool = False
    rf_trip: bool = False
    smk_intlk: bool = True
    fs_intlk: bool = True
    lo_oa_t: bool = False
    man_auto: bool = True
    occ_sched: bool = True
    trip_reset: bool = False


class PlantSim:
    def __init__(self, client, outdoor_c=35.0, occupancy=0, co2_start_ppm=None):
        self.client = client
        self.plant = AhuPlant(occupancy=occupancy, outdoor_c=outdoor_c)
        if co2_start_ppm is not None:
            self.plant.s.co2_ppm = co2_start_ppm
            self.plant.s._co2_lag_ppm = co2_start_ppm
        self.field = FieldInputs()
        self.out = {}
        self.t = 0.0
        self.sf_run_fb = False
        self.events = []
        self._contactor_s = 0.0
        self._schedule = []
        self._seen = {}

    # ---- Modbus exchange
    def _read(self, method, address, count):
        resp = getattr(self.client, method)(address, count=count, device_id=UNIT)
        if resp.isError():
            raise ModbusException("%s(%d, %d) returned %s" % (method, address, count, resp))
        return resp

    def read_outputs(self):
        coils = self._read("read_coils", 0, len(COILS)).bits[:len(COILS)]
        regs = self._read("read_holding_registers", 0, REGISTERS).registers
        self.out = dict(zip(COILS, (int(bit) for bit in coils)))
        self.out["chwv_pct"] = regs[0] / 100.0
        self.out["state"] = regs[1]
        self.out["alarm"] = regs[2]

    def write_inputs(self):
        s, f = self.plant.s, self.field
        f.filt_dp_hs = s.dp_filter_pa > DP_SWITCH_PA
        di = [self.sf_run_fb, f.sf_trip, f.rf_trip, f.smk_intlk, f.fs_intlk, f.filt_dp_hs,
              f.lo_oa_t, f.man_auto, f.occ_sched, f.trip_reset]
        regs = [1 if value else 0 for value in di]
        analog = {"sat_c": s.t_sat_c, "rat_c": s.t_ra_c, "oat_c": self.plant.outdoor_c,
                  "co2_ppm": s.co2_ppm, "dp_pa": s.dp_filter_pa}
        regs += [int(round(min(max(analog[name], 0.0), span) / span * COUNTS)) for name, span in AI]
        if self.client.write_registers(H_IMAGE, regs, device_id=UNIT).isError():
            raise ModbusException("writing the input image failed")

    def cycle(self):
        """One logged second: scheduled events, read, physics, write."""
        for item in list(self._schedule):
            if self.t >= item[0]:
                self._schedule.remove(item)
                self.apply(item[1], item[2])
        self.read_outputs()
        self.step()
        self.write_inputs()
        return self.row()

    # ---- scenario control
    def apply(self, key, value):
        if key == "occ":
            self.plant.occ = int(value)
        elif key == "outdoor":
            self.plant.outdoor_c = float(value)
        elif key == "clog":
            self.plant.clog_filter(float(value))
        elif key == "reset":
            # maintained push-button: released again two seconds later
            self.field.trip_reset = bool(int(value))
            if self.field.trip_reset:
                self._schedule.append((self.t + 2.0, "reset", 0))
        elif key in ("sf_trip", "rf_trip", "smk_intlk", "fs_intlk", "lo_oa_t", "man_auto", "occ_sched"):
            setattr(self.field, key, bool(int(value)))
        else:
            raise ValueError("unknown event key: %s" % key)

    def step(self):
        out, field = self.out, self.field
        self._contactor_s = self._contactor_s + 1.0 if out["sf_cmd"] else 0.0
        self.sf_run_fb = bool(out["sf_cmd"]) and self._contactor_s >= CONTACTOR_S and not field.sf_trip
        self.plant.step(AhuInputs(sf_cmd=out["sf_cmd"], rf_cmd=out["rf_cmd"], exd_open=out["exd_cmd"],
                                  fad_open=out["fad_cmd"], chwv_pct=out["chwv_pct"],
                                  chw_pump_cmd=out["pump_cmd"]), 1.0)
        self.t += 1.0
        for name in ("state", "alarm", *COILS):
            self._watch(name, out[name])

    def _watch(self, name, value):
        if self._seen.get(name) != value:
            if name in self._seen:
                self.events.append((self.t, name, self._seen[name], value))
            self._seen[name] = value

    def row(self):
        out, s, f = self.out, self.plant.s, self.field
        row = {"t": self.t, "state": out["state"], "state_name": STATES.get(out["state"], "?"),
               "alarm": out["alarm"], **{name: out[name] for name in COILS},
               "chwv_pct": round(out["chwv_pct"], 1), "sat_c": round(s.t_sat_c, 2),
               "rat_c": round(s.t_ra_c, 2), "oat_c": round(self.plant.outdoor_c, 1),
               "space_c": round(s.t_space_c, 2), "co2_ppm": round(s.co2_ppm),
               "dp_pa": round(s.dp_filter_pa, 1), "damper_pos": round(s.damper_pos, 2),
               "occupancy": self.plant.occ, "sf_run_fb": int(self.sf_run_fb)}
        for name in ("sf_trip", "rf_trip", "smk_intlk", "fs_intlk", "filt_dp_hs", "lo_oa_t",
                     "man_auto", "occ_sched", "trip_reset"):
            row[name] = int(getattr(f, name))
        return row

    # ---- bring-up and tidy-up between runs
    def bring_to_idle(self, start_occupied):
        """At power-up the PLC sees a zero input image, reads the N/C smoke loop as open and
        trips. Publish a healthy unoccupied image, give a reset pulse, confirm IDLE, then
        hand the schedule back."""
        self.field.occ_sched = False
        self.read_outputs()
        self.write_inputs()
        time.sleep(1.5)
        self.apply("reset", 1)
        self.write_inputs()
        time.sleep(2.5)
        self.apply("reset", 0)
        self._schedule.clear()
        self.write_inputs()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(1.0)
            self.read_outputs()
            if self.out["state"] == 0:
                break
        else:
            raise RuntimeError("PLC did not reach IDLE (state %s)" % self.out.get("state"))
        self.field.occ_sched = start_occupied
        self._seen.clear()

    def settle(self):
        """Clear everything a run injected and leave the unit idle for the next one."""
        f = self.field
        f.sf_trip = f.rf_trip = f.lo_oa_t = f.trip_reset = False
        f.smk_intlk = f.fs_intlk = f.man_auto = True
        f.occ_sched = False
        self.plant.clog_filter(0.0)
        self._schedule.clear()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            self.read_outputs()
            if self.out["state"] == 0:
                return True
            if self.out["state"] == 6 and not f.trip_reset:
                self.apply("reset", 1)
            self.step()
            self.write_inputs()
            time.sleep(1.0)
        return False


def drive(sim, duration, events, sink):
    pending = sorted((float(at), key, value) for at, key, value in events)
    next_t = time.monotonic()
    next_report = 60.0
    while sim.t < duration:
        while pending and pending[0][0] <= sim.t:
            _, key, value = pending.pop(0)
            sim.apply(key, value)
        sink(sim.cycle())
        if sim.t >= next_report:
            out, s = sim.out, sim.plant.s
            print("  t=%4.0f %-7s SAT %.1f C  CO2 %4.0f ppm  valve %3.0f %%  alarm %d"
                  % (sim.t, STATES.get(out["state"]), s.t_sat_c, s.co2_ppm, out["chwv_pct"], out["alarm"]),
                  flush=True)
            next_report += 60.0
        next_t += 1.0
        pause = next_t - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        else:
            next_t = time.monotonic()


# ---- checks, all reading the logged rows
def first(rows, cond, start=0.0):
    for row in rows:
        if row["t"] >= start and cond(row):
            return row["t"]
    return None


def at(rows, t):
    return next(row for row in rows if row["t"] == t)


def window(rows, t0, t1):
    return [row for row in rows if t0 <= row["t"] <= t1]


def fmt(value):
    return "-" if value is None else "%.0f" % value


def check_t1(rows):
    t_fad = first(rows, lambda r: r["fad_cmd"])
    t_rf = first(rows, lambda r: r["rf_cmd"])
    t_sf = first(rows, lambda r: r["sf_cmd"])
    t_run = first(rows, lambda r: r["state"] == 4)
    dev = [abs(r["sat_c"] - 13.0) for r in window(rows, 240, rows[-1]["t"])]
    return [
        ("dampers open first, within 3 s of the occupied schedule", t_fad is not None and t_fad <= 3.0, "t=%s" % fmt(t_fad)),
        ("return fan starts after the 10 s damper prove", None not in (t_fad, t_rf) and 9.0 <= t_rf - t_fad <= 12.0, "%s s later" % fmt(t_rf - t_fad if t_rf and t_fad else None)),
        ("supply fan starts after the 5 s return-fan prove", None not in (t_rf, t_sf) and 4.0 <= t_sf - t_rf <= 7.0, "%s s later" % fmt(t_sf - t_rf if t_sf and t_rf else None)),
        ("RUN after the 5 s supply-fan prove", None not in (t_sf, t_run) and 4.0 <= t_run - t_sf <= 7.0, "%s s later" % fmt(t_run - t_sf if t_run and t_sf else None)),
        ("SAT within 13 +-1 C from 240 s to the end", bool(dev) and max(dev) <= 1.0, "max deviation %.2f K" % (max(dev) if dev else -1)),
        ("no trip latched", all(r["alarm"] & 31 == 0 for r in rows), ""),
    ]


def check_t2(rows):
    t_open = first(rows, lambda r: r["fad_cmd"], start=200.0)
    t_close = first(rows, lambda r: not r["fad_cmd"] and r["state"] == 4, start=(t_open or 0) + 30.0)
    t_hold = rows[-1]["t"] if t_open is None else (t_close - 1.0 if t_close else rows[-1]["t"])
    after = window(rows, t_open, rows[-1]["t"]) if t_open else []
    peak = max((r["co2_ppm"] for r in after), default=0)
    steady = window(rows, t_open, t_hold) if t_open else []
    out_of_band = [r["t"] for r in steady if abs(r["sat_c"] - 13.0) > 1.0]
    recovered = (max(out_of_band) + 1.0 - t_open) if out_of_band else 0.0
    deviation = max((abs(r["sat_c"] - 13.0) for r in steady), default=0.0)
    return [
        ("DCV opens the dampers after the occupancy step", t_open is not None and t_open <= 700.0, "t=%s" % fmt(t_open)),
        ("CO2 peak after opening stays below 850 ppm", bool(after) and peak < 850.0, "peak %s ppm" % peak),
        ("DCV closes the dampers again once CO2 has fallen",
         t_open is not None and t_close is not None, "t=%s" % fmt(t_close)),
        ("CO2 below 800 ppm at the end", rows[-1]["co2_ppm"] < 800.0, "%s ppm" % rows[-1]["co2_ppm"]),
        ("SAT within 13 +-1 C before the step (240-370 s)",
         bool(window(rows, 240, 370)) and all(abs(r["sat_c"] - 13.0) <= 1.0 for r in window(rows, 240, 370)), ""),
        ("SAT back within 13 +-1 C within 150 s of the damper step",
         t_open is not None and recovered <= 150.0,
         "peak deviation %.1f K, recovered after %s s" % (deviation, fmt(recovered))),
        ("no trip latched", all(r["alarm"] & 31 == 0 for r in rows), ""),
    ]


def check_t3(rows):
    # the trip is published at the end of the 120 s cycle, so row 122 is the first sample after it
    r = at(rows, 122)
    off = all(r[k] == 0 for k in ("sf_cmd", "rf_cmd", "fad_cmd", "exd_cmd", "pump_cmd")) and r["chwv_pct"] == 0.0
    hold = window(rows, 151, 209)
    return [
        ("every output off within 1 s of the trip", off, ""),
        ("general alarm on, state TRIPPED", r["alm_gen"] == 1 and r["state"] == 6, ""),
        ("first-out cause is the supply-fan trip", r["alarm"] & 31 == 1, "alarm word %d" % r["alarm"]),
        ("still tripped after the cause clears (151-209 s)", all(x["state"] == 6 and x["alm_gen"] == 1 for x in hold), ""),
        ("manual reset restarts the sequence: RUN by 240 s", any(x["state"] == 4 for x in window(rows, 212, 240)), ""),
    ]


def check_t4(rows):
    r = at(rows, 122)
    off = all(r[k] == 0 for k in ("sf_cmd", "rf_cmd", "fad_cmd", "exd_cmd", "pump_cmd")) and r["chwv_pct"] == 0.0
    t_exd = first(rows, lambda x: x["exd_cmd"], start=121.0)
    return [
        ("every output off within 1 s, exhaust damper still closed", off, ""),
        ("general alarm on, state TRIPPED, first-out = smoke", r["alm_gen"] == 1 and r["state"] == 6 and r["alarm"] & 31 == 4, "alarm word %d" % r["alarm"]),
        ("exhaust damper stays closed for the first 9 s (fan spin-down)", all(x["exd_cmd"] == 0 for x in window(rows, 121, 129)), ""),
        ("exhaust damper opens for smoke purge 10-14 s after the trip", t_exd is not None and 10.0 <= t_exd - 120 <= 14.0, "t=%s" % fmt(t_exd)),
        ("fresh-air damper stays closed while tripped", all(x["fad_cmd"] == 0 for x in window(rows, 121, 209)), ""),
        ("still tripped after the smoke clears (181-209 s)", all(x["state"] == 6 for x in window(rows, 181, 209)), ""),
        ("manual reset restarts the sequence: RUN by 240 s", any(x["state"] == 4 for x in window(rows, 212, 240)), ""),
    ]


def check_t5(rows):
    t_alarm = first(rows, lambda r: r["alm_gen"] == 1 and r["alarm"] & 32, start=61.0)
    dev = [abs(r["sat_c"] - 13.0) for r in window(rows, 240, rows[-1]["t"])]
    return [
        ("filter alarm raised once dP passes 250 Pa", t_alarm is not None and at(rows, t_alarm)["dp_pa"] >= 250.0, "t=%s" % fmt(t_alarm)),
        ("no trip raised by the filter alarm", all(r["state"] != 6 and r["alarm"] & 31 == 0 for r in rows), ""),
        ("unit stays in RUN throughout", all(r["state"] == 4 for r in window(rows, 30, rows[-1]["t"])),
         "from %d s" % min(r["t"] for r in window(rows, 30, rows[-1]["t"]))),
        ("SAT within 13 +-1 C from 240 s to the end with the alarm present", bool(dev) and max(dev) <= 1.0, "max deviation %.2f K" % (max(dev) if dev else -1)),
    ]


@dataclass
class Test:
    code: str
    title: str
    duration: float
    plant: dict
    events: list
    check: object


TESTS = [
    Test("T1", "Cold start: ordered sequence, SAT converges to 13 +-1 C", 360, {},
         [(0, "occ_sched", 1)], check_t1),
    Test("T2", "Occupancy step: DCV opens the dampers, CO2 settles below 800 ppm", 960,
         {"co2_start_ppm": 750.0}, [(0, "occ_sched", 1), (180, "occ", 100)], check_t2),
    Test("T3", "Supply-fan trip mid-run: failsafe stop, latched until reset", 300, {},
         [(0, "occ_sched", 1), (120, "sf_trip", 1), (150, "sf_trip", 0), (210, "reset", 1)], check_t3),
    Test("T4", "Smoke interlock: failsafe stop, exhaust purge, latched until reset", 330, {},
         [(0, "occ_sched", 1), (120, "smk_intlk", 0), (180, "smk_intlk", 1), (210, "reset", 1)], check_t4),
    Test("T5", "Filter clog ramp: alarm at 250 Pa, control unaffected", 360, {},
         [(0, "occ_sched", 1), (60, "clog", 1.0)], check_t5),
]


def save_events(sim, path):
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("t", "field", "old", "new"))
        writer.writerows(sim.events)


def run_test(client, test, out_dir):
    sim = PlantSim(client, **test.plant)
    sim.bring_to_idle(start_occupied=False)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out_dir / ("%s-%s.csv" % (test.code, stamp))
    rows = []
    print("%s  %s  (%d s)" % (test.code, test.title, test.duration), flush=True)
    with open(csv_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()

        def sink(row):
            rows.append(row)
            writer.writerow(row)
            handle.flush()

        drive(sim, test.duration, test.events, sink)
    save_events(sim, out_dir / ("%s-%s.events.csv" % (test.code, stamp)))

    checks = []
    for name, ok, detail in test.check(rows):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, "  [%s]" % detail if detail else ""), flush=True)
    passed = all(c["ok"] for c in checks)
    print("  -> %s  (%d/%d)  log %s" % ("PASS" if passed else "FAIL", sum(c["ok"] for c in checks), len(checks), csv_path), flush=True)
    summary = {"test": test.code, "title": test.title, "started": stamp, "duration_s": test.duration,
               "passed": passed, "checks": checks, "csv": csv_path.name}
    (out_dir / ("%s-%s.json" % (test.code, stamp))).write_text(json.dumps(summary, indent=2) + "\n")
    if not sim.settle():
        raise RuntimeError("PLC did not return to IDLE after %s" % test.code)
    return passed


def run_free(client, args, out_dir):
    events = []
    for item in filter(None, (part.strip() for part in args.events.split(";"))):
        at_s, _, assignment = item.partition(":")
        key, _, value = assignment.partition("=")
        if not (at_s and key and value):
            sys.exit('bad event "%s", expected t:key=value' % item)
        events.append((float(at_s), key, value))
    sim = PlantSim(client, outdoor_c=args.outdoor, occupancy=args.occupancy, co2_start_ppm=args.co2_start)
    sim.bring_to_idle(start_occupied=not events)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out_dir / ("free-%s.csv" % stamp)
    with open(csv_path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()

        def sink(row):
            writer.writerow(row)
            handle.flush()

        try:
            drive(sim, args.duration, events, sink)
        except KeyboardInterrupt:
            print("stopped at t=%.0f" % sim.t)
    save_events(sim, csv_path.with_name(csv_path.stem + ".events.csv"))
    print("log %s" % csv_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--test", help="comma-separated T1..T5, or all")
    parser.add_argument("--duration", type=float, default=0.0, help="free-run length in seconds")
    parser.add_argument("--events", default="", help='free-run events as "t:key=value;..."')
    parser.add_argument("--occupancy", type=int, default=0)
    parser.add_argument("--outdoor", type=float, default=35.0, help="outdoor temperature in C")
    parser.add_argument("--co2-start", type=float, default=None, help="initial space CO2 in ppm")
    parser.add_argument("--out-dir", default="runs")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = ModbusTcpClient(HOST, port=PORT)
    try:
        if not client.connect():
            raise ConnectionException("connect() returned False")
        if args.test:
            wanted = [t for t in TESTS if args.test == "all" or t.code in args.test.split(",")]
            if not wanted:
                sys.exit("no test matches --test %s" % args.test)
            failed = [t.code for t in wanted if not run_test(client, t, out_dir)]
            print("RESULT: %s" % ("all passed" if not failed else "failed: %s" % ", ".join(failed)), flush=True)
            sys.exit(1 if failed else 0)
        elif args.duration > 0:
            run_free(client, args, out_dir)
        else:
            parser.print_usage()
    except ConnectionException as exc:
        sys.exit("Modbus TCP is not answering on %s:%d (%s). Is the runtime running?" % (HOST, PORT, exc))
    finally:
        client.close()


if __name__ == "__main__":
    main()
