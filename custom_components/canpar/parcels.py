"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Two things here are carrier-specific:
:data:`_STATUS_MAP` and :func:`normalize_parcel`. Everything else — the
timestamp parsing, the history builder, the sort contract, the delivered
filter, the one-shot warning for unmapped statuses — is suite-wide machinery
and should be left alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-canpar/issues/new"
    "?template=unrecognised_status.yml"
)

# Observed event-code mapping. Unmapped values surface as ``unknown`` with a
# one-shot warning so the vocabulary can grow safely.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "PIC": ParcelStatus.IN_TRANSIT,
    "ARR": ParcelStatus.IN_TRANSIT,
    "SRT": ParcelStatus.IN_TRANSIT,
    "WC": ParcelStatus.OUT_FOR_DELIVERY,
    "DRP": ParcelStatus.AT_PICKUP_POINT,
    "NH": ParcelStatus.PROBLEM,
    "DEL": ParcelStatus.DELIVERED,
    "NSR": ParcelStatus.DELIVERED,
    "DPT": ParcelStatus.IN_TRANSIT,
    "INO": ParcelStatus.IN_TRANSIT,  # handed to a local partner to deliver
    "COA": ParcelStatus.IN_TRANSIT,
    "XC": ParcelStatus.IN_TRANSIT,
    "WXX": ParcelStatus.IN_TRANSIT,  # weather delay, Canpar says it is moving
    "HLD": ParcelStatus.PROBLEM,  # delayed at a facility; sometimes the last scan
    "MIS": ParcelStatus.PROBLEM,  # sorted to the wrong terminal
    "NL": ParcelStatus.PROBLEM,  # "Can't find the address": a failed attempt, not a country code
    "RTN": ParcelStatus.RETURNING,
}

# The public tracking UI exposes this numeric summary alongside the latest
# event. Use it only when the response has no event code at all: the event is
# more specific, and an unfamiliar event must remain visible as ``unknown``.
_SUMMARY_STATUS_MAP: dict[int, ParcelStatus] = {
    1: ParcelStatus.IN_TRANSIT,
    2: ParcelStatus.DELIVERED,
    4: ParcelStatus.OUT_FOR_DELIVERY,
    6: ParcelStatus.REGISTERED,
}

# Keys already warned about, so each unconfirmed shape is logged only once
# per HA session instead of on every poll. One shared set: the warnings below
# are rare and distinct enough that a single namespace-by-prefix set is
# simpler than one set per warning kind.
_warned: set[str] = set()

# events[].address fields we expect on every observed shape. A field added or
# removed is a shape change worth a warning; values are never logged.
#
# ``address_id``, ``id``, ``inserted_on`` and ``updated_on`` are Canpar's own
# record bookkeeping (internal ids and audit timestamps) — confirmed present,
# never useful to a HA user, so just known-and-ignored.
# ``residential`` is a genuine non-PII signal (unlike the surrounding address
# fields, which stay redacted); it is read from the event address.
_KNOWN_ADDRESS_KEYS = frozenset(
    {
        "name",
        "address_line_1",
        "address_line_2",
        "address_line_3",
        "attention",
        "phone",
        "extension",
        "email",
        "city",
        "province",
        "postal_code",
        "country",
        "address_id",
        "id",
        "inserted_on",
        "updated_on",
        "residential",
    }
)

