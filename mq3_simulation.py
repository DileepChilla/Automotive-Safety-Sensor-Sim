#!/usr/bin/env python3
"""
MQ-3 Alcohol Gas Sensor — Automotive Safety Interlock Simulation (static analysis).

Physics and constants are taken from the manufacturer datasheet:

    Hanwei Electronics Co., Ltd. — "TECHNICAL DATA MQ-3 GAS SENSOR"
    http://www.hwsensor.com  (mirror: https://www.sparkfun.com/datasheets/Sensors/MQ-3.pdf,
    served from https://cdn.sparkfun.com/assets/6/a/1/7/b/MQ-3.pdf; fetched 2026-07-30)

Datasheet values used here (quoted from the document):
    Vc  (circuit voltage)      5 V ± 0.1 (AC or DC)
    VH  (heating voltage)      5 V ± 0.1 (AC or DC)
    RH  (heater resistance)    33 Ω ± 5 % at room temperature
    PH  (heating consumption)  < 750 mW
    RL  (load resistance)      200 kΩ recommended (usable 100 kΩ – 470 kΩ)
    Rs  (sensing resistance)   1 MΩ – 8 MΩ at 0.4 mg/L alcohol
    α(0.4/1 mg/L) slope        ≤ 0.6   (our digitized curve gives 0.55 — consistent)
    Detection range            0.05 mg/L – 10 mg/L alcohol
    Preheat time               over 24 h
    R0 reference               sensor resistance at 0.4 mg/L (≈ 200 ppm) alcohol
    Clean-air line (Fig. 2)    Rs(air)/R0 ≈ 60

Note on the widely quoted coefficients a = 0.3934, b = -1.504: they do NOT fit the
datasheet in the form Rs/R0 = a·ppm^b (that would give Rs/R0 ≈ 1.4e-4 at 200 ppm
instead of 1.0). They belong to the inverse regression

    mg/L = 0.3934 · (Rs/R0)^(-1.504)

which is equivalent to the forward power law Rs/R0 ≈ 33.5 · ppm^(-0.665) using the
datasheet conversion 0.4 mg/L ≈ 200 ppm.  This module fits its own forward power
law to the digitized Fig. 2 curve with scipy and verifies consistency with the
inverse form at import time.
"""

import argparse
import os
import sys

import numpy as np
from scipy.optimize import curve_fit

# ----------------------------------------------------------------------------
# Datasheet constants (Hanwei MQ-3 technical data — see module docstring)
# ----------------------------------------------------------------------------
VCC = 5.0                 # circuit voltage Vc [V]
V_HEATER = 5.0            # heater voltage VH [V]
R_HEATER = 33.0           # heater resistance RH [Ω]
P_HEATER_MAX = 0.750      # heating consumption PH [W]
PREHEAT_HOURS = 24        # required preheat time [h]
ADC_MAX = 1023            # 10-bit ADC (Arduino-style), Vref = VCC
CLEAN_AIR_RATIO = 60.0    # Rs(air)/R0, the "Air" line of datasheet Fig. 2
MG_L_TO_PPM = 500.0       # datasheet: 0.4 mg/L ≈ 200 ppm alcohol

# The datasheet defines R0 at 0.4 mg/L alcohol with Rs = 1–8 MΩ and RL = 200 kΩ.
# This simulation models an Arduino-style breakout with RL = 10 kΩ, so R0 is
# scaled proportionally (module trimpot calibration does exactly this) to keep
# the divider inside the 0–5 V ADC range.  The Rs/R0 curve — which carries all
# of the sensitivity information — is untouched.
RL = 10_000.0             # load resistance [Ω] (breakout-board value)
R0 = 2_000.0              # effective calibrated R0 [Ω]

# Digitized ethanol curve from datasheet Fig. 2 "sensitivity characteristics of
# the MQ-3" (log-log, 0.1–10 mg/L, Rs/R0 0.1–100; conditions 20 °C, 65 % RH,
# 21 % O2, RL = 200 kΩ):
DATASHEET_MG_L = np.array([0.1, 0.2, 0.4, 1.0, 2.0, 4.0, 10.0])
DATASHEET_RS_R0 = np.array([2.3, 1.7, 1.0, 0.55, 0.38, 0.26, 0.12])
DATASHEET_PPM = DATASHEET_MG_L * MG_L_TO_PPM

