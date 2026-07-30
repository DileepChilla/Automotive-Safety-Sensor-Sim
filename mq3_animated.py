#!/usr/bin/env python3
"""
MQ-3 Alcohol Gas Sensor — real-time animated simulation.

Live dashboard built with matplotlib FuncAnimation:
  * Left panel   — alcohol "vapor cloud" that grows and shifts green → red
  * Right panels — live-scrolling ppm and ADC traces
  * Bottom bar   — large CLEAR / ALCOHOL DETECTED status indicator
  * Relay-click beep when the detection threshold is crossed
  * Saves the first 30 frames to results/mq3_animation.gif

Sensor physics is imported from mq3_simulation.py, which implements the
Hanwei Electronics MQ-3 datasheet (see that file's docstring for the source
and all quoted constants).

Usage:
    python mq3_animated.py            # live window + GIF export
    MPLBACKEND=Agg python mq3_animated.py   # headless: GIF export only
"""

import os
import sys

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, PillowWriter
from matplotlib.patches import Circle

from mq3_simulation import (
    ADC_MAX, CLEAN_AIR_RATIO, DEFAULT_THRESHOLD_PPM, INK, INK_2, MUTED,
    SERIES_BLUE, STATUS_CRIT, STATUS_GOOD, SURFACE, VCC, apply_style,
    concentration_profile, engine_state, ppm_to_ratio, ppm_to_vout, vout_to_adc,
)

# Scenario (chosen so the threshold crossing lands inside the 30-frame GIF)
DURATION = 40.0        # s
DT = 0.2               # s per frame
PPM_PEAK = 200.0
RISE_TIME = 6.0
THRESHOLD = DEFAULT_THRESHOLD_PPM
SCROLL_WINDOW = 12.0   # s of history shown in the scrolling plots
GIF_FRAMES = 30
RESULTS_DIR = "results"


def relay_click():
    """Audible 'relay click' on threshold crossing; silently ignored if no audio."""
    try:
        if sys.platform == "win32":
            import winsound
            winsound.Beep(2200, 90)
        else:
            os.system("play -nq -t alsa synth 0.08 sine 2200 2>/dev/null &")
    except Exception:
        pass


def cloud_color(ppm):
    """Interpolate green → red as concentration approaches 2× threshold."""
    x = float(np.clip(ppm / (2.0 * THRESHOLD), 0.0, 1.0))
    good = np.array([0x0C, 0xA3, 0x0C]) / 255.0     # status good
    crit = np.array([0xD0, 0x3B, 0x3B]) / 255.0     # status critical
    return tuple((1 - x) * good + x * crit)


