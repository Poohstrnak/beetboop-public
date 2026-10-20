"""
Renders glucose visuals as PNG images, styled to sit reasonably well inside
a Discord embed, in the user's choice of `THEMES` (dark by default):

  - `render_glucose_graph`: a trend line + stats panel for a recent window.
  - `render_agp_graph`: an Ambulatory Glucose Profile (percentile bands by
    time of day) over several days.
  - `render_rolling_averages_graph`: 7/14/30-day rolling mean glucose over
    a longer period.
  - `render_time_in_range_image`: a "Glucose" summary card + a "Time in
    Range" breakdown card, in the style of typical CGM clinical reports
    (Dexcom Clarity / LibreView / AGP reports).

All are pure/CPU-bound (matplotlib) and should be called via
`asyncio.to_thread` / an executor from async code so they don't block the
bot's event loop.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")  # headless, no display needed

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.collections import LineCollection
from matplotlib.font_manager import FontProperties
from matplotlib.patches import FancyBboxPatch, Rectangle
from matplotlib.textpath import TextPath
from matplotlib.transforms import IdentityTransform
import numpy as np

from nightscout_client import direction_to_arrow

MGDL_PER_MMOL = 18.0182

# Default "very low" / "very high" thresholds, for callers that don't pass a
# user's own. Users configure theirs with `/ns thresholds`.
DEFAULT_VERY_LOW_MGDL = 55.0
DEFAULT_VERY_HIGH_MGDL = 250.0

# `render_glucose_graph`'s optional fixed y-axis range. 400 mg/dL is the top
# of most CGMs' reportable range and 40 mg/dL is close to the bottom, so
# pinning both (instead of scaling to whatever the data happens to span)
# keeps the same reading at the same visual height across different graphs.
GRAPH_FIXED_Y_AXIS_MAX_MGDL = 400.0
GRAPH_FIXED_Y_AXIS_MIN_MGDL = 40.0

@dataclass(frozen=True)
class Theme:
    """The surfaces and ink every graph is drawn on/with (everything except
    the range colors). Users pick one with /ns theme."""
    bg: str         # chart surface
    panel: str      # card surface, one step off the chart surface
    text: str       # primary ink
    muted: str      # secondary ink
    dim: str        # muted ink (axis ticks/labels)
    grid: str       # hairline gridlines
    baseline: str   # axis/spine
    neutral: str    # the uncolored line on the rolling averages graph


# Dark (the default) is taken from a validated dark palette so text and
# gridlines stay legible against the Discord embed; the rest keep the same
# steps between surfaces and ink.
THEMES = {
    "dark": Theme(bg="#1a1a19", panel="#232322", text="#ffffff", muted="#c3c2b7",
                  dim="#898781", grid="#2c2c2a", baseline="#383835", neutral="#e5e4dc"),
    "light": Theme(bg="#ffffff", panel="#f3f3f0", text="#1a1a19", muted="#4f4e49",
                   dim="#74736d", grid="#e8e7e2", baseline="#d4d3cd", neutral="#55544f"),
    "oled": Theme(bg="#000000", panel="#111111", text="#ffffff", muted="#c3c2b7",
                  dim="#8a8983", grid="#1d1d1c", baseline="#2c2c2a", neutral="#e5e4dc"),
    "midnight": Theme(bg="#0f172a", panel="#18223a", text="#f8fafc", muted="#cbd5e1",
                      dim="#8b9ab3", grid="#1e293b", baseline="#334155", neutral="#e2e8f0"),
    "discord": Theme(bg="#313338", panel="#2b2d31", text="#f2f3f5", muted="#b5bac1",
                     dim="#949ba4", grid="#3c3e44", baseline="#4e5058", neutral="#e3e5e8"),
}
THEME_NAMES = {
    "dark": "Dark",
    "light": "Light",
    "oled": "OLED black",
    "midnight": "Midnight blue",
    "discord": "Discord",
}
DEFAULT_THEME = THEMES["dark"]

@dataclass(frozen=True)
class RangeColors:
    """The 5-tier time-in-range palette (Very Low -> Very High). The defaults
    match the color conventions used by most CGM clinical reports; users can
    pick their own for any range with /ns colors."""
    very_low: str = "#a91d1d"
    low: str = "#e2574c"
    in_range: str = "#5da130"
    high: str = "#f2c94c"
    very_high: str = "#e6791e"


DEFAULT_COLORS = RangeColors()

# Used instead of the user's colors when /ns colorblind is on. Lows are reds
# (deepest for very low), highs are blues (deepest for very high), in range is
# neutral, and lightness steps between neighbouring ranges, so every pair stays
# distinct under simulated protanopia, deuteranopia and tritanopia (unlike the
# default red/green set).
COLORBLIND_COLORS = RangeColors(
    very_low="#c8302c",
    low="#f4a582",
    in_range="#ecebe4",
    high="#93c5f5",
    very_high="#3a68e0",
)

# Named choices offered by /ns colors (any hex code works too).
COLOR_PRESETS = {
    "Green": "#5da130",
    "Sky blue": "#87ceeb",
    "Blue": "#3b8beb",
    "Teal": "#2dd4bf",
    "Mint": "#6ee7b7",
    "Lime": "#a3e635",
    "Yellow": "#f2c94c",
    "Orange": "#e6791e",
    "Red": "#e2574c",
    "Dark red": "#a91d1d",
    "Purple": "#a78bfa",
    "Pink": "#f472b6",
    "White": "#e5e4dc",
}

# Colors darker than this (relative luminance, 0-1) would all but vanish
# against the dark chart background, so /ns colors refuses them.
MIN_COLOR_LUMINANCE = 0.06


def parse_color(text: str) -> Optional[str]:
    """
    A preset name (any case) or a hex code ("#87ceeb", "87ceeb", "#8ce")
    to a normalized "#rrggbb" string, or None if it's neither.
    """
    text = text.strip()
    for name, hex_code in COLOR_PRESETS.items():
        if text.lower() == name.lower():
            return hex_code
    digits = text.removeprefix("#").lower()
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    if len(digits) == 6 and all(c in "0123456789abcdef" for c in digits):
        return f"#{digits}"
    return None


def color_luminance(hex_code: str) -> float:
    """WCAG relative luminance of a "#rrggbb" color: 0 (black) to 1 (white)."""
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_code[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)



# Font for the trend graph (`/ns graph`), falling back to matplotlib's default
# if Noto Sans isn't installed on the host.
GRAPH_RC = {"font.family": ["Noto Sans", "DejaVu Sans"]}


def mgdl_to_units(value_mgdl: float, units: str) -> float:
    if units == "mmol/L":
        return value_mgdl / MGDL_PER_MMOL
    return value_mgdl


def format_value(value_mgdl: float, units: str) -> str:
    converted = mgdl_to_units(value_mgdl, units)
    return f"{converted:.1f}" if units == "mmol/L" else f"{converted:.0f}"


def format_delta(delta_mgdl: float, units: str) -> str:
    """Same formatting as `format_value`, but always signed (+/-) -- for
    showing the change since the previous reading, e.g. "+0.2" or "-3"."""
    converted = mgdl_to_units(delta_mgdl, units)
    return f"{converted:+.1f}" if units == "mmol/L" else f"{converted:+.0f}"


def format_time_span(hours: int) -> str:
    """e.g. "Last hour", "Last 12 hours", "Last 24 hours", "Last 7 days"."""
    if hours == 1:
        return "Last hour"
    if hours > 24 and hours % 24 == 0:
        return f"Last {hours // 24} days"
    return f"Last {hours} hours"


def compute_gmi(average_mgdl: float) -> float:
    """
    Glucose Management Indicator (Bergenstal et al. 2018) -- the modern,
    CGM-specific replacement for the old eAG-derived A1C estimate. Returns
    a percentage, e.g. 6.9.
    """
    return 3.31 + 0.02392 * average_mgdl


@dataclass
class GlucoseStats:
    average_mgdl: float
    std_mgdl: float
    time_in_range_pct: float
    # Low / high exclude the very low / very high readings, so the five
    # percentages add up to 100.
    time_very_low_pct: float
    time_below_pct: float
    time_above_pct: float
    time_very_high_pct: float
    latest_mgdl: float
    latest_time: datetime


@dataclass
class TimeInRangeStats:
    average_mgdl: float
    std_mgdl: float
    gmi_pct: float
    very_low_pct: float
    low_pct: float
    in_range_pct: float
    high_pct: float
    very_high_pct: float
    reading_count: int


class NoDataError(Exception):
    """Raised when there are no usable entries to graph."""


def filter_entries_within_hours(
    entries: list[dict], hours: int, tz: ZoneInfo
) -> tuple[list[datetime], list[float]]:
    """
    Sorts `entries` oldest -> newest, keeps only those within `hours` of the
    most recent entry, and returns parallel (times, values_mgdl) lists.
    """
    ordered = sorted(entries, key=lambda e: e["date"])
    cutoff_ms = ordered[-1]["date"] - hours * 3600 * 1000 if ordered else 0
    ordered = [e for e in ordered if e["date"] >= cutoff_ms]

    if not ordered:
        raise NoDataError("No glucose readings in the requested window.")

    times = [datetime.fromtimestamp(e["date"] / 1000, tz=tz) for e in ordered]
    values_mgdl = [float(e["sgv"]) for e in ordered]
    return times, values_mgdl


def compute_stats(
    times: list[datetime],
    values_mgdl: list[float],
    low_mgdl: float,
    high_mgdl: float,
    *,
    very_low_mgdl: float = DEFAULT_VERY_LOW_MGDL,
    very_high_mgdl: float = DEFAULT_VERY_HIGH_MGDL,
) -> GlucoseStats:
    # Same five bands, and the same edge handling, as `/ns tir`.
    tiers = compute_time_in_range_stats(
        values_mgdl, low_mgdl, high_mgdl,
        very_low_mgdl=very_low_mgdl, very_high_mgdl=very_high_mgdl,
    )
    return GlucoseStats(
        average_mgdl=tiers.average_mgdl,
        std_mgdl=tiers.std_mgdl,
        time_in_range_pct=tiers.in_range_pct,
        time_very_low_pct=tiers.very_low_pct,
        time_below_pct=tiers.low_pct,
        time_above_pct=tiers.high_pct,
        time_very_high_pct=tiers.very_high_pct,
        latest_mgdl=float(values_mgdl[-1]),
        latest_time=times[-1],
    )


def compute_time_in_range_stats(
    values_mgdl: list[float],
    low_mgdl: float,
    high_mgdl: float,
    *,
    very_low_mgdl: float = DEFAULT_VERY_LOW_MGDL,
    very_high_mgdl: float = DEFAULT_VERY_HIGH_MGDL,
) -> TimeInRangeStats:
    """
    5-tier breakdown (Very Low / Low / In Range / High / Very High), plus
    average, std dev, and GMI -- the numbers a typical CGM clinical report
    (Dexcom Clarity, LibreView, AGP) shows for a multi-day period.

    All four band edges are the user's own thresholds. The outer ones are
    widened if needed so the bands never invert (e.g. a very low above low).
    """
    if not values_mgdl:
        raise NoDataError("No glucose readings in the requested window.")

    arr = np.asarray(values_mgdl, dtype=float)
    average = float(arr.mean())

    very_low_bound = min(very_low_mgdl, low_mgdl)
    very_high_bound = max(very_high_mgdl, high_mgdl)

    very_low = arr < very_low_bound
    low = (arr >= very_low_bound) & (arr < low_mgdl)
    in_range = (arr >= low_mgdl) & (arr <= high_mgdl)
    high = (arr > high_mgdl) & (arr <= very_high_bound)
    very_high = arr > very_high_bound

    return TimeInRangeStats(
        average_mgdl=average,
        std_mgdl=float(arr.std()),
        gmi_pct=compute_gmi(average),
        very_low_pct=float(very_low.mean() * 100),
        low_pct=float(low.mean() * 100),
        in_range_pct=float(in_range.mean() * 100),
        high_pct=float(high.mean() * 100),
        very_high_pct=float(very_high.mean() * 100),
        reading_count=len(arr),
    )


def _rounded_card(ax, *, facecolor: str, pad: float = 0.02, rounding: float = 0.06) -> None:
    """Draws a rounded card background filling `ax`, behind everything else."""
    ax.add_patch(
        FancyBboxPatch(
            (pad, pad),
            1 - 2 * pad,
            1 - 2 * pad,
            boxstyle=f"round,pad=0,rounding_size={rounding}",
            transform=ax.transAxes,
            facecolor=facecolor,
            edgecolor="none",
            zorder=0,
        )
    )


def _axes_pixel_height(fig, ax) -> float:
    """The axes' actual rendered height in pixels, from its figure-fraction
    position and the figure's real size/dpi -- exact, not estimated."""
    pos = ax.get_position()
    fig_h_px = fig.get_size_inches()[1] * fig.dpi
    return pos.height * fig_h_px