# German StVO §24a: 0.25 mg/L breath alcohol (≈ 0.5 ‰ BAC) — project threshold 50 ppm
LEGAL_LIMIT_MG_L = 0.25
DEFAULT_THRESHOLD_PPM = 50.0


def _power_law(ppm, a, b):
    return a * np.power(ppm, b)


def fit_datasheet_curve():
    """Fit Rs/R0 = a·ppm^b to the digitized datasheet points (log-space fit)."""
    coeffs = np.polyfit(np.log(DATASHEET_PPM), np.log(DATASHEET_RS_R0), 1)
    b0, log_a0 = coeffs[0], coeffs[1]
    (a, b), _ = curve_fit(
        lambda x, la, bb: la + bb * x,
        np.log(DATASHEET_PPM), np.log(DATASHEET_RS_R0), p0=(log_a0, b0),
    )
    return float(np.exp(a)), float(b)


A_COEF, B_EXP = fit_datasheet_curve()

# Below this concentration the model would exceed the clean-air line; clamp there.
PPM_CLEAN_FLOOR = (CLEAN_AIR_RATIO / A_COEF) ** (1.0 / B_EXP)


def ppm_to_ratio(ppm):
    """Rs/R0 for a given ethanol concentration [ppm], clamped at the air line."""
    ppm = np.asarray(ppm, dtype=float)
    safe = np.maximum(ppm, 1e-9)
    return np.minimum(CLEAN_AIR_RATIO, _power_law(safe, A_COEF, B_EXP))


def ppm_to_rs(ppm):
    """Sensing resistance Rs [Ω]."""
    return ppm_to_ratio(ppm) * R0


def rs_to_vout(rs):
    """Voltage-divider output: Vout = VCC·RL / (Rs + RL)."""
    return VCC * RL / (np.asarray(rs, dtype=float) + RL)


def ppm_to_vout(ppm):
    return rs_to_vout(ppm_to_rs(ppm))


def vout_to_adc(vout):
    """10-bit ADC reading: ADC = Vout · 1023 / 5.0."""
    return np.round(np.asarray(vout, dtype=float) * ADC_MAX / VCC).astype(int)


def concentration_profile(t, ppm_peak, rise_time, duration, noise=True, seed=42):
    """Breath-alcohol exposure scenario: clean air → smooth rise → plateau → decay."""
    t = np.asarray(t, dtype=float)
    t_start = 0.10 * duration
    t_plateau_end = 0.55 * duration
    tau = 0.10 * duration

    ppm = np.zeros_like(t)
    rising = (t >= t_start) & (t < t_start + rise_time)
    x = (t[rising] - t_start) / rise_time
    ppm[rising] = ppm_peak * (3 * x**2 - 2 * x**3)          # smoothstep rise
    hold = (t >= t_start + rise_time) & (t < t_plateau_end)
    ppm[hold] = ppm_peak
    decay = t >= t_plateau_end
    ppm[decay] = ppm_peak * np.exp(-(t[decay] - t_plateau_end) / tau)

    if noise:
        # Multiplicative noise: the Rs/R0 power law is very steep near 0 ppm, so
        # absolute noise there would swing the ratio wildly; scale noise with ppm.
        rng = np.random.default_rng(seed)
        ppm = np.maximum(0.0, ppm * (1.0 + 0.02 * rng.standard_normal(t.shape)))
    return ppm


def engine_state(ppm, threshold):
    """Ignition-interlock state: 1 = engine enabled, 0 = blocked.

    Blocks at ppm ≥ threshold, re-arms only below 80 % of it (hysteresis).
    """
    state = np.ones(len(ppm), dtype=int)
    blocked = False
    for i, c in enumerate(ppm):
        if not blocked and c >= threshold:
            blocked = True
        elif blocked and c < 0.8 * threshold:
            blocked = False
        state[i] = 0 if blocked else 1
    return state


