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

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry

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
_KNOWN_ADDRESS_KEYS = frozenset({
    "name", "address_line_1", "address_line_2", "address_line_3",
    "attention", "phone", "extension", "email", "city", "province",
    "postal_code", "country",
})

# Sensitive fields that were null/absent in the only capture this integration
# is built from. A populated one is new evidence, worth a warning — but never
# its value, since these fields can carry a delivery photo or a name.
_SENSITIVE_EVENT_FIELDS = ("image_url",)
_SENSITIVE_RESULT_FIELDS = ("signature_url", "signed_by", "signature", "consignee_address")


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


def _warn_time_shift(value: Any) -> None:
    """Warn once for a ``time_shift`` other than the only value seen so far (``3``)."""
    if isinstance(value, int) and value == 3:
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
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
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
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def canpar_local_timestamp(value: Any) -> str | None:
    """Format Canpar's offset-less event clock without inventing a timezone."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d %H%M%S").isoformat()
    except ValueError:
        return None


def canpar_delivery_window(value: Any) -> tuple[str | None, str | None]:
    """Expand Canpar's date-only ETA to its local calendar-day bounds."""
    if not isinstance(value, str):
        return None, None
    try:
        date = datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        return None, None
    return f"{date}T00:00:00", f"{date}T23:59:59"


def safe_raw_payload(raw: dict) -> dict:
    """Return carrier metadata safe to expose in Home Assistant attributes.

    Canpar's response includes recipient addresses, contact data, signatures
    and tracking URLs. ``raw`` is an entity attribute, not a private cache, so
    retain only status and timing metadata needed for debugging.
    """
    safe_events = []
    for event in raw.get("events") or []:
        if not isinstance(event, dict):
            continue
        safe_events.append({
            key: event[key]
            for key in (
                "code",
                "code_description_en",
                "code_description_fr",
                "local_date_time",
                "time_shift",
            )
            if key in event
        })
    return {
        key: raw[key]
        for key in (
            "status",
            "status_description_en",
            "status_description_fr",
            "delivered",
            "shipping_date",
            "estimated_delivery_date",
            "pd_flag",
            "pd_flag_desc_en",
            "pd_flag_desc_fr",
        )
        if key in raw
    } | {"events": safe_events}


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
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
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

    Canpar provides offset-less local wall times. Preserve them as naive ISO
    strings rather than inventing a timezone; source events are newest-first.
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


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are
    the contract**: every carrier in the suite returns exactly these, in this
    order, and the aggregator and cross-carrier dashboards depend on it. Set a
    key to ``None`` when the carrier does not expose it — never omit it.

    Rules worth keeping when you rewrite the body:

    * ``status`` is canonical, ``raw_status`` is the carrier's own text.
    * A delivered parcel has ``delivered_at`` set and ``planned_from`` /
      ``planned_to`` cleared — the ETA is meaningless once it has arrived.
    * ``planned_to`` is ``None`` for a point estimate; only fill it when the
      carrier genuinely reports a *window*.
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
    status_code = str(newest_event.get("code") or raw.get("statusCode") or "").strip() or None
    status = map_parcel_status(status_code) if status_code else map_summary_status(raw.get("status"))
    raw_delivered = raw.get("delivered")
    if isinstance(raw_delivered, bool) and raw_delivered != (status is ParcelStatus.DELIVERED):
        _warn_delivered_mismatch(raw_delivered, status)
    delivered = raw_delivered is True and status is ParcelStatus.DELIVERED
    address = newest_event.get("address") or {}
    pickup_point = None
    if status is ParcelStatus.AT_PICKUP_POINT:
        pickup_point = ", ".join(
            str(value) for value in (address.get("city"), address.get("province")) if value
        ) or None

    delivered_at = canpar_local_timestamp(newest_event.get("local_date_time")) if delivered else None
    planned_from, planned_to = (None, None) if delivered else canpar_delivery_window(raw.get("estimated_delivery_date"))

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
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": pickup_point,
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
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
