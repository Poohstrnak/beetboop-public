"""
Minimal async client for the Nightscout REST API.

Nightscout supports two auth schemes:
  - Legacy API_SECRET: the plaintext secret is SHA1-hashed client-side and
    sent as the `api-secret` header.
  - Role-based access tokens (newer Nightscout versions): an opaque
    `subject-hash` string sent as the `token` query parameter, no hashing
    needed.

Either, both, or neither may be configured for a given site depending on
how that Nightscout instance is set up, so both are supported here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10

# Long `fetch_sgv_entries` ranges are split into chunks of this many days and
# fetched a few at a time. One request for months of readings (tens of
# thousands of entries) is enough to make many hosted Nightscout sites time
# out or run out of memory and answer with a 502; a week at a time is small
# enough for even a free-tier host.
SGV_CHUNK_DAYS = 7
SGV_CHUNK_CONCURRENCY = 3

# Gateway errors are usually a momentarily overloaded (or just-waking) host,
# so they're retried with a short backoff before giving up.
RETRYABLE_STATUSES = {502, 503, 504}
RETRY_DELAYS_SECONDS = (1, 3)


class NightscoutError(Exception):
    """
    Raised for any failure talking to a Nightscout site.

    Its message is shown in Discord -- sometimes publicly, sometimes to a
    different user than the site's owner (`/ns check-on`) -- so it must never
    include the site's URL, hostname, or credentials. Put those details in
    the log instead.
    """


class NightscoutHTTPError(NightscoutError):
    """A non-200 response, with its status code kept for retry decisions."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


