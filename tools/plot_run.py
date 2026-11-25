#!/usr/bin/env python3
"""Trend chart for one plant_sim log. The CSV itself is the table view of this chart.

    .venv/bin/python tools/plot_run.py runs/T1-20261125-201500.csv docs/img/T1.png
    .venv/bin/python tools/plot_run.py runs/T1-20261125-201500.csv docs/img/T1-start.png --xlim 0 60
"""
import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE = "#2a78d6", "#eb6834"   # categorical slots 1 and 2
CRITICAL = "#d03b3b"                  # status colour, used only on the labelled TRIPPED band
LINE = 1.6                            # 2 px at this figure's scale
COMMANDS = (("alm_gen", "ALARM"), ("pump_cmd", "PUMP"), ("exd_cmd", "EXD"),
            ("fad_cmd", "FAD"), ("rf_cmd", "RF"), ("sf_cmd", "SF"))
CAUSES = (("sf_trip", "SF trip", "SF trip cleared"), ("rf_trip", "RF trip", "RF trip cleared"),
          ("smk_intlk", "smoke cleared", "smoke"), ("fs_intlk", "leak cleared", "gas leak"),
          ("trip_reset", "reset", None))
SAT_SP, CO2_OPEN, CO2_CLOSE, DP_ALARM = 13.0, 800.0, 700.0, 250.0


def load(path):
    rows = []
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append({key: (value if key == "state_name" else float(value)) for key, value in row.items()})
    return rows


def scenario_events(rows):
    """Injected field events, read back from the logged input columns."""
    events, previous = [], rows[0]
    for row in rows[1:]:
        for key, rising_label, falling_label in CAUSES:
            before, now = previous[key], row[key]
            if now == 1.0 and before == 0.0 and rising_label:
                events.append((row["t"], rising_label))
            elif now == 0.0 and before == 1.0 and falling_label:
                events.append((row["t"], falling_label))
        if row["occupancy"] != previous["occupancy"]:
            events.append((row["t"], "occupancy %d" % row["occupancy"]))
        previous = row
    return events


def style(ax, title, pad=7):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_title(title, loc="left", fontsize=9, color=INK_2, pad=pad)


def reference(ax, level):
    ax.axhline(level, color=MUTED, linewidth=0.8, linestyle=(0, (4, 3)))


def end_label(ax, t, value, text, color):
    ax.annotate(text, xy=(t, value), xytext=(6, 0), textcoords="offset points",
                va="center", ha="left", fontsize=8, color=INK_2)
    ax.plot([t], [value], marker="o", markersize=5, color=color, markeredgecolor=SURFACE, markeredgewidth=1.2)


def draw_commands(ax, rows):
    t = [row["t"] for row in rows]
    for index, (key, _) in enumerate(COMMANDS):
        level = [index + 0.7 * row[key] for row in rows]
        ax.fill_between(t, index, level, step="post", color=BLUE, alpha=0.10, linewidth=0)
        ax.step(t, level, where="post", color=BLUE, linewidth=LINE, solid_joinstyle="round")
    top = len(COMMANDS)
    start, current = rows[0]["t"], rows[0]["state_name"]
    for row in rows[1:] + [None]:
        name = row["state_name"] if row else None
        if name != current:
            end = row["t"] if row else rows[-1]["t"] + 1.0
            if current == "TRIPPED":
                ax.fill_between([start, end], top, top + 0.9, step="mid", color=CRITICAL, alpha=0.14, linewidth=0)
            span = ax.get_xlim()[1] - ax.get_xlim()[0]
            if end - start >= 0.075 * span:
                ax.text((start + end) / 2, top + 0.45, current, ha="center", va="center", fontsize=7.5,
                        color=INK if current == "TRIPPED" else INK_2)
            start, current = end, name
    ax.set_yticks([index + 0.35 for index in range(len(COMMANDS))] + [top + 0.45])
    ax.set_yticklabels([label for _, label in COMMANDS] + ["STATE"])
    ax.set_ylim(-0.15, top + 1.05)
    ax.grid(False)


def draw_temps(ax, rows):
    t = [row["t"] for row in rows]
    ax.axhline(SAT_SP, color=MUTED, linewidth=0.8, linestyle=(0, (4, 3)))
    ax.plot(t, [row["sat_c"] for row in rows], color=BLUE, linewidth=LINE, solid_joinstyle="round", label="Supply air (SAT)")
    ax.plot(t, [row["rat_c"] for row in rows], color=ORANGE, linewidth=LINE, solid_joinstyle="round", label="Return air (RAT)")
    last = rows[-1]
    end_label(ax, last["t"], last["sat_c"], "SAT %.1f" % last["sat_c"], BLUE)
    if abs(last["rat_c"] - last["sat_c"]) > 0.12 * (ax.get_ylim()[1] - ax.get_ylim()[0]):
        end_label(ax, last["t"], last["rat_c"], "RAT %.1f" % last["rat_c"], ORANGE)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1.01), ncol=2, borderaxespad=0,
              frameon=False, fontsize=8, labelcolor=INK_2, handlelength=1.6)


