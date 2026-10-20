"""
Total daily dose (TDD) of insulin, split into basal and bolus, computed from
Nightscout treatments plus the scheduled basal rates in the user's profile.

Nightscout doesn't store a TDD itself, so it's rebuilt here:

  - Bolus: every treatment that records insulin (meal / correction boluses,
    SMBs, combo boluses -- uploaders differ on the eventType, so any
    `insulin` amount counts), except temp basals: some uploaders (e.g.
    tconnectsync) also put the insulin a temp basal delivered on it, and
    that's already counted as basal below. A Combo Bolus's
    `enteredinsulin`, when present, is its full amount including the
    extended part.
  - Basal: temp basals (rate x time actually delivered, each cut short by
    the next one or by a zero-duration "cancel"), with the scheduled basal
    from the profile filling every stretch no temp basal covers. Uploaders
    that send every basal change as a temp basal (e.g. tconnectsync for
    Tandem pumps) never need the schedule; Loop / AAPS rely on it.

Known simplifications: the latest profile's default basal schedule is used
for the whole window (profile switches and percentage overrides aren't
applied), and a pump suspend only counts if the uploader records it as a
zero-rate temp basal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from nightscout_client import treatment_time_ms

MS_PER_HOUR = 3600 * 1000


@dataclass
class TotalDailyDose:
    days: int
    total_units: float        # over the whole window
    basal_units: float
    bolus_units: float
    # True when some of the window had neither a temp basal nor a schedule
    # to fill it, so basal is a lower bound.
    basal_incomplete: bool

    @property
    def daily_total(self) -> float:
        return self.total_units / self.days

    @property
    def daily_basal(self) -> float:
        return self.basal_units / self.days

    @property
    def daily_bolus(self) -> float:
        return self.bolus_units / self.days

    @property
    def basal_pct(self) -> float:
        return 100 * self.basal_units / self.total_units if self.total_units else 0.0

    @property
    def bolus_pct(self) -> float:
        return 100 * self.bolus_units / self.total_units if self.total_units else 0.0


@dataclass
class BasalSchedule:
    """Scheduled basal rates: (seconds after local midnight, U/hr), sorted."""

    segments: list[tuple[int, float]]
    tz: ZoneInfo

    def units_between(self, start_ms: int, end_ms: int) -> float:
        return sum(rate * (hi - lo) / MS_PER_HOUR for lo, hi, rate in self.rate_segments(start_ms, end_ms))

    def rate_segments(self, start_ms: int, end_ms: int) -> list[tuple[int, int, float]]:
        """The scheduled rate over [start_ms, end_ms), split where it changes:
        (start ms, end ms, U/hr) spans."""
        segments: list[tuple[int, int, float]] = []
        cur = start_ms
        while cur < end_ms:
            local = datetime.fromtimestamp(cur / 1000, tz=self.tz)
            sec = local.hour * 3600 + local.minute * 60 + local.second + local.microsecond / 1e6
            rate = self.segments[0][1]
            next_boundary = 86400.0
            for seg_start, seg_rate in self.segments:
                if seg_start <= sec:
                    rate = seg_rate
                else:
                    next_boundary = seg_start
                    break
            step_ms = max(1, int((next_boundary - sec) * 1000))
            seg_end = min(end_ms, cur + step_ms)
            segments.append((cur, seg_end, rate))
            cur = seg_end
        return segments


def basal_schedule_from_profile(profile: Optional[dict[str, Any]], fallback_tz: ZoneInfo) -> Optional[BasalSchedule]:
    """The default profile's basal schedule from a `profile.json` document."""
    if not isinstance(profile, dict):
        return None
    store = profile.get("store")
    if not isinstance(store, dict) or not store:
        return None
    active = store.get(profile.get("defaultProfile")) or next(iter(store.values()))
    if not isinstance(active, dict) or not isinstance(active.get("basal"), list):
        return None

    segments = []
    for item in active["basal"]:
        if not isinstance(item, dict):
            continue
        try:
            rate = float(item["value"])
        except (KeyError, TypeError, ValueError):
            continue
        seconds = item.get("timeAsSeconds")
        if not isinstance(seconds, (int, float)):
            try:
                hh, mm = str(item.get("time", "00:00")).split(":")[:2]
                seconds = int(hh) * 3600 + int(mm) * 60
            except ValueError:
                continue
        segments.append((int(seconds), rate))
    if not segments:
        return None
    segments.sort()

    try:
        tz = ZoneInfo(active["timezone"]) if active.get("timezone") else fallback_tz
    except Exception:  # noqa: BLE001 - unknown tz name, use the user's
        tz = fallback_tz
    return BasalSchedule(segments=segments, tz=tz)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def bolus_units(treatment: dict[str, Any]) -> float:
    event_type = str(treatment.get("eventType", "")).lower()
    # Insulin recorded on a temp basal is basal delivery, counted from its
    # rate and duration -- never a bolus.
    if event_type == "temp basal":
        return 0.0
    if event_type == "combo bolus":
        entered = _number(treatment.get("enteredinsulin"))
        if entered is not None and entered > 0:
            return entered
    insulin = _number(treatment.get("insulin"))
    return insulin if insulin is not None and insulin > 0 else 0.0