def _axes_pixel_width(fig, ax) -> float:
    """The axes' actual rendered width in pixels -- see `_axes_pixel_height`."""
    pos = ax.get_position()
    fig_w_px = fig.get_size_inches()[0] * fig.dpi
    return pos.width * fig_w_px


def _text_pixel_height(fig, text_artist) -> float:
    """A text artist's actual rendered height in pixels, via the real font
    renderer -- this is what makes layout correct without having to guess
    font metrics by hand (which is what caused the last version's overlaps)."""
    renderer = fig.canvas.get_renderer()
    return text_artist.get_window_extent(renderer=renderer).height


def _flow_text(fig, ax, x_frac: float, y_px: float, s: str, *, gap_after_px: float = 0.0, **kwargs) -> float:
    """
    Draws `s` with its top at `y_px` pixels below the axes' top edge, and
    returns the y (in the same px-from-top units) for whatever comes next:
    this text's own measured bottom, plus `gap_after_px` of breathing room.
    """
    axes_h = _axes_pixel_height(fig, ax)
    y_frac = 1.0 - (y_px / axes_h)
    text_artist = ax.text(x_frac, y_frac, s, transform=ax.transAxes, va="top", **kwargs)
    return y_px + _text_pixel_height(fig, text_artist) + gap_after_px


def _format_tir_pct(pct: float) -> str:
    """Whole percent, but "<1%" / ">99%" instead of rounding a sliver of
    time to 0 or 100 (the convention CGM reports use)."""
    if 0 < pct < 1:
        return "<1%"
    if 99 < pct < 100:
        return ">99%"
    return f"{pct:.0f}%"


def _vertical_stacked_bar(
    fig, ax, *, left_px: float, top_px: float, width_px: float, height_px: float,
    segments: list[tuple[float, str]], gap_px: float = 4.0, min_segment_px: float = 9.0,
    corner_px: float = 7.0,
) -> None:
    """
    A vertical stacked bar, segments drawn top to bottom, inside a rounded
    outline. Positions are pixels from the axes' top-left corner. Any
    non-zero segment gets at least `min_segment_px` so a <1% sliver stays
    visible; the larger segments give up the difference.
    """
    shown = [(pct, color) for pct, color in segments if pct > 0]
    if not shown:
        return

    usable = height_px - gap_px * (len(shown) - 1)
    heights = [usable * pct / 100 for pct, _ in shown]
    small = [h < min_segment_px for h in heights]
    deficit = sum(min_segment_px - h for h, is_small in zip(heights, small) if is_small)
    large_total = sum(h for h, is_small in zip(heights, small) if not is_small)
    heights = [
        min_segment_px if is_small else h - deficit * h / large_total
        for h, is_small in zip(heights, small)
    ]

    # Work in display pixels so the corners stay circular whatever the axes'
    # aspect ratio.
    bbox = ax.get_window_extent()
    x0 = bbox.x0 + left_px
    top = bbox.y1 - top_px
    to_display = IdentityTransform()

    clip = FancyBboxPatch(
        (x0, top - height_px), width_px, height_px,
        boxstyle=f"round,pad=0,rounding_size={corner_px}",
        transform=to_display, facecolor="none", edgecolor="none",
    )
    ax.add_patch(clip)

    y_top = top
    for (_, color), h in zip(shown, heights):
        rect = Rectangle((x0, y_top - h), width_px, h, transform=to_display,
                         facecolor=color, edgecolor="none", zorder=1)
        ax.add_patch(rect)
        rect.set_clip_path(clip)
        y_top -= h + gap_px


def _draw_tir_card(
    fig, ax, stats: TimeInRangeStats, *, colors: RangeColors = DEFAULT_COLORS,
    subtitle: Optional[str] = None, theme: Theme = DEFAULT_THEME,
) -> float:
    """Draws the Time in Range card -- just a title (plus an optional
    subtitle, e.g. the time span), the vertical bar and the five
    percentages, with no card background -- top-down and returns its total
    content height in pixels."""
    axes_h = _axes_pixel_height(fig, ax)
    axes_w = _axes_pixel_width(fig, ax)

    # Fixed pixel sizes (not fractions of the width) so the bar keeps its
    # look if the image width changes.
    left_px = 54
    bar_width_px = 89
    label_gap_px = 71

    y = _flow_text(fig, ax, left_px / axes_w, 28, "Time in Range",
                    fontsize=21, fontweight="bold", color=theme.text,
                    gap_after_px=10 if subtitle else 24)
    if subtitle:
        y = _flow_text(fig, ax, left_px / axes_w, y, subtitle,
                        fontsize=12, color=theme.muted, gap_after_px=24)

    # Clarity-style vertical stacked bar (Very High on top), with the five
    # labels evenly spaced beside it rather than pinned to their segments --
    # tiny segments would otherwise stack their labels on top of each other.
    bar_height_px = 250
    tiers = [
        (stats.very_high_pct, "Very High", colors.very_high),
        (stats.high_pct, "High", colors.high),
        (stats.in_range_pct, "In Range", colors.in_range),
        (stats.low_pct, "Low", colors.low),
        (stats.very_low_pct, "Very Low", colors.very_low),
    ]
    _vertical_stacked_bar(
        fig, ax, left_px=left_px, top_px=y, width_px=bar_width_px, height_px=bar_height_px,
        segments=[(pct, color) for pct, _, color in tiers],
    )

    label_x = (left_px + bar_width_px + label_gap_px) / axes_w
    row_px = bar_height_px / len(tiers)
    for i, (pct, label, _) in enumerate(tiers):
        is_in_range = label == "In Range"
        center_frac = 1.0 - (y + (i + 0.5) * row_px) / axes_h
        ax.text(
            label_x, center_frac, f"{_format_tir_pct(pct)} {label}",
            transform=ax.transAxes, va="center", ha="left",
            fontsize=22 if is_in_range else 15,
            color=theme.text if is_in_range else theme.muted,
        )
    y += bar_height_px + 6

    return y