def build_dashboard():
    apply_style(plt)
    fig = plt.figure(figsize=(12, 7))
    fig.suptitle("MQ-3 Alcohol Sensor — Live Simulation (Hanwei datasheet physics)",
                 fontsize=14, color=INK)
    gs = fig.add_gridspec(3, 2, width_ratios=[1.1, 1.6],
                          height_ratios=[1, 1, 0.35], hspace=0.45, wspace=0.25)

    # -- left: concentration cloud ------------------------------------------
    ax_cloud = fig.add_subplot(gs[0:2, 0])
    ax_cloud.set_xlim(-5, 5)
    ax_cloud.set_ylim(-5, 5)
    ax_cloud.set_aspect("equal")
    ax_cloud.axis("off")
    ax_cloud.set_title("Cabin alcohol vapor", fontsize=12, color=INK)
    halo = Circle((0, 0), 0.6, color=STATUS_GOOD, alpha=0.18)
    cloud = Circle((0, 0), 0.4, color=STATUS_GOOD, alpha=0.55)
    ax_cloud.add_patch(halo)
    ax_cloud.add_patch(cloud)
    cloud_txt = ax_cloud.text(0, -4.4, "0 ppm", ha="center", fontsize=13,
                              color=INK, fontweight="bold")

    # -- right: scrolling ppm + ADC -----------------------------------------
    ax_ppm = fig.add_subplot(gs[0, 1])
    ax_ppm.set_ylabel("Ethanol [ppm]")
    ax_ppm.set_ylim(0, PPM_PEAK * 1.15)
    ax_ppm.axhline(THRESHOLD, color=STATUS_CRIT, ls="--", lw=1.2)
    ax_ppm.text(0.99, THRESHOLD / (PPM_PEAK * 1.15) + 0.02,
                f"threshold {THRESHOLD:.0f} ppm", transform=ax_ppm.transAxes,
                ha="right", color=STATUS_CRIT, fontsize=9)
    line_ppm, = ax_ppm.plot([], [], color=SERIES_BLUE, lw=2)

    ax_adc = fig.add_subplot(gs[1, 1], sharex=ax_ppm)
    ax_adc.set_ylabel("ADC (10-bit)")
    ax_adc.set_xlabel("Time [s]")
    ax_adc.set_ylim(0, ADC_MAX)
    adc_thr = int(vout_to_adc(ppm_to_vout(THRESHOLD)))
    ax_adc.axhline(adc_thr, color=STATUS_CRIT, ls="--", lw=1.2)
    line_adc, = ax_adc.plot([], [], color=SERIES_BLUE, lw=2)

    # -- bottom: status indicator -------------------------------------------
    ax_status = fig.add_subplot(gs[2, :])
    ax_status.set_xlim(0, 1)
    ax_status.set_ylim(0, 1)
    ax_status.axis("off")
    # axis("off") also hides the axes background patch, so draw our own banner
    from matplotlib.patches import Rectangle
    status_bg = Rectangle((0, 0), 1, 1, transform=ax_status.transAxes,
                          facecolor=STATUS_GOOD, zorder=1)
    ax_status.add_patch(status_bg)
    status_txt = ax_status.text(0.5, 0.5, "", ha="center", va="center",
                                fontsize=22, fontweight="bold", color="white",
                                zorder=2)

    artists = dict(fig=fig, halo=halo, cloud=cloud, cloud_txt=cloud_txt,
                   ax_ppm=ax_ppm, ax_adc=ax_adc, line_ppm=line_ppm,
                   line_adc=line_adc, status_bg=status_bg, status_txt=status_txt)
    return fig, artists


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    os.makedirs(RESULTS_DIR, exist_ok=True)

    t = np.arange(0.0, DURATION + 1e-9, DT)
    ppm = concentration_profile(t, PPM_PEAK, RISE_TIME, DURATION)
    vout = ppm_to_vout(ppm)
    adc = vout_to_adc(vout)
    state = engine_state(ppm, THRESHOLD)
    detected = ppm >= THRESHOLD

    fig, art = build_dashboard()
    sound_enabled = {"live": False}
    prev_detected = {"v": False}

    def update(frame):
        i = min(frame, len(t) - 1)
        ti = t[i]

        # cloud: radius grows with concentration, hue shifts green → red
        r = 0.4 + 3.6 * min(1.0, ppm[i] / PPM_PEAK)
        col = cloud_color(ppm[i])
        art["cloud"].set_radius(r)
        art["cloud"].set_color(col)
        art["halo"].set_radius(min(4.8, r * 1.35))
        art["halo"].set_color(col)
        art["cloud_txt"].set_text(
            f"{ppm[i]:.0f} ppm   Rs/R0 = {float(ppm_to_ratio(ppm[i])):.2f}")

        lo = max(0.0, ti - SCROLL_WINDOW)
        hi = max(SCROLL_WINDOW, ti)
        win = (t >= lo) & (t <= ti)
        art["line_ppm"].set_data(t[win], ppm[win])
        art["line_adc"].set_data(t[win], adc[win])
        art["ax_ppm"].set_xlim(lo, hi)

        if detected[i]:
            art["status_bg"].set_facecolor(STATUS_CRIT)
            art["status_txt"].set_text(
                f"✗ ALCOHOL DETECTED — ENGINE LOCKED   ({vout[i]:.2f} V)")
        else:
            art["status_bg"].set_facecolor(STATUS_GOOD)
            eng = "ENGINE ENABLED" if state[i] == 1 else "RE-ARMING"
            art["status_txt"].set_text(f"✓ CLEAR — {eng}   ({vout[i]:.2f} V)")

        # relay click on the rising edge of detection (live playback only)
        if detected[i] and not prev_detected["v"] and sound_enabled["live"]:
            relay_click()
        prev_detected["v"] = bool(detected[i])

        return (art["cloud"], art["halo"], art["cloud_txt"],
                art["line_ppm"], art["line_adc"], art["status_bg"],
                art["status_txt"])

    # 1) Export the first 30 frames as a GIF
    gif_path = os.path.join(RESULTS_DIR, "mq3_animation.gif")
    ani_save = FuncAnimation(fig, update, frames=GIF_FRAMES, interval=100)
    ani_save.save(gif_path, writer=PillowWriter(fps=10), dpi=80)
    print(f"Saved: {gif_path} ({GIF_FRAMES} frames)")

    # 2) Live playback (skipped automatically on a non-interactive backend)
    if matplotlib.get_backend().lower().startswith("agg"):
        print("Non-interactive backend — skipping live window.")
        return

    prev_detected["v"] = False
    sound_enabled["live"] = True
    ani_live = FuncAnimation(fig, update, frames=len(t), interval=int(DT * 1000),
                             repeat=True)
    plt.show()
    # keep a reference so the animation isn't garbage-collected
    _ = ani_live


if __name__ == "__main__":
    main()