def draw_co2(ax, rows):
    t = [row["t"] for row in rows]
    ax.plot(t, [row["co2_ppm"] for row in rows], color=BLUE, linewidth=LINE, solid_joinstyle="round")
    reference(ax, CO2_OPEN)
    reference(ax, CO2_CLOSE)
    end_label(ax, rows[-1]["t"], rows[-1]["co2_ppm"], "%.0f" % rows[-1]["co2_ppm"], BLUE)


def draw_valve(ax, rows):
    t = [row["t"] for row in rows]
    ax.plot(t, [row["chwv_pct"] for row in rows], color=BLUE, linewidth=LINE, solid_joinstyle="round")
    ax.set_ylim(-3, 108)
    end_label(ax, rows[-1]["t"], rows[-1]["chwv_pct"], "%.0f %%" % rows[-1]["chwv_pct"], BLUE)


def draw_dp(ax, rows):
    t = [row["t"] for row in rows]
    ax.plot(t, [row["dp_pa"] for row in rows], color=BLUE, linewidth=LINE, solid_joinstyle="round")
    reference(ax, DP_ALARM)
    end_label(ax, rows[-1]["t"], rows[-1]["dp_pa"], "%.0f" % rows[-1]["dp_pa"], BLUE)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", help="CSV written by sim.plant_sim")
    parser.add_argument("png", help="output image")
    parser.add_argument("--title", help="chart title (default: taken from the test summary next to the log)")
    parser.add_argument("--xlim", nargs=2, type=float, metavar=("START", "END"), help="crop the time axis")
    args = parser.parse_args()

    log = Path(args.log)
    rows = load(log)
    events = scenario_events(rows)
    x0, x1 = args.xlim or (0, rows[-1]["t"])
    rows = [row for row in rows if x0 <= row["t"] <= x1]
    if x1 <= x0 or not rows:
        parser.error("time window must have a positive duration and include logged samples")
    summary_path = log.with_name(log.stem + ".json")
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else None
    if args.title:
        title = args.title
    elif summary:
        title = "%s  %s  (%s, %d of %d checks)" % (summary["test"], summary["title"],
                                                   "pass" if summary["passed"] else "FAIL",
                                                   sum(c["ok"] for c in summary["checks"]), len(summary["checks"]))
    else:
        title = log.stem

    plt.rcParams.update({"font.family": ["Helvetica Neue", "Arial", "DejaVu Sans"], "font.size": 9,
                         "axes.unicode_minus": False, "savefig.facecolor": SURFACE, "figure.facecolor": SURFACE})
    with_dp = max(row["dp_pa"] for row in rows) > 95.0
    heights = [2.6, 1.7, 1.25, 1.2] + ([1.1] if with_dp else [])
    fig, axes = plt.subplots(len(heights), 1, sharex=True, figsize=(11, sum(heights) + 1.1),
                             gridspec_kw={"height_ratios": heights})
    span = x1 - x0
    axes[0].set_xlim(x0, x1)

    style(axes[0], "Commands and sequence state", pad=20 if events else 7)
    draw_commands(axes[0], rows)
    draw_temps(axes[1], rows)
    style(axes[1], "Air temperature (°C)   ·   dashed line at 13 °C")
    draw_co2(axes[2], rows)
    style(axes[2], "Space CO₂ (ppm)   ·   open above 800, close below 700")
    draw_valve(axes[3], rows)
    style(axes[3], "Chilled-water valve command (%)")
    if with_dp:
        draw_dp(axes[4], rows)
        style(axes[4], "Filter differential pressure (Pa)   ·   alarm above 250 Pa")

    for t_event, label in events:
        if not x0 <= t_event <= x1:
            continue
        for ax in axes:
            ax.axvline(t_event, color=AXIS, linewidth=0.8)
        axes[0].annotate(label, (t_event, 1), xycoords=("data", "axes fraction"),
                         xytext=(0, 3), textcoords="offset points", ha="center", fontsize=7.5, color=INK_2)
    axes[-1].xaxis.set_major_locator(MaxNLocator(nbins=9, integer=True))
    axes[-1].set_xlabel("Elapsed time (s)", fontsize=9, color=INK_2)
    for ax in axes[:-1]:
        ax.tick_params(labelbottom=False)

    fig.subplots_adjust(left=0.08, right=0.90, top=0.88, bottom=0.085, hspace=0.48)
    fig.suptitle(title.replace("+-", "±"), x=0.08, y=0.97, ha="left", fontsize=12, color=INK)
    fig.text(0.012, 0.012, "PLC program plc/control.st on OpenPLC, plant simulated by sim/plant_sim.py over Modbus TCP  ·  source %s"
             % log.name, fontsize=7.5, color=MUTED, ha="left", va="bottom")
    Path(args.png).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.png, dpi=160)
    print("wrote %s" % args.png)


if __name__ == "__main__":
    main()