def render_time_in_range_image(
    stats: TimeInRangeStats, *, colors: RangeColors = DEFAULT_COLORS,
    subtitle: Optional[str] = None, theme: Theme = DEFAULT_THEME,
) -> io.BytesIO:
    """
    Renders the "Time in Range" card on its own: a vertical stacked bar
    across the five ranges with its percentages beside it.

    Sized in two passes: the first draws the card into an oversized scratch
    axes purely to *measure* how tall it actually renders (matplotlib text
    metrics aren't reliably guessable by hand), then the second builds the
    figure at exactly that height, with no leftover empty space.
    """
    dpi = 150
    fig_width_in = 4.6
    bottom_pad_px = 28
    x_margin_frac = 0.02

    def _new_figure(height_in: float):
        fig = plt.figure(figsize=(fig_width_in, height_in), facecolor=theme.bg, dpi=dpi)
        ax = fig.add_axes([x_margin_frac, 0.0, 1 - 2 * x_margin_frac, 1.0])
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        fig.canvas.draw()
        return fig, ax

    # --- pass 1: measure -----------------------------------------------------
    draft_fig, draft_ax = _new_figure(12)
    content_px = _draw_tir_card(draft_fig, draft_ax, stats, colors=colors,
                                subtitle=subtitle, theme=theme) + bottom_pad_px
    plt.close(draft_fig)

    # --- pass 2: build the real figure, sized exactly to fit -----------------
    fig, ax = _new_figure(content_px / dpi)
    _draw_tir_card(fig, ax, stats, colors=colors, subtitle=subtitle, theme=theme)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


def _draw_very_threshold_lines(
    ax, units: str, very_low_mgdl: float, very_high_mgdl: float, *, colors: RangeColors = DEFAULT_COLORS
) -> None:
    """
    Dotted very low / very high lines. Call it once the y-axis limits are
    final: a line outside the current range is skipped rather than drawn,
    so these never stretch a dynamically scaled axis (unlike the low/high
    lines, which are always meant to be in view).
    """
    bottom, top = ax.get_ylim()
    for value_mgdl, color in ((very_low_mgdl, colors.very_low), (very_high_mgdl, colors.very_high)):
        y = mgdl_to_units(value_mgdl, units)
        if bottom <= y <= top:
            ax.axhline(y, color=color, linestyle=":", linewidth=1.2, alpha=0.85, zorder=1)
    ax.set_ylim(bottom, top)