# Sensitive fields that were null/absent in the only capture this integration
# is built from. A populated one is new evidence, worth a warning — but never
# its value, since these fields can carry a delivery photo or a name.
_SENSITIVE_EVENT_FIELDS = ("image_url",)
_SENSITIVE_RESULT_FIELDS = (
    "signature_url",
    "signed_by",
    "signature",
    "consignee_address",
)


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message, *args)


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    _warn_once(
        f"status:{code}",
        "Unrecognised Canpar status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def _warn_delivered_mismatch(raw_delivered: Any, status: ParcelStatus) -> None:
    """Warn once when ``delivered`` disagrees with the newest mapped event."""
    _warn_once(
        "delivered-mismatch",
        "Canpar's delivered flag (%s) disagrees with the newest mapped "
        "status (%s) — open an issue and paste this line: %s",
        raw_delivered,
        status,
        NEW_ISSUE_URL,
    )


# time_shift's meaning is still unconfirmed (not a UTC offset — see
# canpar_local_timestamp / CLAUDE.md), but every value observed so far (3 on
# an Ontario/Quebec route, 7 since) is a small positive integer, consistent
# with a bounded regional/zone code across Canada's handful of time zones
# rather than an open-ended one. Validate the *shape* (a plausible small
# integer) instead of hard-coding each newly observed value as its own
# enum member — that would never stop growing and would tell us nothing
# past "it happened again". Anything outside this range is still a genuine
# surprise worth a warning.
_TIME_SHIFT_MIN = 0
_TIME_SHIFT_MAX = 12


def _warn_time_shift(value: Any) -> None:
    """Warn once for a ``time_shift`` outside the plausible small-integer range."""
    if isinstance(value, int) and _TIME_SHIFT_MIN <= value <= _TIME_SHIFT_MAX:
        return
    _warn_once(
        "time-shift",
        "Unrecognised Canpar time_shift value — open an issue and paste "
        "this line: %s\n  time_shift=%r",
        NEW_ISSUE_URL,
        value,
    )


def _warn_sensitive_field(field: str) -> None:
    """Warn once that a field this integration never exposes was populated — keys only."""
    _warn_once(
        f"sensitive:{field}",
        "Canpar response populated the %s field, which this integration "
        "does not expose — open an issue (no need to attach the value): %s",
        field,
        NEW_ISSUE_URL,
    )


def _warn_address_shape(address: dict) -> None:
    """Warn once when events[].address carries a key outside the known shape."""
    unexpected = frozenset(address) - _KNOWN_ADDRESS_KEYS
    if not unexpected:
        return
    _warn_once(
        f"address-shape:{','.join(sorted(unexpected))}",
        "Canpar events[].address has an unrecognised field — open an "
        "issue and paste this line: %s\n  unexpected keys=%s",
        NEW_ISSUE_URL,
        sorted(unexpected),
    )


def _warn_event_shape(event: dict) -> None:
    """Run the per-event one-shot checks: time_shift, address shape, image_url."""
    if "time_shift" in event:
        _warn_time_shift(event.get("time_shift"))
    address = event.get("address")
    if isinstance(address, dict):
        _warn_address_shape(address)
    for field in _SENSITIVE_EVENT_FIELDS:
        if event.get(field):
            _warn_sensitive_field(field)


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    code = code.strip()
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    code = code.strip()
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def map_summary_status(value: Any) -> ParcelStatus:
    """Map Canpar's numeric UI summary only when no event code is available."""
    try:
        return _SUMMARY_STATUS_MAP[int(value)]
    except (KeyError, TypeError, ValueError):
        _warn_once(
            f"summary-status:{value!r}",
            "Unrecognised Canpar summary status — help us map it. Open an "
            "issue and paste this line: %s\n  status=%r → reported as 'unknown'",
            NEW_ISSUE_URL,
            value,
        )
        return ParcelStatus.UNKNOWN


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def canpar_local_timestamp(value: Any) -> str | None:
    """Format Canpar's offset-less event clock as ISO with an offset.

    No field says which Canadian zone an event is in, so the wall time is read
    in Home Assistant's zone.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y%m%d %H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt_util.get_default_time_zone()).isoformat()


def canpar_eta_date(value: Any) -> str | None:
    """Return Canpar's date-only ETA as an ISO date, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


# Links that open a signature or delivery photo; the one thing ``raw`` omits.
_RAW_OMITTED_RESULT_KEYS = frozenset({"signature_url", "signature"})
_RAW_OMITTED_EVENT_KEYS = frozenset({"image_url", "signature_url", "signature"})


def safe_raw_payload(raw: dict) -> dict:
    """Return the full carrier record minus signature and image links."""
    result = {k: v for k, v in raw.items() if k not in _RAW_OMITTED_RESULT_KEYS}
    if isinstance(raw.get("events"), list):
        result["events"] = [
            {k: v for k, v in event.items() if k not in _RAW_OMITTED_EVENT_KEYS}
            if isinstance(event, dict)
            else event
            for event in raw["events"]
        ]
    return result


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{round(length)} x {round(width)} x {round(height)} cm",
    }