def basal_timeline(
    treatments: list[dict[str, Any]],
    *,
    schedule: Optional[BasalSchedule],
    start_ms: int,
    end_ms: int,
) -> list[tuple[int, int, Optional[float], bool]]:
    """
    The basal rate actually running over [start_ms, end_ms), as
    (start ms, end ms, U/hr, from a temp basal?) spans in time order:
    temp basals (each running for its duration, or until the next temp
    basal or zero-duration cancel starts, whichever is sooner), with the
    scheduled basal filling every stretch no temp basal covers. A rate of
    None marks time with neither a temp basal nor a schedule to fill it.
    `treatments` may start before `start_ms` (a temp basal begun earlier
    still counts for the part inside the window).
    """
    timed = [(t, treatment_time_ms(t)) for t in treatments]
    timed = sorted(((t, ms) for t, ms in timed if ms is not None), key=lambda pair: pair[1])

    temps = [(t, ms) for t, ms in timed if str(t.get("eventType", "")).lower() == "temp basal"]
    intervals: list[tuple[int, int, dict[str, Any]]] = []
    for i, (t, ms) in enumerate(temps):
        duration_min = _number(t.get("duration")) or 0.0
        end = ms + int(duration_min * 60 * 1000)
        if i + 1 < len(temps):
            end = min(end, temps[i + 1][1])
        if end > ms:
            intervals.append((ms, end, t))

    timeline: list[tuple[int, int, Optional[float], bool]] = []
    cursor = start_ms

    def fill_gap(lo: int, hi: int) -> None:
        if hi <= lo:
            return
        if schedule is not None:
            timeline.extend((a, b, rate, False) for a, b, rate in schedule.rate_segments(lo, hi))
        else:
            timeline.append((lo, hi, None, False))

    for lo, hi, t in intervals:
        lo, hi = max(lo, start_ms), min(hi, end_ms)
        if hi <= lo or hi <= cursor:
            continue
        lo = max(lo, cursor)
        fill_gap(cursor, lo)

        absolute = _number(t.get("absolute"))
        if absolute is None:
            absolute = _number(t.get("rate"))
        if absolute is not None:
            timeline.append((lo, hi, max(absolute, 0.0), True))
        else:
            # Percent temp (AAPS): relative to the scheduled rate.
            percent = _number(t.get("percent"))
            if percent is not None and schedule is not None:
                factor = max(0.0, 1 + percent / 100)
                timeline.extend((a, b, rate * factor, True) for a, b, rate in schedule.rate_segments(lo, hi))
            else:
                fill_gap(lo, hi)
        cursor = hi
    fill_gap(cursor, end_ms)
    return timeline


def compute_total_daily_dose(
    treatments: list[dict[str, Any]],
    *,
    schedule: Optional[BasalSchedule],
    start_ms: int,
    end_ms: int,
    days: int,
) -> TotalDailyDose:
    """
    TDD over [start_ms, end_ms). `treatments` may start before `start_ms`
    (a temp basal begun earlier still counts for the part inside the window).
    """
    timed = [(t, treatment_time_ms(t)) for t in treatments]
    timed = sorted(((t, ms) for t, ms in timed if ms is not None), key=lambda pair: pair[1])

    bolus = sum(bolus_units(t) for t, ms in timed if start_ms <= ms < end_ms)

    basal = 0.0
    uncovered_ms = 0
    for lo, hi, rate, _ in basal_timeline(treatments, schedule=schedule, start_ms=start_ms, end_ms=end_ms):
        if rate is None:
            uncovered_ms += hi - lo
        else:
            basal += rate * (hi - lo) / MS_PER_HOUR

    return TotalDailyDose(
        days=days,
        total_units=basal + bolus,
        basal_units=basal,
        bolus_units=bolus,
        basal_incomplete=uncovered_ms > 0.05 * (end_ms - start_ms),
    )


def compute_daily_doses(
    treatments: list[dict[str, Any]],
    *,
    schedule: Optional[BasalSchedule],
    end_ms: int,
    days: int,
) -> tuple[TotalDailyDose, list[tuple[int, TotalDailyDose]]]:
    """
    TDD for each of the last `days` rolling 24-hour windows ending at
    `end_ms`, plus their combined total. Returns (overall, per_day), with
    per_day as (window end ms, dose) pairs, oldest first.
    """
    day_ms = 24 * MS_PER_HOUR
    per_day = []
    for i in range(days - 1, -1, -1):
        window_end = end_ms - i * day_ms
        dose = compute_total_daily_dose(
            treatments, schedule=schedule, start_ms=window_end - day_ms, end_ms=window_end, days=1
        )
        per_day.append((window_end, dose))

    basal = sum(d.basal_units for _, d in per_day)
    bolus = sum(d.bolus_units for _, d in per_day)
    overall = TotalDailyDose(
        days=days,
        total_units=basal + bolus,
        basal_units=basal,
        bolus_units=bolus,
        basal_incomplete=any(d.basal_incomplete for _, d in per_day),
    )
    return overall, per_day