def render_glucose_graph(
    entries: list[dict],
    *,
    units: str,
    low_mgdl: float,
    high_mgdl: float,
    hours: int,
    tz_name: str = "UTC",
    y_axis_max_mgdl: Optional[float] = None,
    days_mode: bool = False,
    title_name: Optional[str] = None,
    very_low_mgdl: float = DEFAULT_VERY_LOW_MGDL,
    very_high_mgdl: float = DEFAULT_VERY_HIGH_MGDL,
    colors: RangeColors = DEFAULT_COLORS,
    boluses: Optional[list[tuple[int, float]]] = None,
    basal: Optional[list[tuple[int, int, Optional[float], bool]]] = None,
    scheduled_basal: Optional[list[tuple[int, int, float]]] = None,
    theme: Theme = DEFAULT_THEME,
) -> tuple[io.BytesIO, GlucoseStats]:
    """
    `boluses` are (epoch ms, units) pairs to mark on the trend line; any
    outside the plotted window are ignored.

    `basal` is the basal rate actually running, as (start ms, end ms, U/hr,
    from a temp basal?) spans (see `insulin.basal_timeline`), drawn hanging
    from the top of the chart; `scheduled_basal` is the profile's scheduled
    rate as (start ms, end ms, U/hr) spans, drawn as a dashed line over it.

    `entries` are raw Nightscout SGV entries (as returned by
    `nightscout_client.fetch_sgv_entries`), newest first.

    By default the y-axis scales dynamically to whatever the data spans
    (matplotlib's own autoscale, untouched). Passing `y_axis_max_mgdl` (e.g.
    `GRAPH_FIXED_Y_AXIS_MAX_MGDL`) instead pins the axis to a fixed
    `GRAPH_FIXED_Y_AXIS_MIN_MGDL`-`y_axis_max_mgdl` range regardless of what
    the data spans.

    `days_mode` (for multi-day windows requested in days) swaps the hourly
    x-axis for a tick every 6 hours with a label only every 24 hours,
    counted back from the latest reading ("now", "1d ago", ...).

    `title_name` (e.g. the requester's Discord name) replaces "Glucose" at
    the start of the chart title.

    Returns a PNG image buffer and the computed stats.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - fall back rather than hard error
        tz = ZoneInfo("UTC")

    times, values_mgdl = filter_entries_within_hours(entries, hours, tz)

    stats = compute_stats(
        times, values_mgdl, low_mgdl, high_mgdl,
        very_low_mgdl=very_low_mgdl, very_high_mgdl=very_high_mgdl,
    )

    # Nightscout's trend arrow for the latest reading ("?" -- not computable or
    # no direction sent -- is left off), and the change since the previous
    # reading, unless a sensor gap separates them.
    latest_entry = max(entries, key=lambda e: e["date"])
    direction = latest_entry.get("direction")
    trend_arrow = direction_to_arrow(direction)
    if trend_arrow == "?":
        trend_arrow = None
    delta_mgdl = None
    if len(times) >= 2 and (times[-1] - times[-2]).total_seconds() <= TREND_MAX_GAP_MINUTES * 60:
        delta_mgdl = values_mgdl[-1] - values_mgdl[-2]

    with plt.rc_context(GRAPH_RC):
        buf = _draw_glucose_graph(
            times, values_mgdl, stats, units=units, tz=tz, subtitle=format_time_span(hours),
            trend_arrow=trend_arrow, trend_direction=direction, delta_mgdl=delta_mgdl,
            low_mgdl=low_mgdl, high_mgdl=high_mgdl,
            very_low_mgdl=very_low_mgdl, very_high_mgdl=very_high_mgdl,
            y_axis_max_mgdl=y_axis_max_mgdl, days_mode=days_mode, title_name=title_name,
            colors=colors, theme=theme, boluses=boluses or [],
            basal=basal or [], scheduled_basal=scheduled_basal or [],
        )
    return buf, stats


def _draw_glucose_graph(
    times: list[datetime],
    values_mgdl: list[float],
    stats: GlucoseStats,
    *,
    units: str,
    tz: ZoneInfo,
    subtitle: str,
    trend_arrow: Optional[str],
    trend_direction: Optional[str],
    delta_mgdl: Optional[float],
    low_mgdl: float,
    high_mgdl: float,
    very_low_mgdl: float,
    very_high_mgdl: float,
    y_axis_max_mgdl: Optional[float],
    days_mode: bool,
    title_name: Optional[str],
    colors: RangeColors,
    theme: Theme,
    boluses: list[tuple[int, float]],
    basal: list[tuple[int, int, Optional[float], bool]],
    scheduled_basal: list[tuple[int, int, float]],
) -> io.BytesIO:
    """The drawing half of `render_glucose_graph` -- see there."""
    low_display = mgdl_to_units(low_mgdl, units)
    high_display = mgdl_to_units(high_mgdl, units)
    very_low_display = mgdl_to_units(very_low_mgdl, units)
    very_high_display = mgdl_to_units(very_high_mgdl, units)
    unit_label = "mg/dL" if units != "mmol/L" else "mmol/L"

    def zone_color(value_display: float) -> str:
        return _zone_colors(
            np.asarray([value_display]), low_display, high_display,
            very_low_display, very_high_display, colors=colors,
        )[0]

    fig = plt.figure(figsize=(10, 5), facecolor=theme.bg, dpi=150)
    ax = fig.add_axes([0.065, 0.11, 0.615, 0.70])
    card_ax = fig.add_axes([0.765, 0.05, 0.22, 0.90])

    # --- main trend plot -------------------------------------------------
    ax.set_facecolor(theme.bg)
    ax.xaxis_date(tz=tz)
    _draw_target_band(ax, low_display, high_display, colors=colors)

    t_num = mdates.date2num(times)
    values_arr = np.asarray([mgdl_to_units(v, units) for v in values_mgdl])

    # Pin the x-axis to exactly the data's own time span -- matplotlib's
    # default 5% autoscale margin would otherwise leave a visible gap before
    # the first point and after the last.
    if t_num[-1] > t_num[0]:
        ax.set_xlim(t_num[0], t_num[-1])
    else:
        # A single reading: give it some room either side rather than a
        # zero-width axis.
        ax.set_xlim(t_num[0] - 15 / 1440, t_num[0] + 15 / 1440)

    # The line is colored by range, CGM-app style. Each reading-to-reading
    # segment is split into short pieces so the color changes close to where
    # the line actually crosses a threshold, and segments spanning a sensor
    # gap are dropped instead of drawn as a straight line across it.
    _draw_range_colored_line(ax, t_num, values_arr, low_display, high_display,
                             very_low_display, very_high_display,
                             max_gap=TREND_MAX_GAP_MINUTES / (24 * 60),
                             colors=colors)

    # latest reading: a halo plus a solid dot in its range color
    latest_color = zone_color(values_arr[-1])
    ax.scatter([t_num[-1]], [values_arr[-1]], s=170, color=latest_color, alpha=0.22,
               linewidth=0, zorder=4, clip_on=False)
    ax.scatter([t_num[-1]], [values_arr[-1]], s=42, color=latest_color, zorder=5,
               edgecolor=theme.bg, linewidth=1.8, clip_on=False)

    if y_axis_max_mgdl is not None:
        # Fixed mode: pin both ends of the axis (rather than only the top)
        # so the same reading always lands at the same visual height,
        # regardless of what this particular window's data happens to span.
        ax.set_ylim(
            bottom=mgdl_to_units(GRAPH_FIXED_Y_AXIS_MIN_MGDL, units),
            top=mgdl_to_units(y_axis_max_mgdl, units),
        )
    else:
        ax.autoscale_view(scalex=False)
        bottom, top = ax.get_ylim()
        # breathing room so the line and the latest-point halo never touch the edges
        pad = (top - bottom) * 0.06
        bottom, top = bottom - pad, top + pad
        if any(rate is not None for _, _, rate, _ in basal):
            # Raise the top so the basal icicles get their band (the top
            # BASAL_BAND_FRACTION of the chart) above the readings, not over them.
            top += (top - bottom) * BASAL_BAND_FRACTION / (1 - BASAL_BAND_FRACTION)
        ax.set_ylim(bottom, top)

    _draw_very_threshold_lines(ax, units, very_low_mgdl, very_high_mgdl, colors=colors)
    _draw_basal(ax, basal, scheduled_basal, colors.in_range, theme=theme)
    _draw_boluses(fig, ax, boluses, t_num, values_arr, low=low_display, high=high_display,
                  very_low=very_low_display, very_high=very_high_display, colors=colors,
                  theme=theme)

    span_hours = (times[-1] - times[0]).total_seconds() / 3600
    if days_mode:
        # Ticks anchored to the latest reading rather than to clock hours, so
        # the labelled ticks land on exact whole days back from "now".
        latest_num = t_num[-1]
        tick_count = int(span_hours // 6)
        ax.xaxis.set_major_locator(mticker.FixedLocator(
            [latest_num - k for k in range(int(span_hours // 24) + 1)]
        ))
        ax.xaxis.set_minor_locator(mticker.FixedLocator(
            [latest_num - k * 0.25 for k in range(tick_count + 1) if k % 4]
        ))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, pos=None: "now" if round(latest_num - x) == 0 else f"{round(latest_num - x)}d ago"
        ))
    else:
        if span_hours <= 6:
            tick_interval = 1
        elif span_hours <= 12:
            tick_interval = 2
        elif span_hours <= 24:
            tick_interval = 3
        else:
            tick_interval = 6
        # Same idea as days mode: ticks counted back from the latest reading,
        # so "now" sits at the end of the line rather than on a clock hour.
        latest_num = t_num[-1]
        step = tick_interval / 24
        ax.xaxis.set_major_locator(mticker.FixedLocator(
            [latest_num - k * step for k in range(int(span_hours // tick_interval) + 1)]
        ))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, pos=None: "now" if round((latest_num - x) * 24) == 0 else f"{round((latest_num - x) * 24)}h ago"
        ))

    _style_chart_axes(ax, units, colors=colors, theme=theme)

    # --- title ---------------------------------------------------------------
    # "$" is escaped so matplotlib doesn't treat a name like "$am" as math text.
    title_prefix = title_name.replace("$", r"\$") if title_name else "Glucose"
    _draw_chart_title(fig, ax, title_prefix, subtitle, theme=theme)

    # --- summary card ------------------------------------------------------
    _draw_summary_card(fig, card_ax, stats, units=units, unit_label=unit_label, tz=tz,
                       latest_color=latest_color, colors=colors, theme=theme,
                       trend_arrow=trend_arrow, trend_direction=trend_direction, delta_mgdl=delta_mgdl)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf


# Readings further apart than this are treated as a sensor gap: the trend
# line breaks there rather than bridging it with a straight segment.
TREND_MAX_GAP_MINUTES = 15
# How many pieces each reading-to-reading segment is split into for coloring.
TREND_SEGMENT_SUBDIVISIONS = 6


def _range_line_segments(x: np.ndarray, values: np.ndarray, *, max_gap: float):
    """
    Splits a line into short (2-point) segments for a range-colored
    `LineCollection`. Points further apart than `max_gap` (in x units), or
    with a NaN on either end, aren't joined. Returns the segments, each
    segment's midpoint value (to color it by), and a mask of points with no
    neighbour on either side (which no segment covers, so they're drawn as
    dots instead).
    """
    finite = np.isfinite(values)
    keep = (np.diff(x) <= max_gap) & finite[:-1] & finite[1:]

    frac = np.linspace(0, 1, TREND_SEGMENT_SUBDIVISIONS + 1)
    x0, x1 = x[:-1][keep], x[1:][keep]
    v0, v1 = values[:-1][keep], values[1:][keep]
    xs = x0[:, None] + (x1 - x0)[:, None] * frac
    vs = v0[:, None] + (v1 - v0)[:, None] * frac
    points = np.stack([xs, vs], axis=-1)
    segments = np.stack([points[:, :-1], points[:, 1:]], axis=2).reshape(-1, 2, 2)
    segment_values = ((vs[:, :-1] + vs[:, 1:]) / 2).reshape(-1)

    connected = np.zeros(len(values), dtype=bool)
    connected[:-1] |= keep
    connected[1:] |= keep
    return segments, segment_values, finite & ~connected


def _draw_range_colored_line(
    ax, x: np.ndarray, values: np.ndarray,
    low: float, high: float, very_low: float, very_high: float,
    *, max_gap: float = np.inf, linewidth: float = 2.4, colors: RangeColors = DEFAULT_COLORS,
) -> None:
    """
    A line colored by range, CGM-app style. Each point-to-point segment is
    split into short pieces so the color changes close to where the line
    actually crosses a threshold, and gaps (see `_range_line_segments`) are
    left open instead of bridged with a straight line.
    """
    segments, segment_values, isolated = _range_line_segments(x, values, max_gap=max_gap)
    if len(segments):
        segment_colors = _zone_colors(segment_values, low, high, very_low, very_high,
                                      colors=colors)
        ax.add_collection(LineCollection(segments, colors=segment_colors, linewidths=linewidth,
                                         capstyle="round", joinstyle="round", zorder=3))
    if isolated.any():
        ax.scatter(x[isolated], values[isolated], s=10, zorder=3, linewidth=0,
                   color=_zone_colors(values[isolated], low, high, very_low, very_high,
                                      colors=colors))


# Basal "icicles", Nightscout-style: the rate hangs down from the top of the
# chart, deeper for a higher rate, in the user's in-range color, behind the
# glucose line.
# The highest rate in view reaches this far down the chart.
BASAL_BAND_FRACTION = 0.18
# Uploaders that send a temp basal every few minutes (e.g. tconnectsync)
# leave seconds-long gaps between one ending and the next starting; drawn
# as-is, each would flash down to the scheduled rate. A scheduled stretch
# shorter than this, right after a temp basal, is drawn as that temp basal
# continuing.
BASAL_GAP_BRIDGE_MS = 3 * 60 * 1000
# The same at the right edge, where the next temp basal usually just hasn't
# been uploaded yet: a scheduled stretch up to this long at the very end is
# drawn (and labelled) as the last temp basal still running.
BASAL_TRAILING_BRIDGE_MS = 20 * 60 * 1000


def _ms_to_num(ms: int) -> float:
    return mdates.date2num(datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("UTC")))


def _step_points(spans) -> tuple[list[float], list[float]]:
    """(x, rate) points tracing a step line through (start ms, end ms, rate)
    spans, with a NaN break wherever the rate is unknown or there's a gap."""
    xs: list[float] = []
    ys: list[float] = []
    last_end = None
    for lo, hi, rate in spans:
        if rate is None or (last_end is not None and lo != last_end):
            xs.append(np.nan)
            ys.append(np.nan)
        if rate is not None:
            xs += [_ms_to_num(lo), _ms_to_num(hi)]
            ys += [rate, rate]
        last_end = hi
    return xs, ys


def format_rate(rate: float) -> str:
    """e.g. "0.85 U/h", "1 U/h"."""
    return f"{rate:.2f}".rstrip("0").rstrip(".") + " U/h"


def _draw_basal(ax, basal, scheduled, color: str, *, theme: Theme = DEFAULT_THEME) -> None:
    """
    The basal rate as icicles hanging from the top of the chart, with the
    scheduled rate as a dashed line, so temp basals and suspends show as
    departures from it. Call it once the axis limits are final.
    """
    rates = [rate for _, _, rate, _ in basal if rate is not None]
    if not rates:
        return
    bottom, top = ax.get_ylim()
    peak = max(rates + [rate for _, _, rate in scheduled] + [0.1])
    depth = (top - bottom) * BASAL_BAND_FRACTION

    def to_y(values):
        return [top - depth * v / peak if v == v else np.nan for v in values]  # v != v for NaN

    spans = []
    for i, (lo, hi, rate, is_temp) in enumerate(basal):
        limit = BASAL_TRAILING_BRIDGE_MS if i == len(basal) - 1 else BASAL_GAP_BRIDGE_MS
        if spans and not is_temp and hi - lo < limit and spans[-1][3] and spans[-1][2] is not None:
            rate, is_temp = spans[-1][2], True
        spans.append((lo, hi, rate, is_temp))
    xs, ys = _step_points([(lo, hi, rate) for lo, hi, rate, _ in spans])
    ys = to_y(ys)
    ax.fill_between(xs, top, ys, color=color, alpha=0.13, linewidth=0, zorder=1.5)
    ax.plot(xs, ys, color=color, alpha=0.65, linewidth=0.9, zorder=1.6)
    if scheduled:
        sx, sy = _step_points(scheduled)
        ax.plot(sx, to_y(sy), color=color, alpha=0.8, linewidth=0.9,
                linestyle=(0, (3, 2)), zorder=1.7)

    current = spans[-1][2]
    label = "Basal" if current is None else f"Basal  {format_rate(current)}"
    ax.text(0.008, 0.985, label, transform=ax.transAxes, color=theme.muted, fontsize=7,
            ha="left", va="top", zorder=7,
            bbox=dict(boxstyle="round,pad=0.25", facecolor=theme.bg, edgecolor="none", alpha=0.75))