async def _get_json(
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    request_params: dict[str, Any] = dict(params or {})
    headers: dict[str, str] = {}

    if api_token:
        request_params["token"] = api_token
    if api_secret_hash:
        headers["api-secret"] = api_secret_hash

    try:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(url, params=request_params, headers=headers) as resp:
                if resp.status == 401:
                    raise NightscoutHTTPError(
                        "Authentication failed. Check the API secret / access "
                        "token with `/ns setup`.",
                        resp.status,
                    )
                if resp.status == 404:
                    raise NightscoutHTTPError(
                        "The Nightscout site returned a 404 (not found). Double-check "
                        "the URL with `/ns setup`.",
                        resp.status,
                    )
                if resp.status in RETRYABLE_STATUSES:
                    raise NightscoutHTTPError(
                        f"Nightscout's server returned HTTP {resp.status} - it may be "
                        "overloaded or starting up. Try again in a minute.",
                        resp.status,
                    )
                if resp.status != 200:
                    raise NightscoutHTTPError(
                        f"Nightscout returned an unexpected HTTP {resp.status}.",
                        resp.status,
                    )
                try:
                    return await resp.json(content_type=None)
                except Exception as exc:  # noqa: BLE001 - surfaced to the user
                    raise NightscoutError(
                        "Nightscout's response could not be parsed as JSON. Is "
                        "the URL correct?"
                    ) from exc
    # aiohttp's own exception text can include the full request URL (with the
    # access token in its query string), so it's logged for the bot host and
    # replaced with a fixed message for Discord.
    except aiohttp.ClientSSLError as exc:
        log.warning("Nightscout SSL error for %s: %r", url, exc)
        raise NightscoutError(
            "Couldn't make a secure connection to the Nightscout site (SSL/certificate error)."
        ) from exc
    except aiohttp.ClientConnectorError as exc:
        log.warning("Couldn't connect to %s: %r", url, exc)
        raise NightscoutError(
            "Couldn't connect to the Nightscout site. Check that it's online and "
            "that the URL set with `/ns setup` is right."
        ) from exc
    except aiohttp.InvalidURL as exc:
        log.warning("Invalid Nightscout URL %s: %r", url, exc)
        raise NightscoutError("The Nightscout URL isn't valid. Set it again with `/ns setup`.") from exc
    except aiohttp.ClientError as exc:
        log.warning("Nightscout request to %s failed: %r", url, exc)
        raise NightscoutError(
            f"The request to the Nightscout site failed ({type(exc).__name__})."
        ) from exc
    except ValueError as exc:
        # yarl rejects some malformed URLs with a plain ValueError before
        # aiohttp ever sends the request.
        log.warning("Malformed Nightscout URL %s: %r", url, exc)
        raise NightscoutError("The Nightscout URL isn't valid. Set it again with `/ns setup`.") from exc
    except TimeoutError as exc:
        # aiohttp's ClientTimeout raises (asyncio.)TimeoutError, which is
        # NOT an aiohttp.ClientError subclass, so it needs its own handler
        # or it slips past the two clauses above uncaught.
        raise NightscoutError(
            f"Nightscout didn't respond within {timeout}s. The site may be slow "
            "or overloaded -- try again, or ask for fewer days."
        ) from exc


async def fetch_sgv_entries(
    base_url: str,
    *,
    count: int = 1000,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """
    Fetch sensor glucose value (SGV) entries, newest first.

    By default this just returns up to `count` most recent entries -- fine
    for a small, fixed request like "the latest reading". But for anything
    that needs "all readings in the last N days", pass `start_ms` (and
    optionally `end_ms`): those get sent as Nightscout's `find[date]` query
    filter, so the *server* returns exactly what falls in that window,
    instead of us having to guess a `count` large enough to cover N days at
    an assumed reporting frequency. That guess is exactly the kind of thing
    that quietly truncates results -- a site reporting every minute instead
    of every 5 minutes needs 5x the count, and there's no way to tell from
    here how often a given site actually reports. When `start_ms`/`end_ms`
    are given, `count` still gets sent too, purely as a generous safety
    ceiling in case a very old Nightscout version doesn't understand `find`.

    Ranges longer than `SGV_CHUNK_DAYS` are fetched as several smaller
    requests and merged, and each request is retried on a 502/503/504, so a
    months-long window doesn't depend on one huge response succeeding.

    Returns raw Nightscout entry dicts, each with at least `sgv` (mg/dL) and
    `date` (epoch milliseconds) keys.
    """
    if not base_url:
        raise NightscoutError("No Nightscout URL is configured.")

    # The general entries endpoint filtered to readings, rather than the
    # /api/v1/entries/sgv.json shortcut: some Nocturne versions ignore `count`
    # and `find[...]` on the shortcut and always return just the latest 10
    # readings, while every Nightscout and Nocturne site tested honors them
    # here. On standard Nightscout the two return identical data.
    url = f"{base_url.rstrip('/')}/api/v1/entries.json"
    auth = dict(api_secret_hash=api_secret_hash, api_token=api_token, timeout=timeout)

    async def fetch_range(lo: Optional[int], hi: Optional[int], inclusive_end: bool) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"count": count, "find[type]": "sgv"}
        if lo is not None:
            params["find[date][$gte]"] = lo
        if hi is not None:
            params["find[date][$lte]" if inclusive_end else "find[date][$lt]"] = hi
        data = await _get_list_with_retry(url, params, **auth)
        return [e for e in data if isinstance(e, dict) and e.get("type", "sgv") == "sgv" and "sgv" in e]

    entries = await _fetch_chunked(
        fetch_range, start_ms, end_ms,
        dedupe_key=lambda e: e.get("_id") or (e.get("date"), e.get("sgv")),
    )
    entries.sort(key=lambda e: e.get("date", 0), reverse=True)
    return entries


async def fetch_treatments(
    base_url: str,
    *,
    start_ms: int,
    end_ms: Optional[int] = None,
    count: int = 100_000,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """
    Fetch all treatments (boluses, temp basals, carbs, ...) from `start_ms`
    onward, oldest first. Treatments are filtered by their `created_at` ISO
    timestamp, and long ranges are chunked and retried the same way as
    `fetch_sgv_entries`. `count` matters more here than for entries: without
    it Nightscout returns only the latest 100 treatments.
    """
    if not base_url:
        raise NightscoutError("No Nightscout URL is configured.")

    url = f"{base_url.rstrip('/')}/api/v1/treatments.json"
    auth = dict(api_secret_hash=api_secret_hash, api_token=api_token, timeout=timeout)

    async def fetch_range(lo: Optional[int], hi: Optional[int], inclusive_end: bool) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"count": count}
        if lo is not None:
            params["find[created_at][$gte]"] = _iso_utc(lo)
        if hi is not None:
            params["find[created_at][$lte]" if inclusive_end else "find[created_at][$lt]"] = _iso_utc(hi)
        data = await _get_list_with_retry(url, params, **auth)
        return [t for t in data if isinstance(t, dict)]

    treatments = await _fetch_chunked(
        fetch_range, start_ms, end_ms,
        dedupe_key=lambda t: t.get("_id") or (t.get("created_at"), t.get("eventType"), t.get("insulin")),
    )
    treatments.sort(key=lambda t: treatment_time_ms(t) or 0)
    return treatments


async def fetch_profile(
    base_url: str,
    *,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[dict[str, Any]]:
    """The most recent profile document (`/api/v1/profile.json`), or None."""
    if not base_url:
        raise NightscoutError("No Nightscout URL is configured.")

    url = f"{base_url.rstrip('/')}/api/v1/profile.json"
    data = await _get_list_with_retry(
        url, {"count": 1}, api_secret_hash=api_secret_hash, api_token=api_token, timeout=timeout,
    )
    return data[0] if data and isinstance(data[0], dict) else None


def treatment_time_ms(treatment: dict[str, Any]) -> Optional[int]:
    """A treatment's timestamp in epoch ms, from `date`/`mills` or `created_at`."""
    for key in ("date", "mills"):
        value = treatment.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    created = treatment.get("created_at")
    if isinstance(created, str):
        try:
            parsed = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _fetch_chunked(
    fetch_range: Callable[[Optional[int], Optional[int], bool], Awaitable[list[dict[str, Any]]]],
    start_ms: Optional[int],
    end_ms: Optional[int],
    *,
    dedupe_key: Callable[[dict[str, Any]], Any],
) -> list[dict[str, Any]]:
    """
    Calls `fetch_range(lo, hi, inclusive_end)` once for a short (or
    open-start) range, or once per `SGV_CHUNK_DAYS` chunk for a long one --
    [lo, hi) chunks, the last keeping the caller's inclusive end or staying
    open-ended -- a few at a time, and merges the results with duplicates
    (from overlapping chunk edges) dropped. Order is unspecified.
    """
    chunk_ms = SGV_CHUNK_DAYS * 24 * 3600 * 1000
    if start_ms is None or (end_ms if end_ms is not None else _now_ms()) - start_ms <= chunk_ms:
        return await fetch_range(start_ms, end_ms, True)

    range_end = end_ms if end_ms is not None else _now_ms()
    bounds = []
    lo = start_ms
    while lo < range_end:
        hi = min(lo + chunk_ms, range_end)
        is_last = hi >= range_end
        bounds.append((lo, end_ms if is_last else hi, is_last))
        lo = hi

    semaphore = asyncio.Semaphore(SGV_CHUNK_CONCURRENCY)

    async def fetch_chunk(lo: int, hi: Optional[int], is_last: bool) -> list[dict[str, Any]]:
        async with semaphore:
            return await fetch_range(lo, hi, is_last)

    chunks = await asyncio.gather(*(fetch_chunk(*b) for b in bounds))

    seen: set[Any] = set()
    merged: list[dict[str, Any]] = []
    for item in (i for chunk in chunks for i in chunk):
        key = dedupe_key(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


async def _get_list_with_retry(
    url: str,
    params: dict[str, Any],
    *,
    api_secret_hash: Optional[str],
    api_token: Optional[str],
    timeout: int,
) -> list[Any]:
    """One GET expected to return a JSON list, retried on gateway errors."""
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        try:
            data = await _get_json(
                url, params=params, api_secret_hash=api_secret_hash,
                api_token=api_token, timeout=timeout,
            )
            break
        except NightscoutHTTPError as exc:
            if exc.status not in RETRYABLE_STATUSES or attempt == len(RETRY_DELAYS_SECONDS):
                raise
            await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])

    if not isinstance(data, list):
        raise NightscoutError("Nightscout returned an unexpected response shape.")
    return data


# Short, since /ns config waits on it.
SERVER_TYPE_TIMEOUT_SECONDS = 5


async def detect_server_type(base_url: str, *, timeout: int = SERVER_TYPE_TIMEOUT_SECONDS) -> Optional[str]:
    """
    "Nocturne" or "Nightscout" for the site at `base_url`, or None if it's
    neither (or unreachable). Needs no credentials.

    Nocturne serves the Nightscout API too, so it's told apart by its
    public OAuth metadata (`/.well-known/oauth-authorization-server`, whose
    issuer is "nocturne"), which Nightscout doesn't have. Anything else
    counts as Nightscout if its `/api/v1/status.json` answers with JSON --
    even a 401, which is what a locked-down Nightscout site returns.
    """
    base = base_url.rstrip("/")
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(f"{base}/.well-known/oauth-authorization-server") as resp:
                if resp.status == 200:
                    try:
                        metadata = await resp.json(content_type=None)
                    except Exception:  # noqa: BLE001 - not JSON, so not Nocturne
                        metadata = None
                    if isinstance(metadata, dict) and metadata.get("issuer") == "nocturne":
                        return "Nocturne"
            async with session.get(f"{base}/api/v1/status.json") as resp:
                try:
                    await resp.json(content_type=None)
                except Exception:  # noqa: BLE001 - not JSON, so not Nightscout
                    return None
                return "Nightscout"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None


async def fetch_status(
    base_url: str,
    *,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Fetch the site's `/api/v1/status.json`, which includes site settings
    such as `settings.customTitle` (the name shown in the Nightscout web UI).
    """
    if not base_url:
        raise NightscoutError("No Nightscout URL is configured.")

    url = f"{base_url.rstrip('/')}/api/v1/status.json"
    data = await _get_json(
        url, api_secret_hash=api_secret_hash, api_token=api_token, timeout=timeout
    )

    if not isinstance(data, dict):
        raise NightscoutError("Nightscout returned an unexpected response shape.")
    return data


async def fetch_latest_devicestatus(
    base_url: str,
    *,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[dict[str, Any]]:
    """
    Fetch the single most recent `/api/v1/devicestatus.json` entry, which
    (for users running a closed loop -- OpenAPS, AndroidAPS, or Loop) carries
    pump/loop data such as insulin-on-board. Returns None if the site has no
    device status data at all (most sites without a loop system won't).
    """
    if not base_url:
        raise NightscoutError("No Nightscout URL is configured.")

    url = f"{base_url.rstrip('/')}/api/v1/devicestatus.json"
    data = await _get_json(
        url, params={"count": 1}, api_secret_hash=api_secret_hash,
        api_token=api_token, timeout=timeout,
    )

    if not isinstance(data, list) or not data:
        return None
    return data[0] if isinstance(data[0], dict) else None


async def fetch_recent_devicestatus(
    base_url: str,
    *,
    count: int = 20,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """
    The `count` most recent devicestatus documents, newest first. Several
    are needed because uploaders often split data across documents (e.g.
    the phone app posts uploader battery while the loop posts pump data).
    """
    if not base_url:
        raise NightscoutError("No Nightscout URL is configured.")

    url = f"{base_url.rstrip('/')}/api/v1/devicestatus.json"
    data = await _get_list_with_retry(
        url, {"count": count}, api_secret_hash=api_secret_hash, api_token=api_token, timeout=timeout,
    )
    return [d for d in data if isinstance(d, dict)]


# How far back fetch_latest_treatment looks (e.g. for /ns ages).
LATEST_TREATMENT_LOOKBACK_DAYS = 365


async def fetch_latest_treatment(
    base_url: str,
    event_type: str,
    *,
    api_secret_hash: Optional[str] = None,
    api_token: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Optional[dict[str, Any]]:
    """
    The most recent treatment with exactly this `eventType` (e.g. "Site
    Change") in the last `LATEST_TREATMENT_LOOKBACK_DAYS`, or None. Filtered
    server-side, so it's one small request no matter how many other
    treatments (temp basals, boluses) the site has. The explicit date range
    matters: without one, Nocturne only searches roughly the last 4 days.
    """
    if not base_url:
        raise NightscoutError("No Nightscout URL is configured.")

    url = f"{base_url.rstrip('/')}/api/v1/treatments.json"
    since_ms = _now_ms() - LATEST_TREATMENT_LOOKBACK_DAYS * 86400 * 1000
    data = await _get_list_with_retry(
        url,
        {"find[eventType]": event_type, "count": 1, "find[created_at][$gte]": _iso_utc(since_ms)},
        api_secret_hash=api_secret_hash, api_token=api_token, timeout=timeout,
    )
    return data[0] if data and isinstance(data[0], dict) else None


def devicestatus_time_ms(doc: dict[str, Any]) -> Optional[int]:
    """A devicestatus document's own timestamp in epoch ms."""
    return treatment_time_ms(doc)


def _iso_or_ms_to_ms(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str):
        return treatment_time_ms({"created_at": value})
    return None


def _nested(doc: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def extract_uploader_battery(doc: dict[str, Any]) -> Optional[float]:
    """Phone / uploader battery %: Loop, AAPS and OpenAPS nest it under
    `uploader`; xDrip and older uploaders use a top-level field."""
    nested = _as_number(_nested(doc, "uploader", "battery"))
    return nested if nested is not None else _as_number(doc.get("uploaderBattery"))


def extract_pump_battery(doc: dict[str, Any]) -> Optional[str]:
    """Pump battery as "85%" or "1.38 V", whichever the uploader reports."""
    percent = _as_number(_nested(doc, "pump", "battery", "percent"))
    if percent is not None:
        return f"{percent:.0f}%"
    voltage = _as_number(_nested(doc, "pump", "battery", "voltage"))
    if voltage is not None:
        return f"{voltage:.2f} V"
    return None


def extract_reservoir(doc: dict[str, Any]) -> Optional[float]:
    return _as_number(_nested(doc, "pump", "reservoir"))


def extract_pump_suspended(doc: dict[str, Any]) -> Optional[bool]:
    for value in (_nested(doc, "pump", "status", "suspended"), _nested(doc, "pump", "suspended")):
        if isinstance(value, bool):
            return value
    return None


def extract_last_loop_ms(doc: dict[str, Any]) -> Optional[int]:
    """When the closed loop last ran (Loop, or OpenAPS / AAPS enacted or
    suggested), in epoch ms."""
    for path in (("loop", "timestamp"), ("openaps", "enacted", "timestamp"), ("openaps", "suggested", "timestamp")):
        ms = _iso_or_ms_to_ms(_nested(doc, *path))
        if ms is not None:
            return ms
    return None


# Nightscout's `direction` strings, mapped to the arrows familiar from
# Nightscout's own web UI and most CGM follower apps.
_DIRECTION_ARROWS = {
    "DoubleUp": "⇈",
    "SingleUp": "↑",
    "FortyFiveUp": "↗",
    "Flat": "→",
    "FortyFiveDown": "↘",
    "SingleDown": "↓",
    "DoubleDown": "⇊",
    "NOT COMPUTABLE": "?",
    "RATE OUT OF RANGE": "⚠",
    "NONE": "?",
}


def direction_to_arrow(direction: Optional[str]) -> str:
    return _DIRECTION_ARROWS.get(direction or "NONE", "?")


def extract_iob(devicestatus: Optional[dict[str, Any]]) -> Optional[float]:
    """
    Best-effort extraction of insulin-on-board from a devicestatus document.

    Closed-loop systems (OpenAPS, AndroidAPS, Loop) each shape this data a
    little differently, so a few known layouts are tried; if none match (or
    the user isn't running a loop system at all), None is returned and the
    caller should simply omit IOB rather than error out.
    """
    if not isinstance(devicestatus, dict):
        return None

    for source_key in ("openaps", "loop"):
        source = devicestatus.get(source_key)
        if not isinstance(source, dict):
            continue
        iob_field = source.get("iob")
        if isinstance(iob_field, dict):
            value = iob_field.get("iob")
            if isinstance(value, (int, float)):
                return float(value)
        elif isinstance(iob_field, list) and iob_field and isinstance(iob_field[0], dict):
            value = iob_field[0].get("iob")
            if isinstance(value, (int, float)):
                return float(value)

    pump = devicestatus.get("pump")
    if isinstance(pump, dict):
        iob_field = pump.get("iob")
        if isinstance(iob_field, dict):
            value = iob_field.get("iob", iob_field.get("bolusiob"))
            if isinstance(value, (int, float)):
                return float(value)

    return None