# ----------------------------------------------------------------------------
# Plot styling (light-mode, print-friendly)
# ----------------------------------------------------------------------------
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#c3c2b7"
SURFACE = "#fcfcfb"
SERIES_BLUE = "#2a78d6"
SERIES_ORANGE = "#eb6834"
STATUS_GOOD = "#0ca30c"
STATUS_CRIT = "#d03b3b"


def apply_style(plt):
    plt.rcParams.update({
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.6,
        "grid.alpha": 0.8,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": INK_2,
        "ytick.labelcolor": INK_2,
        "font.family": "sans-serif",
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.labelsize": 11.5,
        "legend.frameon": False,
        "figure.dpi": 110,
        "savefig.dpi": 160,
        "savefig.bbox": "tight",
    })


# ----------------------------------------------------------------------------
# Plot 1 — sensitivity curve
# ----------------------------------------------------------------------------
def plot_sensitivity_curve(plt, threshold, outpath, keep=False):
    fig, ax = plt.subplots(figsize=(9, 6.5))
    ppm = np.logspace(1, 4, 400)

    ax.plot(ppm, _power_law(ppm, A_COEF, B_EXP), color=SERIES_BLUE, lw=2,
            label=f"Power-law fit  Rs/R0 = {A_COEF:.2f}·ppm^{B_EXP:.3f}", zorder=3)
    ax.scatter(DATASHEET_PPM, DATASHEET_RS_R0, s=64, facecolor=SERIES_ORANGE,
               edgecolor=SURFACE, linewidth=1.5, zorder=4,
               label="Hanwei datasheet Fig. 2 (digitized)")

    ax.axhline(CLEAN_AIR_RATIO, color=MUTED, ls=":", lw=1.4)
    ax.text(11, CLEAN_AIR_RATIO * 1.12, f"clean air  Rs/R0 ≈ {CLEAN_AIR_RATIO:.0f}",
            color=INK_2, fontsize=10)

    ax.axvline(threshold, color=STATUS_CRIT, ls="--", lw=1.6)
    ratio_thr = float(ppm_to_ratio(threshold))
    ax.plot([threshold], [ratio_thr], "o", ms=9, color=STATUS_CRIT, zorder=5)
    ax.annotate(
        f"⚠ legal limit\n{threshold:.0f} ppm ≈ {LEGAL_LIMIT_MG_L} mg/L BrAC\n"
        f"(StVO §24a) → Rs/R0 = {ratio_thr:.2f}",
        xy=(threshold, ratio_thr), xytext=(threshold * 2.6, ratio_thr * 3.2),
        color=INK, fontsize=10.5,
        arrowprops=dict(arrowstyle="->", color=INK_2, lw=1.2))

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(10, 10_000)
    ax.set_ylim(0.05, 100)
    ax.grid(True, which="both", lw=0.5, alpha=0.6)
    ax.set_xlabel("Ethanol concentration [ppm]")
    ax.set_ylabel("Rs / R0   (R0 @ 0.4 mg/L alcohol)")
    ax.set_title("MQ-3 sensitivity characteristic — ethanol (datasheet Fig. 2 + regression)")

    sec = ax.secondary_xaxis("top", functions=(lambda p: p / MG_L_TO_PPM,
                                               lambda m: m * MG_L_TO_PPM))
    sec.set_xlabel("Breath-alcohol equivalent [mg/L]  (0.4 mg/L ≈ 200 ppm, per datasheet)",
                   fontsize=10.5, color=INK_2)
    sec.tick_params(labelcolor=INK_2, color=MUTED)

    ax.legend(loc="lower left", fontsize=10.5)
    fig.savefig(outpath)
    if not keep:
        plt.close(fig)