# The /ns graph trend chip's color, by Nightscout trend direction: which of
# the user's range colors it takes. Anything else (no direction, "NOT
# COMPUTABLE", "RATE OUT OF RANGE") uses the latest reading's own color.
TREND_RANGES = {
    "DoubleUp": "very_high",
    "SingleUp": "high",
    "FortyFiveUp": "in_range",
    "Flat": "in_range",
    "FortyFiveDown": "in_range",
    "SingleDown": "low",
    "DoubleDown": "very_low",
}


# Labels closer together than this (in pixels) are skipped so they don't
# overlap; the marker itself is always drawn.
BOLUS_LABEL_MIN_GAP_PX = 34


def format_units(units: float) -> str:
    """e.g. "2U", "2.5U", "4.35U" -- pumps deliver in steps as small as
    0.05 U, so up to two decimals, trailing zeros dropped."""
    return f"{units:.2f}".rstrip("0").rstrip(".") + "U"


def _draw_boluses(
    fig, ax, boluses: list[tuple[int, float]], t_num: np.ndarray, values: np.ndarray,
    *, low: float, high: float, very_low: float, very_high: float, colors: RangeColors,
    theme: Theme = DEFAULT_THEME,
) -> None:
    """
    A dot on the trend line where each bolus happened, filled with the range
    color of the reading there and outlined in the theme's secondary ink,
    with its size just above it (or below, near the top of the chart). Call
    it once the axis limits are final. Boluses outside the plotted window
    are skipped, and so is a label that would crowd the previous one.
    """
    if not boluses or len(t_num) == 0:
        return
    x0, x1 = ax.get_xlim()
    bottom, top = ax.get_ylim()
    points = []
    for ms, units in sorted(boluses):
        x = mdates.date2num(datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("UTC")))
        if x0 <= x <= x1:
            points.append((x, float(np.interp(x, t_num, values)), units))
    if not points:
        return

    xs, ys = [p[0] for p in points], [p[1] for p in points]
    fills = _zone_colors(np.asarray(ys), low, high, very_low, very_high, colors=colors)
    ax.scatter(xs, ys, marker="o", s=34, c=list(fills), edgecolors=theme.muted,
               linewidths=1.0, zorder=6, clip_on=False)

    to_px = ax.transData.transform
    last_label_px = None
    for x, y, units in points:
        px = to_px((x, y))[0]
        if last_label_px is not None and px - last_label_px < BOLUS_LABEL_MIN_GAP_PX:
            continue
        last_label_px = px
        near_top = (y - bottom) / (top - bottom) > 0.88
        ax.annotate(format_units(units), (x, y), xytext=(0, -9 if near_top else 7),
                    textcoords="offset points", ha="center", va="top" if near_top else "bottom",
                    color=theme.muted, fontsize=7, fontweight="bold", zorder=7,
                    annotation_clip=False,
                    bbox=dict(boxstyle="round,pad=0.15", facecolor=theme.bg, edgecolor="none", alpha=0.8))


def _draw_target_band(
    ax, low_display: float, high_display: float, *, colors: RangeColors = DEFAULT_COLORS
) -> None:
    """The shaded target range with thin low / high lines in their range colors."""
    ax.axhspan(low_display, high_display, color=colors.in_range, alpha=0.07, linewidth=0, zorder=0)
    ax.axhline(low_display, color=colors.low, linewidth=0.9, alpha=0.45, zorder=1)
    ax.axhline(high_display, color=colors.high, linewidth=0.9, alpha=0.45, zorder=1)


def _style_chart_axes(
    ax, units: str, *, colors: RangeColors = DEFAULT_COLORS, theme: Theme = DEFAULT_THEME,
) -> None:
    """No borders, tick marks on the x-axis only (the in-between ones in the
    in-range color), faint horizontal gridlines, and the other unit mirrored
    on the right."""
    unit_label = "mg/dL" if units != "mmol/L" else "mmol/L"
    ax.set_facecolor(theme.bg)
    # Tick marks on the x-axis only (labelled ones longer); the y-axis uses
    # its gridlines instead.
    ax.tick_params(axis="both", which="major", length=0, colors=theme.dim, labelsize=8.5, pad=7)
    ax.tick_params(axis="x", which="major", length=6, width=1, color=theme.dim, pad=5)
    ax.tick_params(axis="x", which="minor", length=3, width=1, color=colors.in_range)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(True, axis="y", color=theme.grid, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_ylabel(unit_label, color=theme.dim, fontsize=8.5, labelpad=8)

    secondary_ax = _add_secondary_units_axis(ax, units, theme=theme)
    secondary_ax.spines["right"].set_visible(False)
    secondary_ax.tick_params(length=0, labelsize=8, pad=7)


def _draw_chart_title(
    fig, ax, title: str, subtitle: Optional[str] = None, *, theme: Theme = DEFAULT_THEME,
) -> None:
    """A bold title, with an optional dim subtitle under it, left-aligned with the chart."""
    x = ax.get_position().x0
    fig.text(x, 0.935, title, color=theme.text, fontsize=16, fontweight="bold", va="baseline")
    if subtitle:
        fig.text(x, 0.875, subtitle, color=theme.dim, fontsize=9, va="baseline")


def _zone_colors(
    values: np.ndarray, low: float, high: float, very_low: float, very_high: float,
    *, colors: RangeColors = DEFAULT_COLORS,
) -> np.ndarray:
    """The 5-tier time-in-range color for each value (same palette as `/ns tir`)."""
    result = np.full(values.shape, colors.in_range, dtype=object)
    result[values > high] = colors.high
    result[values > very_high] = colors.very_high
    result[values < low] = colors.low
    result[values < very_low] = colors.very_low
    return result


def _format_age(then: datetime, tz: ZoneInfo) -> str:
    minutes = max(0, round((datetime.now(tz) - then).total_seconds() / 60))
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60}h {minutes % 60}m ago"


def _center_group(fig, ax, artists, center_x: float) -> None:
    """Shift text artists together (keeping their spacing) so the group is
    centered on `center_x` (axes fraction)."""
    renderer = fig.canvas.get_renderer()
    extents = [a.get_window_extent(renderer=renderer) for a in artists]
    group_center_px = (min(e.x0 for e in extents) + max(e.x1 for e in extents)) / 2
    shift = center_x - ax.transAxes.inverted().transform((group_center_px, 0))[0]
    for a in artists:
        x, y = a.get_position()
        a.set_position((x + shift, y))


def _ink_extents(s: str, fontsize: float, *, bold: bool = False) -> tuple[float, float, float, float]:
    """
    Where the glyphs of `s` actually draw, in points relative to the text's
    origin (left edge, baseline): (left, bottom, right, top). Unlike a text
    artist's extent, this excludes the font's side bearings and the empty
    space above / below the glyphs, so it's what lines things up visually.
    Uses the current font settings (call inside the graph's rc_context).
    """
    path = TextPath((0, 0), s, size=fontsize, prop=FontProperties(weight="bold" if bold else "normal"))
    ext = path.get_extents()
    return ext.x0, ext.y0, ext.x1, ext.y1


def _text_after(fig, ax, anchor, s: str, *, gap_px: float = 4.0, **kwargs):
    """Draws `s` on the same baseline as text artist `anchor`, just to its right."""
    renderer = fig.canvas.get_renderer()
    extent = anchor.get_window_extent(renderer=renderer)
    x_frac = ax.transAxes.inverted().transform((extent.x1 + gap_px, 0))[0]
    return ax.text(x_frac, anchor.get_position()[1], s, transform=ax.transAxes,
                   va="baseline", **kwargs)


def _horizontal_pill_bar(
    ax, *, x_frac: float, y_frac: float, width_frac: float, height_px: float,
    segments: list[tuple[float, str]], gap_px: float = 3.0, min_segment_px: float = 5.0,
) -> None:
    """
    A horizontal stacked bar of (percent, color) segments, left to right,
    clipped to a pill outline. `y_frac` is the bar's top edge, in axes
    fraction. Any non-zero segment gets at least `min_segment_px` so a <1%
    sliver stays visible.
    """
    shown = [(pct, color) for pct, color in segments if pct > 0]
    if not shown:
        return
    bbox = ax.get_window_extent()
    x0 = bbox.x0 + x_frac * bbox.width
    width_px = width_frac * bbox.width
    top = bbox.y0 + y_frac * bbox.height
    to_display = IdentityTransform()

    usable = width_px - gap_px * (len(shown) - 1)
    widths = [usable * pct / 100 for pct, _ in shown]
    small = [w < min_segment_px for w in widths]
    deficit = sum(min_segment_px - w for w, is_small in zip(widths, small) if is_small)
    large_total = sum(w for w, is_small in zip(widths, small) if not is_small)
    widths = [
        min_segment_px if is_small else w - deficit * w / large_total
        for w, is_small in zip(widths, small)
    ]

    clip = FancyBboxPatch(
        (x0, top - height_px), width_px, height_px,
        boxstyle=f"round,pad=0,rounding_size={height_px / 2}",
        transform=to_display, facecolor="none", edgecolor="none",
    )
    ax.add_patch(clip)
    x = x0
    for (_, color), w in zip(shown, widths):
        rect = Rectangle((x, top - height_px), w, height_px, transform=to_display,
                         facecolor=color, edgecolor="none", zorder=1)
        ax.add_patch(rect)
        rect.set_clip_path(clip)
        x += w + gap_px