def build_history(
    events: list | None, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own text, or
    its event code when the API has no human-readable text. Sorted oldest →
    newest and capped to the most recent ``max_events``.

    Canpar provides offset-less local wall times; they are read in Home
    Assistant's zone. Source events are newest-first.
    """
    entries: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        _warn_event_shape(event)
        timestamp = canpar_local_timestamp(event.get("local_date_time"))
        if not timestamp:
            continue
        entry = {
            "timestamp": timestamp,
            "status": map_event_status(event.get("code")),
            "raw_status": event.get("code_description_en") or event.get("code"),
        }
        entries.append(entry)
    # Canpar's consumer UI receives and displays these entries newest-first.
    return list(reversed(entries))[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    # The observed carrier URLs are HTTP and include the tracking code. Do not
    # expose them until an HTTPS consumer deep link is confirmed.
    return None


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {ParcelStatus.AT_PICKUP_POINT, ParcelStatus.RETURNING, ParcelStatus.DELIVERED}
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are
    the contract**: every carrier in the suite returns exactly these, in this
    order, and the aggregator and cross-carrier dashboards depend on it. Set a
    key to ``None`` when the carrier does not expose it — never omit it.

    Rules worth keeping when you rewrite the body:

    * ``status`` is canonical, ``raw_status`` is the carrier's own text.
    * ``delivered`` is ``status is DELIVERED``; ``delivered_at`` only then.
    * ``planned_from`` / ``planned_to`` come from :func:`planned_window` and are
      emptied by :func:`planned_for_status`.
    * ``pickup`` comes from :func:`is_pickup` and stays ``True`` after collection.
    * ``weight`` is kilograms, ``dimensions`` centimetres (see
      :func:`format_dimensions`).
    * ``history`` is ``None`` when the option is off — the key still exists.
    """
    tracking_code = raw.get("barcode")
    events = raw.get("events") or []
    newest_event = events[0] if events and isinstance(events[0], dict) else {}
    if newest_event:
        _warn_event_shape(newest_event)
    for field in _SENSITIVE_RESULT_FIELDS:
        if raw.get(field):
            _warn_sensitive_field(field)
    status_code = (
        str(newest_event.get("code") or raw.get("statusCode") or "").strip() or None
    )
    status = (
        map_parcel_status(status_code)
        if status_code
        else map_summary_status(raw.get("status"))
    )
    raw_delivered = raw.get("delivered")
    if isinstance(raw_delivered, bool) and raw_delivered != (
        status is ParcelStatus.DELIVERED
    ):
        _warn_delivered_mismatch(raw_delivered, status)
    delivered = status is ParcelStatus.DELIVERED
    delivered_at = (
        canpar_local_timestamp(newest_event.get("local_date_time"))
        if delivered
        else None
    )
    planned_from, planned_to = planned_for_status(
        status,
        *planned_window(
            canpar_eta_date(raw.get("estimated_delivery_date")),
            None,
            zone=dt_util.get_default_time_zone(),
        ),
    )
    pickup = is_pickup(
        None,
        status,
        (
            map_event_status(event.get("code"))
            for event in events
            if isinstance(event, dict)
        ),
    )

    return {
        "carrier": "Canpar",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": status_code,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": safe_raw_payload(raw),
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