# ----------------------------------------------------------------------------
# Plot 2 — time-domain simulation
# ----------------------------------------------------------------------------
def plot_time_simulation(plt, t, ppm, threshold, outpath, keep=False):
    vout = ppm_to_vout(ppm)
    adc = vout_to_adc(vout)
    v_thr = float(ppm_to_vout(threshold))
    adc_thr = int(vout_to_adc(v_thr))
    state = engine_state(ppm, threshold)

    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True,
                             gridspec_kw={"height_ratios": [3, 3, 3, 1.4]})
    ax1, ax2, ax3, ax4 = axes

    ax1.plot(t, ppm, color=SERIES_BLUE, lw=2)
    ax1.axhline(threshold, color=STATUS_CRIT, ls="--", lw=1.4)
    ax1.fill_between(t, 0, ppm, where=ppm >= threshold,
                     color=STATUS_CRIT, alpha=0.15, lw=0)
    ax1.text(t[-1] * 0.995, threshold, f" threshold {threshold:.0f} ppm",
             color=STATUS_CRIT, fontsize=9.5, ha="right", va="bottom")
    ax1.set_ylabel("Ethanol [ppm]")
    ax1.set_title("MQ-3 time-domain simulation — breath-alcohol exposure scenario")

    ax2.plot(t, vout, color=SERIES_BLUE, lw=2)
    ax2.axhline(v_thr, color=STATUS_CRIT, ls="--", lw=1.4)
    ax2.fill_between(t, v_thr, vout, where=vout >= v_thr,
                     color=STATUS_CRIT, alpha=0.15, lw=0)
    ax2.text(t[-1] * 0.995, v_thr, f" Vout(threshold) = {v_thr:.2f} V",
             color=STATUS_CRIT, fontsize=9.5, ha="right", va="bottom")
    ax2.set_ylabel("Vout [V]")
    ax2.set_ylim(0, VCC)

    ax3.plot(t, adc, color=SERIES_BLUE, lw=2)
    ax3.axhline(adc_thr, color=STATUS_CRIT, ls="--", lw=1.4)
    ax3.text(t[-1] * 0.995, adc_thr, f" ADC threshold = {adc_thr}",
             color=STATUS_CRIT, fontsize=9.5, ha="right", va="bottom")
    ax3.set_ylabel("ADC (10-bit)")
    ax3.set_ylim(0, ADC_MAX)

    ax4.fill_between(t, 0, 1, where=state == 1, step="post",
                     color=STATUS_GOOD, alpha=0.75, lw=0)
    ax4.fill_between(t, 0, 1, where=state == 0, step="post",
                     color=STATUS_CRIT, alpha=0.8, lw=0)
    for seg_state, label in ((1, "ENGINE ON"), (0, "ENGINE OFF — INTERLOCK")):
        idx = np.where(state == seg_state)[0]
        if len(idx):
            splits = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
            seg = max(splits, key=len)
            ax4.text(t[seg].mean(), 0.5, label, ha="center", va="center",
                     color="white", fontsize=10, fontweight="bold")
    ax4.set_ylim(0, 1)
    ax4.set_yticks([])
    ax4.grid(False)
    ax4.set_ylabel("Engine")
    ax4.set_xlabel("Time [s]")
    ax4.set_xlim(t[0], t[-1])

    fig.align_ylabels(axes)
    fig.savefig(outpath)
    if not keep:
        plt.close(fig)