def _draw_summary_card(
    fig, ax, stats: GlucoseStats, *, units: str, unit_label: str, tz: ZoneInfo, latest_color: str,
    colors: RangeColors = DEFAULT_COLORS, theme: Theme = DEFAULT_THEME,
    trend_arrow: Optional[str] = None, trend_direction: Optional[str] = None,
    delta_mgdl: Optional[float] = None,
) -> None:
    """The right-hand card: latest reading (with its trend and change), average /
    std dev, and time in range."""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    _rounded_card(ax, facecolor=theme.panel, pad=0.0, rounding=0.05)

    left, right = 0.1, 0.9

    def caption(x: float, y: float, s: str, **kwargs):
        return ax.text(x, y, s, transform=ax.transAxes, color=theme.dim, fontsize=8.5,
                       va="baseline", **kwargs)

    # latest reading: the number, with the trend chip (arrow + change since
    # the previous reading, e.g. "↗ +4") beside the top of it and the unit
    # under the chip. The number is sized so its digits run exactly from the
    # top of the chip to the unit's baseline; the whole block is centered
    # across the card, and vertically between the "Latest" line and the
    # "Average" label. Measured from the glyph outlines (see _ink_extents),
    # not estimated font metrics. Every row of the card is centered.
    renderer = fig.canvas.get_renderer()
    to_axes = ax.transAxes.inverted()
    to_px = ax.transAxes.transform
    px_per_pt = fig.dpi / 72
    caption_fontsize = 8.5
    middle = (left + right) / 2
    latest_caption = caption(left, 0.895, f"Latest  ·  {_format_age(stats.latest_time, tz)}")
    _center_group(fig, ax, [latest_caption], middle)
    # The "Latest" line visually ends at its baseline (a "g" hanging below
    # doesn't read as its edge); "Average" visually starts at its glyph top.
    above_px = to_px((0, 0.895))[1]
    below_px = to_px((0, 0.655))[1] + _ink_extents("Average", caption_fontsize)[3] * px_per_pt

    # The side stack: the chip box, a gap, then the unit (glyph top down to its
    # baseline -- a "g" hangs below it, as it does in running text).
    unit_fontsize = 10
    unit_ink_top = _ink_extents(unit_label, unit_fontsize)[3]
    stack_px = unit_ink_top * px_per_pt

    delta = format_delta(delta_mgdl, units) if delta_mgdl is not None else None
    chip_text = " ".join(b for b in (trend_arrow, delta) if b)
    chip = None
    chip_fontsize, chip_pad = 10, 0.35
    chip_pad_px = chip_pad * chip_fontsize * px_per_pt
    chip_gap_px = 6
    if chip_text:
        trend_range = TREND_RANGES.get(trend_direction or "")
        chip_color = getattr(colors, trend_range) if trend_range else latest_color
        chip = ax.text(left, 0.5, chip_text, transform=ax.transAxes, color=chip_color,
                       fontsize=chip_fontsize, fontweight="bold", va="bottom", ha="left",
                       bbox=dict(boxstyle=f"round,pad={chip_pad},rounding_size=0.8", edgecolor="none",
                                 facecolor=matplotlib.colors.to_rgba(chip_color, 0.16)))
        # (the box isn't laid out until the figure draws, so measure the text
        # and add the padding the box adds above and below it)
        chip_ext = chip.get_window_extent(renderer=renderer)
        chip_box_px = chip_ext.height + 2 * chip_pad_px
        chip_box_w_px = chip_ext.width + 2 * chip_pad_px
        stack_px += chip_gap_px + chip_box_px

    # Size the number so its digits are as tall as the stack (no chip: 30pt).
    value_text = format_value(stats.latest_mgdl, units)
    digits_top_per_pt = _ink_extents(value_text, 100, bold=True)[3] / 100
    value_fontsize = max(30.0, stack_px / (digits_top_per_pt * px_per_pt)) if chip else 30.0
    value_left_pt, _, value_right_pt, value_top_pt = _ink_extents(value_text, value_fontsize, bold=True)
    block_px = value_top_pt * px_per_pt

    # Center the block (digits' top to baseline) between the two lines of text.
    baseline_px = (above_px + below_px) / 2 - block_px / 2
    unit_ink = _ink_extents(unit_label, unit_fontsize)
    stack_w_px = max((unit_ink[2] - unit_ink[0]) * px_per_pt, chip_box_w_px if chip is not None else 0)
    block_w_px = (value_right_pt - value_left_pt) * px_per_pt + 20 + stack_w_px
    left_px = to_px((middle, 0))[0] - block_w_px / 2 - value_left_pt * px_per_pt
    ax.text(*to_axes.transform((left_px, baseline_px)), value_text, transform=ax.transAxes,
            color=latest_color, fontsize=value_fontsize, fontweight="bold", va="baseline")
    digits_top_px = baseline_px + block_px
    side_x_px = left_px + value_right_pt * px_per_pt + 20

    # The stack beside it: the chip's box top level with the digits' top (the
    # box reaches chip_pad_px past its text, hence the inset), the unit on the
    # number's baseline. With no chip, the unit sits level with the top instead.
    unit_x_px = side_x_px - _ink_extents(unit_label, unit_fontsize)[0] * px_per_pt
    if chip is not None:
        chip.set_va("top")
        chip.set_position(to_axes.transform((side_x_px + chip_pad_px, digits_top_px - chip_pad_px)))
        unit_baseline_px = baseline_px
    else:
        unit_baseline_px = digits_top_px - unit_ink_top * px_per_pt
    ax.text(*to_axes.transform((unit_x_px, unit_baseline_px)), unit_label,
            transform=ax.transAxes, color=theme.dim, fontsize=unit_fontsize, va="baseline")

    # average / std dev, side by side
    for x, column_center, label, value_mgdl in (
        (left, left + (right - left) / 4, "Average", stats.average_mgdl),
        (0.55, right - (right - left) / 4, "Std Dev", stats.std_mgdl),
    ):
        label_text = caption(x, 0.655, label)
        number = ax.text(x, 0.595, format_value(value_mgdl, units), transform=ax.transAxes,
                         color=theme.text, fontsize=15, fontweight="bold", va="baseline")
        unit_text = _text_after(fig, ax, number, unit_label, color=theme.dim, fontsize=7.5, gap_px=3)
        _center_group(fig, ax, [label_text], column_center)
        _center_group(fig, ax, [number, unit_text], column_center)

    ax.plot([left, right], [0.535, 0.535], transform=ax.transAxes, color=theme.baseline,
            linewidth=0.8)

    # time in range: headline %, then a pill bar (low -> high, left to right)
    # and the five ranges listed high -> low, top to bottom, like the y-axis
    tir_caption = caption(left, 0.465, "Time in Range")
    headline = ax.text(left, 0.375, _format_tir_pct(stats.time_in_range_pct), transform=ax.transAxes,
                       color=colors.in_range, fontsize=22, fontweight="bold", va="baseline")
    headline_label = _text_after(fig, ax, headline, "in range", color=theme.muted, fontsize=9, gap_px=6)
    _center_group(fig, ax, [tir_caption], middle)
    _center_group(fig, ax, [headline, headline_label], middle)

    tiers = [
        (stats.time_very_low_pct, "Very low", colors.very_low),
        (stats.time_below_pct, "Low", colors.low),
        (stats.time_in_range_pct, "In range", colors.in_range),
        (stats.time_above_pct, "High", colors.high),
        (stats.time_very_high_pct, "Very high", colors.very_high),
    ]
    _horizontal_pill_bar(ax, x_frac=left, y_frac=0.335, width_frac=right - left, height_px=10,
                         segments=[(pct, color) for pct, _, color in tiers])

    y = 0.245
    for pct, label, color in reversed(tiers):
        ax.scatter([left + 0.025], [y + 0.009], s=22, color=color, transform=ax.transAxes,
                   linewidth=0, clip_on=False, zorder=2)
        ax.text(left + 0.075, y, label, transform=ax.transAxes, color=theme.muted,
                fontsize=8.5, va="baseline")
        ax.text(right, y, _format_tir_pct(pct), transform=ax.transAxes, color=theme.text,
                fontsize=8.5, va="baseline", ha="right", fontweight="bold")
        y -= 0.048


def _add_secondary_units_axis(ax, units: str, *, theme: Theme = DEFAULT_THEME):
    """Adds a right-hand y-axis mirroring `ax`'s primary axis in the other glucose unit."""
    secondary_unit_label = "mmol/L" if units != "mmol/L" else "mg/dL"
    if units == "mmol/L":
        secondary_forward = lambda v: v * MGDL_PER_MMOL
        secondary_inverse = lambda v: v / MGDL_PER_MMOL
    else:
        secondary_forward = lambda v: v / MGDL_PER_MMOL
        secondary_inverse = lambda v: v * MGDL_PER_MMOL
    ax2 = ax.secondary_yaxis("right", functions=(secondary_forward, secondary_inverse))
    ax2.set_ylabel(secondary_unit_label, color=theme.dim, fontsize=9, labelpad=6)
    ax2.tick_params(colors=theme.dim, labelsize=8, pad=3)
    ax2.spines["right"].set_color(theme.baseline)
    ax2.yaxis.set_major_locator(mticker.MaxNLocator(nbins=5))
    if secondary_unit_label == "mmol/L":
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, pos=None: f"{v:.1f}"))
    else:
        ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, pos=None: f"{v:.0f}"))
    return ax2