# ----------------------------------------------------------------------------
# Plot 3 — voltage-divider analysis (schematic + transfer + sensitivity)
# ----------------------------------------------------------------------------
def _draw_schematic(ax):
    from matplotlib.patches import Rectangle, Circle

    ax.set_xlim(0, 10)
    ax.set_ylim(0, 14)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Measuring circuit (datasheet Fig. 2)", fontsize=12)
    wire = dict(color=INK, lw=1.8)
    x = 4.0

    ax.plot([x, x], [12.2, 13.0], **wire)
    ax.plot([x - 0.5, x + 0.5], [13.0, 13.0], **wire)
    ax.text(x, 13.35, f"VCC = {VCC:.0f} V", ha="center", color=INK, fontsize=11)

    ax.add_patch(Rectangle((x - 1.1, 9.2), 2.2, 3.0, fill=True,
                           facecolor="#e8f0fb", edgecolor=SERIES_BLUE, lw=1.8))
    ax.text(x, 11.05, "MQ-3", ha="center", color=INK, fontsize=11, fontweight="bold")
    ax.text(x, 10.35, "Rs (SnO2)", ha="center", color=INK_2, fontsize=9.5)
    ax.text(x, 9.7, "1–8 MΩ @0.4 mg/L", ha="center", color=INK_2, fontsize=8)

    ax.plot([x, x], [7.6, 9.2], **wire)
    ax.add_patch(Circle((x, 7.6), 0.13, color=INK, zorder=5))
    ax.plot([x, 7.4], [7.6, 7.6], **wire)
    ax.annotate("Vout → ADC (10-bit)", xy=(7.4, 7.6), xytext=(7.55, 7.6),
                color=INK, fontsize=10.5, va="center")
    ax.text(7.55, 6.9, r"Vout = VCC·RL/(Rs+RL)", color=INK_2, fontsize=9)

    ax.add_patch(Rectangle((x - 0.75, 4.4), 1.5, 2.6, fill=True,
                           facecolor="#fdeee7", edgecolor=SERIES_ORANGE, lw=1.8))
    ax.text(x, 5.95, "RL", ha="center", color=INK, fontsize=11, fontweight="bold")
    ax.text(x, 5.2, f"{RL/1000:.0f} kΩ", ha="center", color=INK_2, fontsize=9.5)

    ax.plot([x, x], [3.2, 4.4], **wire)
    for i, w in enumerate((0.9, 0.6, 0.3)):
        ax.plot([x - w, x + w], [3.2 - i * 0.35, 3.2 - i * 0.35], **wire)
    ax.text(x + 1.3, 2.7, "GND", color=INK_2, fontsize=10)

    ax.text(0.3, 1.3, f"Heater: VH = {V_HEATER:.0f} V, RH = {R_HEATER:.0f} Ω ±5%,\n"
                      f"PH < {P_HEATER_MAX*1000:.0f} mW, preheat > {PREHEAT_HOURS} h",
            color=INK_2, fontsize=9)


def plot_divider_analysis(plt, threshold, outpath, keep=False):
    fig, (ax_s, ax_v, ax_d) = plt.subplots(
        1, 3, figsize=(15, 5.4), gridspec_kw={"width_ratios": [1.1, 1.3, 1.3]})

    _draw_schematic(ax_s)

    ppm = np.linspace(0.1, 500, 800)
    vout = ppm_to_vout(ppm)
    v_thr = float(ppm_to_vout(threshold))

    ax_v.plot(ppm, vout, color=SERIES_BLUE, lw=2)
    ax_v.axvline(threshold, color=STATUS_CRIT, ls="--", lw=1.4)
    ax_v.plot([threshold], [v_thr], "o", color=STATUS_CRIT, ms=8, zorder=5)
    ax_v.annotate(f"{threshold:.0f} ppm → {v_thr:.2f} V",
                  xy=(threshold, v_thr), xytext=(threshold + 90, v_thr - 1.1),
                  color=INK, fontsize=10.5,
                  arrowprops=dict(arrowstyle="->", color=INK_2, lw=1.2))
    ax_v.set_xlabel("Ethanol [ppm]")
    ax_v.set_ylabel("Vout [V]")
    ax_v.set_ylim(0, VCC)
    ax_v.set_title("Static transfer curve Vout(ppm)")

    ppm_s = np.logspace(0, np.log10(500), 400)
    dv = np.gradient(ppm_to_vout(ppm_s), ppm_s)
    ax_d.plot(ppm_s, dv * 1000, color=SERIES_ORANGE, lw=2)
    dv_thr = float(np.interp(threshold, ppm_s, dv)) * 1000
    ax_d.plot([threshold], [dv_thr], "o", color=STATUS_CRIT, ms=8, zorder=5)
    ax_d.annotate(f"at {threshold:.0f} ppm:\n{dv_thr:.1f} mV/ppm",
                  xy=(threshold, dv_thr), xytext=(threshold * 2.2, dv_thr * 3.5),
                  color=INK, fontsize=10.5,
                  arrowprops=dict(arrowstyle="->", color=INK_2, lw=1.2))
    ax_d.axvline(threshold, color=STATUS_CRIT, ls="--", lw=1.2)
    ax_d.set_xscale("log")
    ax_d.set_yscale("log")
    ax_d.grid(True, which="both", lw=0.5, alpha=0.6)
    ax_d.set_xlabel("Ethanol [ppm]")
    ax_d.set_ylabel("dVout/dppm [mV/ppm]")
    ax_d.set_title("Divider sensitivity dVout/dppm (log-log)\n"
                   "highest at low ppm — ideal for a breath-alcohol threshold",
                   fontsize=11.5)

    fig.suptitle("MQ-3 voltage-divider analysis", fontsize=14, y=1.02)
    fig.tight_layout()
    fig.savefig(outpath)
    if not keep:
        plt.close(fig)