# AGP time-of-day binning: 15-minute bins across the day, then a circular
# moving average (wrapping midnight) over neighbouring bins to smooth the
# percentile curves the way clinical AGP reports do. Bins with fewer than
# `AGP_MIN_READINGS_PER_BIN` readings are left as gaps rather than drawn from
# too little data.
AGP_BIN_MINUTES = 15
AGP_SMOOTHING_BINS = 5
AGP_MIN_READINGS_PER_BIN = 3


def render_agp_graph(
    entries: list[dict],
    *,
    units: str,
    low_mgdl: float,
    high_mgdl: float,
    days: int,
    tz_name: str = "UTC",
    very_low_mgdl: float = DEFAULT_VERY_LOW_MGDL,
    very_high_mgdl: float = DEFAULT_VERY_HIGH_MGDL,
    colors: RangeColors = DEFAULT_COLORS,
    theme: Theme = DEFAULT_THEME,
) -> io.BytesIO:
    """
    Renders an Ambulatory Glucose Profile: every reading in the last `days`
    days folded onto a single 24-hour day (in the user's timezone), drawn as
    the median plus 25-75th and 5-95th percentile bands per time of day.

    `entries` are raw Nightscout SGV entries, in any order.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - fall back rather than hard error
        tz = ZoneInfo("UTC")

    ordered = sorted(entries, key=lambda e: e["date"])
    if ordered:
        cutoff_ms = ordered[-1]["date"] - days * 24 * 3600 * 1000
        ordered = [e for e in ordered if e["date"] >= cutoff_ms]
    if not ordered:
        raise NoDataError("No glucose readings in the requested window.")

    bins_per_day = 24 * 60 // AGP_BIN_MINUTES
    bin_values: list[list[float]] = [[] for _ in range(bins_per_day)]
    for e in ordered:
        local = datetime.fromtimestamp(e["date"] / 1000, tz=tz)
        minute_of_day = local.hour * 60 + local.minute
        bin_values[minute_of_day // AGP_BIN_MINUTES].append(float(e["sgv"]))

    percentiles = (5, 25, 50, 75, 95)
    raw = np.full((len(percentiles), bins_per_day), np.nan)
    for i, values in enumerate(bin_values):
        if len(values) >= AGP_MIN_READINGS_PER_BIN:
            raw[:, i] = np.percentile(values, percentiles)

    # Circular moving average that ignores empty bins; a bin with no data of
    # its own stays empty so real gaps aren't painted over.
    half = AGP_SMOOTHING_BINS // 2
    smoothed = np.full_like(raw, np.nan)
    for i in range(bins_per_day):
        if np.isnan(raw[0, i]):
            continue
        window = [(i + k) % bins_per_day for k in range(-half, half + 1)]
        smoothed[:, i] = np.nanmean(raw[:, window], axis=1)

    if np.isnan(smoothed[0]).all():
        raise NoDataError("Not enough readings to build a profile.")

    p5, p25, p50, p75, p95 = (mgdl_to_units(1.0, units) * row for row in smoothed)
    low_display = mgdl_to_units(low_mgdl, units)
    high_display = mgdl_to_units(high_mgdl, units)

    # x positions in hours at each bin's centre, plus a wrapped copy of the
    # first bin at 24h so the curves run edge to edge.
    x = (np.arange(bins_per_day) + 0.5) * AGP_BIN_MINUTES / 60
    x = np.concatenate(([0.0], x, [24.0]))

    def wrap(row: np.ndarray) -> np.ndarray:
        edge = np.nanmean([row[0], row[-1]]) if not (np.isnan(row[0]) and np.isnan(row[-1])) else np.nan
        return np.concatenate(([edge], row, [edge]))

    p5, p25, p50, p75, p95 = (wrap(r) for r in (p5, p25, p50, p75, p95))

    very_low_display = mgdl_to_units(very_low_mgdl, units)
    very_high_display = mgdl_to_units(very_high_mgdl, units)

    with plt.rc_context(GRAPH_RC):
        fig = plt.figure(figsize=(10, 5), facecolor=theme.bg, dpi=150)
        ax = fig.add_axes([0.065, 0.11, 0.86, 0.70])
        _draw_target_band(ax, low_display, high_display, colors=colors)

        # Neutral percentile bands, so the range-colored median reads first.
        ax.fill_between(x, p5, p95, color=theme.muted, alpha=0.08, linewidth=0, zorder=2)
        ax.fill_between(x, p25, p75, color=theme.muted, alpha=0.16, linewidth=0, zorder=2)
        _draw_range_colored_line(ax, x, p50, low_display, high_display,
                                 very_low_display, very_high_display, linewidth=2.6,
                                 colors=colors)

        ax.set_xlim(0, 24)
        ax.set_xticks(range(0, 25, 3))
        ax.set_xticklabels(["12am", "3am", "6am", "9am", "12pm", "3pm", "6pm", "9pm", "12am"])
        ax.autoscale_view(scalex=False)

        _draw_very_threshold_lines(ax, units, very_low_mgdl, very_high_mgdl, colors=colors)

        # Direct band labels at the left edge instead of a legend.
        first = next(i for i in range(len(x)) if not np.isnan(p50[i]))
        for value, label in ((p95[first], "95%"), (p75[first], "75%"), (p50[first], "median"),
                             (p25[first], "25%"), (p5[first], "5%")):
            ax.annotate(label, (x[first], value), xytext=(4, 0), textcoords="offset points",
                        color=theme.muted, fontsize=7.5, va="center", ha="left", zorder=6)

        _style_chart_axes(ax, units, colors=colors, theme=theme)
        day_word = "day" if days == 1 else "days"
        _draw_chart_title(fig, ax, "Ambulatory Glucose Profile",
                          f"Last {days} {day_word}  ·  {len(ordered):,} readings  ·  times in {tz.key}",
                          theme=theme)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
    buf.seek(0)
    return buf


# `render_rolling_averages_graph`: trailing-window lengths (days) and one
# colour per line. The 30-day line needs 30 days of history *before* the
# first plotted day, so callers should fetch `days + max(windows)` days.
ROLLING_AVERAGE_WINDOWS_DAYS = (7, 14, 30)
# Kept clear of the range colors (red / yellow / orange / sky blue) so a line
# is never mistaken for a range; the 30-day line is the theme's neutral.
ROLLING_AVERAGE_COLORS = ("#a78bfa", "#f472b6")
ROLLING_AVERAGE_SAMPLE_HOURS = 6
ROLLING_AVERAGE_Y_AXIS_MAX_MGDL = 400.0


def render_rolling_averages_graph(
    entries: list[dict],
    *,
    units: str,
    low_mgdl: float,
    high_mgdl: float,
    days: int,
    tz_name: str = "UTC",
    title_name: Optional[str] = None,
    very_low_mgdl: float = DEFAULT_VERY_LOW_MGDL,
    very_high_mgdl: float = DEFAULT_VERY_HIGH_MGDL,
    colors: RangeColors = DEFAULT_COLORS,
    theme: Theme = DEFAULT_THEME,
) -> io.BytesIO:
    """
    Plots the 7/14/30-day trailing mean glucose over the last `days` days,
    sampled every `ROLLING_AVERAGE_SAMPLE_HOURS` hours back from the latest
    reading, with each line's current value in the legend.

    `entries` should reach back `days + max(ROLLING_AVERAGE_WINDOWS_DAYS)`
    days so the longest window is full at the left edge; with less history
    a window just averages whatever readings it has. A sample whose window
    has no readings at all is left as a gap.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 - fall back rather than hard error
        tz = ZoneInfo("UTC")

    ordered = sorted(entries, key=lambda e: e["date"])
    if not ordered:
        raise NoDataError("No glucose readings in the requested window.")

    dates_ms = np.asarray([e["date"] for e in ordered], dtype=np.int64)
    values = np.asarray([float(e["sgv"]) for e in ordered], dtype=float)
    # Prefix sums make each window's mean an O(log n) lookup.
    cumsum = np.concatenate(([0.0], np.cumsum(values)))

    latest_ms = int(dates_ms[-1])
    step_ms = ROLLING_AVERAGE_SAMPLE_HOURS * 3600 * 1000
    sample_count = days * 24 // ROLLING_AVERAGE_SAMPLE_HOURS
    sample_ms = latest_ms - step_ms * np.arange(sample_count, -1, -1, dtype=np.int64)
    if sample_ms[0] < dates_ms[0]:
        sample_ms = sample_ms[sample_ms >= dates_ms[0]]

    end_idx = np.searchsorted(dates_ms, sample_ms, side="right")
    sample_times = [datetime.fromtimestamp(ms / 1000, tz=tz) for ms in sample_ms]

    series = []
    for window_days in ROLLING_AVERAGE_WINDOWS_DAYS:
        start_idx = np.searchsorted(dates_ms, sample_ms - window_days * 24 * 3600 * 1000, side="right")
        counts = end_idx - start_idx
        with np.errstate(invalid="ignore", divide="ignore"):
            means = (cumsum[end_idx] - cumsum[start_idx]) / counts
        means[counts == 0] = np.nan
        series.append((window_days, means))

    low_display = mgdl_to_units(low_mgdl, units)
    high_display = mgdl_to_units(high_mgdl, units)
    unit_label = "mg/dL" if units != "mmol/L" else "mmol/L"
    line_colors = [*ROLLING_AVERAGE_COLORS, theme.neutral]

    with plt.rc_context(GRAPH_RC):
        fig = plt.figure(figsize=(10, 5), facecolor=theme.bg, dpi=150)
        ax = fig.add_axes([0.065, 0.11, 0.615, 0.70])
        card_ax = fig.add_axes([0.765, 0.05, 0.22, 0.90])
        _draw_target_band(ax, low_display, high_display, colors=colors)

        times_arr = np.asarray(sample_times)
        for (_, means_mgdl), color in zip(series, line_colors):
            means = mgdl_to_units(means_mgdl, units)
            ax.plot(times_arr, means, color=color, linewidth=2.4, solid_capstyle="round",
                    solid_joinstyle="round", zorder=3)
            if not np.isnan(means[-1]):
                ax.scatter([times_arr[-1]], [means[-1]], color=color, s=42, zorder=4,
                           edgecolor=theme.bg, linewidth=1.8, clip_on=False)

        ax.set_xlim(times_arr[0], times_arr[-1])
        # Fixed 0-400 mg/dL so lines sit at the same height from one chart to
        # the next; only grows if an average somehow exceeds the top.
        data_max_mgdl = max((np.nanmax(m) for _, m in series if not np.isnan(m).all()), default=0.0)
        ax.set_ylim(0, mgdl_to_units(max(ROLLING_AVERAGE_Y_AXIS_MAX_MGDL, data_max_mgdl * 1.05), units))

        _draw_very_threshold_lines(ax, units, very_low_mgdl, very_high_mgdl, colors=colors)

        def date_label(x: float, pos=None) -> str:
            dt = mdates.num2date(x, tz=tz)
            return f"{dt:%b} {dt.day}"

        # Tick density follows the span actually plotted, which can be shorter
        # than `days` for a site with less history than was asked for.
        span_days = (sample_times[-1] - sample_times[0]).total_seconds() / 86400
        if span_days >= 45:
            ax.xaxis.set_major_locator(mdates.MonthLocator(bymonthday=1, tz=tz))
            ax.xaxis.set_minor_locator(mdates.WeekdayLocator(byweekday=mdates.MO, tz=tz))
        elif span_days >= 14:
            ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=mdates.MO, tz=tz))
            ax.xaxis.set_minor_locator(mdates.DayLocator(tz=tz))
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator(tz=tz))
            ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[6, 12, 18], tz=tz))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(date_label))

        _style_chart_axes(ax, units, colors=colors, theme=theme)
        # "$" is escaped so matplotlib doesn't treat a name like "$am" as math text.
        title_prefix = title_name.replace("$", r"\$") if title_name else "Glucose"
        start, end = sample_times[0], sample_times[-1]
        _draw_chart_title(fig, ax, title_prefix,
                          f"Rolling averages  ·  last {days}d  ·  {start:%b} {start.day} – {end:%b} {end.day}",
                          theme=theme)

        _draw_averages_card(card_ax, series, units=units, unit_label=unit_label,
                            line_colors=line_colors, theme=theme)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
    buf.seek(0)
    return buf