# ----------------------------------------------------------------------------
# Serial-style console output
# ----------------------------------------------------------------------------
def print_serial_log(t, ppm, threshold, duration):
    ratio = ppm_to_ratio(ppm)
    vout = ppm_to_vout(ppm)
    adc = vout_to_adc(vout)

    bar = "=" * 40
    print(bar)
    print("  MQ-3 ALCOHOL DETECTION SIMULATION")
    print(f"  Threshold: {threshold:g} ppm | Legal limit: {LEGAL_LIMIT_MG_L} mg/L")
    print(bar)

    step = max(1.0, round(duration / 20))
    next_print = 0.0
    for i, ti in enumerate(t):
        if ti + 1e-9 < next_print and i != len(t) - 1:
            continue
        next_print += step
        status = "✗ ALCOHOL DETECTED" if ppm[i] >= threshold else "✓ CLEAR"
        print(f"[t={ti:04.1f}s] PPM: {ppm[i]:5.1f} | Rs/R0: {ratio[i]:5.2f} | "
              f"Vout: {vout[i]:.2f}V | ADC: {adc[i]:4d} | {status}")

    print(bar)
    above = ppm >= threshold
    if above.any():
        t_first = t[np.argmax(above)]
        dt = t[1] - t[0]
        print(f"  SUMMARY: Detected at t={t_first:.1f}s | "
              f"Duration: {above.sum() * dt:.1f}s")
    else:
        print("  SUMMARY: No alcohol above threshold detected")
    print(bar)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="MQ-3 alcohol sensor simulation (Hanwei datasheet physics)")
    p.add_argument("--ppm-peak", type=float, default=200.0,
                   help="Peak alcohol concentration in ppm (default: 200)")
    p.add_argument("--duration", type=float, default=60.0,
                   help="Total simulation time in seconds (default: 60)")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD_PPM,
                   help="Detection threshold in ppm (default: 50)")
    p.add_argument("--rise-time", type=float, default=10.0,
                   help="Time for concentration to rise in seconds (default: 10)")
    p.add_argument("--output-dir", default="results",
                   help="Where to save plots (default: results/)")
    p.add_argument("--show", action="store_true",
                   help="Display plots interactively after saving")
    return p.parse_args(argv)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)

    import matplotlib
    if not args.show:
        matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    apply_style(plt)

    os.makedirs(args.output_dir, exist_ok=True)

    t = np.arange(0.0, args.duration + 1e-9, 0.1)
    ppm = concentration_profile(t, args.ppm_peak, args.rise_time, args.duration)

    print_serial_log(t, ppm, args.threshold, args.duration)
    a_inv = (A_COEF ** (-1.0 / B_EXP)) / MG_L_TO_PPM   # mg/L = a_inv·(Rs/R0)^(1/b)
    print(f"\nRegression fit to datasheet Fig. 2:  Rs/R0 = "
          f"{A_COEF:.2f} · ppm^({B_EXP:.3f})   "
          f"(inverse form: mg/L ≈ {a_inv:.4g} · (Rs/R0)^({1 / B_EXP:.3f}), "
          f"cf. commonly quoted 0.3934·(Rs/R0)^-1.504)")

    p1 = os.path.join(args.output_dir, "mq3_sensitivity_curve.png")
    p2 = os.path.join(args.output_dir, "mq3_time_simulation.png")
    p3 = os.path.join(args.output_dir, "mq3_voltage_divider_analysis.png")
    plot_sensitivity_curve(plt, args.threshold, p1, keep=args.show)
    plot_time_simulation(plt, t, ppm, args.threshold, p2, keep=args.show)
    plot_divider_analysis(plt, args.threshold, p3, keep=args.show)
    for p in (p1, p2, p3):
        print(f"Saved: {p}")

    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