def _draw_averages_card(
    ax, series: list[tuple[int, np.ndarray]], *, units: str, unit_label: str,
    line_colors: list[str], theme: Theme = DEFAULT_THEME,
) -> None:
    """The right-hand card for the rolling averages graph: each window's
    current average, in its line's color."""
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    _rounded_card(ax, facecolor=theme.panel, pad=0.0, rounding=0.05)

    left = 0.1
    ax.text(left, 0.895, "Current averages", transform=ax.transAxes, color=theme.dim,
            fontsize=8.5, va="baseline")
    y = 0.80
    for (window_days, means_mgdl), color in zip(series, line_colors):
        ax.scatter([left + 0.02], [y + 0.012], s=30, color=color, transform=ax.transAxes,
                   linewidth=0, clip_on=False)
        ax.text(left + 0.07, y, f"{window_days}-day", transform=ax.transAxes, color=theme.muted,
                fontsize=9.5, va="baseline")
        current = means_mgdl[-1]
        value = "–" if np.isnan(current) else format_value(current, units)
        number = ax.text(left, y - 0.105, value, transform=ax.transAxes, color=theme.text,
                         fontsize=24, fontweight="bold", va="baseline")
        if not np.isnan(current):
            _text_after(ax.figure, ax, number, unit_label, color=theme.dim, fontsize=9, gap_px=5)
        y -= 0.28


TDD_BASAL_COLOR = "#3b8beb"
TDD_BOLUS_COLOR = "#e5622b"


def render_tdd_image(
    daily: list[tuple[datetime, float, float]],
    *,
    basal_incomplete: bool = False,
    theme: Theme = DEFAULT_THEME,
) -> io.BytesIO:
    """
    Total daily dose graphic. `daily` is (window end, basal U, bolus U) per
    day, oldest first. Shows the (average) TDD as a headline number, a
    basal/bolus split bar with units and percentages, and -- for more than
    one day -- a stacked column per day with the average marked.
    """
    days = len(daily)
    basal = sum(b for _, b, _ in daily) / days
    bolus = sum(o for _, _, o in daily) / days
    total = basal + bolus

    dpi = 150
    width = 690
    left = 54
    chart_h = 200 if days > 1 else 0
    note_h = 60 if basal_incomplete else 0
    height = 316 + (chart_h + 64 if days > 1 else 0) + note_h

    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi, facecolor=theme.bg)
    canvas = fig.add_axes([0, 0, 1, 1])
    canvas.set_xlim(0, width)
    canvas.set_ylim(0, height)
    canvas.axis("off")

    def top(px: float) -> float:
        return height - px

    # --- header ----------------------------------------------------------
    subtitle = "Last 24 hours" if days == 1 else f"Daily average · last {days} days"
    canvas.text(left, top(28), "Total Daily Dose", fontsize=21, fontweight="bold",
                color=theme.text, va="top")
    canvas.text(left, top(74), subtitle, fontsize=12, color=theme.muted, va="top")
    headline = canvas.text(left, top(110), f"{total:.1f}", fontsize=40, fontweight="bold",
                           color=theme.text, va="top")
    fig.canvas.draw()
    headline_right = canvas.transData.inverted().transform(
        (headline.get_window_extent().x1, 0)
    )[0]
    canvas.text(headline_right + 8, top(136), "U / day", fontsize=14, color=theme.muted, va="top")

    # --- basal / bolus split bar -------------------------------------------
    bar_w, bar_h, gap = width - 2 * left, 26, 4
    bar_y = top(192) - bar_h
    clip = FancyBboxPatch((left, bar_y), bar_w, bar_h, boxstyle="round,pad=0,rounding_size=6",
                          facecolor="none", edgecolor="none")
    canvas.add_patch(clip)
    shown = [(units, color) for units, color in ((basal, TDD_BASAL_COLOR), (bolus, TDD_BOLUS_COLOR)) if units > 0]
    usable = bar_w - gap * (len(shown) - 1)
    x = left
    for units, color in shown:
        seg_w = usable * units / total
        rect = Rectangle((x, bar_y), seg_w, bar_h, facecolor=color, edgecolor="none")
        canvas.add_patch(rect)
        rect.set_clip_path(clip)
        x += seg_w + gap

    label_y = bar_y - 14
    canvas.text(left, label_y, f"{basal:.1f} U basal", color=theme.text, fontsize=14, va="top")
    canvas.text(left, label_y - 30, f"{100 * basal / total:.0f}%", color=TDD_BASAL_COLOR,
                fontsize=12, va="top", fontweight="bold")
    canvas.text(left + bar_w, label_y, f"{bolus:.1f} U bolus", color=theme.text, fontsize=14,
                va="top", ha="right")
    canvas.text(left + bar_w, label_y - 30, f"{100 * bolus / total:.0f}%", color=TDD_BOLUS_COLOR,
                fontsize=12, va="top", ha="right", fontweight="bold")

    # --- per-day stacked columns ---------------------------------------------
    if days > 1:
        ax = fig.add_axes([left / width, (34 + note_h) / height, bar_w / width, chart_h / height])
        ax.set_facecolor(theme.bg)
        xs = list(range(days))
        basals = [b for _, b, _ in daily]
        boluses = [o for _, _, o in daily]
        ax.bar(xs, basals, color=TDD_BASAL_COLOR, width=0.72, zorder=2)
        ax.bar(xs, boluses, bottom=basals, color=TDD_BOLUS_COLOR, width=0.72, zorder=2)
        ax.axhline(total, color=theme.text, linestyle="--", linewidth=1, alpha=0.6, zorder=3)
        # Average label just past the right end of the line, outside the
        # bars, so it never covers a column.
        ax.text(days - 0.4, total, f" avg\n {total:.1f}", color=theme.muted, fontsize=8,
                va="center", ha="left", clip_on=False, linespacing=1.1)

        # Label every day for a week, then thin out -- always keeping the
        # newest day -- so dates never collide.
        step = 1 if days <= 7 else (2 if days <= 14 else 5)
        ticks = [i for i in xs if (days - 1 - i) % step == 0]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{daily[i][0]:%b} {daily[i][0].day}" for i in ticks])
        ax.tick_params(colors=theme.dim, labelsize=8, length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(theme.baseline)
        ax.grid(True, axis="y", color=theme.grid, linewidth=0.8, zorder=0)
        ax.set_xlim(-0.6, days - 0.4)
        ax.set_ylim(0, max(b + o for _, b, o in daily) * 1.12 or 1)

    if basal_incomplete:
        canvas.text(left, 14, "Basal may be under-counted: no basal schedule in your\n"
                    "Nightscout profile to fill gaps between temp basals.",
                    color=theme.dim, fontsize=8, va="bottom", linespacing=1.3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf
