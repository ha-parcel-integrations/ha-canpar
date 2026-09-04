"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping (the part you
rewrite per carrier) can be tested as plain functions.
"""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canpar.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.canpar.parcels import (
    apply_delivered_filter,
    build_history,
    canpar_delivery_window,
    canpar_local_timestamp,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    map_summary_status,
    normalize_parcel,
    parse_iso,
    safe_raw_payload,
    sort_parcels_by_ts,
    to_iso_timestamp,
)

from .payloads import active_sample, delivered_sample, pickup_sample

# ---------------------------------------------------------------------------
# map_parcel_status / map_event_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("PIC", ParcelStatus.IN_TRANSIT),
        ("ARR", ParcelStatus.IN_TRANSIT),
        ("SRT", ParcelStatus.IN_TRANSIT),
        ("WC ", ParcelStatus.OUT_FOR_DELIVERY),
        ("DRP", ParcelStatus.AT_PICKUP_POINT),
        ("NH", ParcelStatus.PROBLEM),
        ("DEL", ParcelStatus.DELIVERED),
        ("NSR", ParcelStatus.DELIVERED),
    ],
)
def test_map_parcel_status_known(code, expected):
    assert map_parcel_status(code) == expected


def test_map_parcel_status_missing_is_unknown():
    assert map_parcel_status(None) == ParcelStatus.UNKNOWN
    assert map_parcel_status("") == ParcelStatus.UNKNOWN


def test_map_parcel_status_unmapped_is_unknown():
    assert map_parcel_status("TELEPORTED") == ParcelStatus.UNKNOWN


def test_map_event_status_missing_and_unmapped_are_none():
    """History keeps ``null`` rather than ``unknown`` so consumers can tell
    "no mapping" from "mapped to unknown"."""
    assert map_event_status(None) is None
    assert map_event_status("SOMETHING_NEW") is None
    assert map_event_status("NSR") == ParcelStatus.DELIVERED


@pytest.mark.parametrize(
    "value,expected",
    [
        (1, ParcelStatus.IN_TRANSIT),
        (2, ParcelStatus.DELIVERED),
        (4, ParcelStatus.OUT_FOR_DELIVERY),
        (6, ParcelStatus.REGISTERED),
        (3, ParcelStatus.UNKNOWN),
        (5, ParcelStatus.UNKNOWN),
    ],
)
def test_map_summary_status(value, expected):
    assert map_summary_status(value) == expected


def test_unmapped_status_warns_only_once(caplog):
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert map_parcel_status("ABDUCTED") == ParcelStatus.UNKNOWN
    assert caplog.text.count("ABDUCTED") == 1
    assert "issues/new" in caplog.text


def test_unmapped_summary_status_warns_once(caplog):
    assert map_summary_status(9) == ParcelStatus.UNKNOWN
    assert map_summary_status(9) == ParcelStatus.UNKNOWN
    assert caplog.text.count("issues/new") == 1


# ---------------------------------------------------------------------------
# pre-1.0 one-shot warnings for unconfirmed shapes
# ---------------------------------------------------------------------------


def test_delivered_flag_disagreeing_with_status_warns_once(caplog):
    raw = delivered_sample()
    raw["delivered"] = False  # disagrees with the mapped NSR -> delivered event
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("delivered flag") == 1
    assert "issues/new" in caplog.text


def test_delivered_flag_agreeing_does_not_warn(caplog):
    normalize_parcel(delivered_sample())
    assert "delivered flag" not in caplog.text


def test_unrecognised_time_shift_warns_once(caplog):
    raw = active_sample()
    raw["events"][0]["time_shift"] = 99
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("time_shift=99") == 1


def test_known_time_shift_does_not_warn(caplog):
    normalize_parcel(active_sample())
    assert "time_shift" not in caplog.text


def test_time_shift_within_plausible_range_does_not_warn(caplog):
    """7 (observed since the 3-only baseline) is still a small positive int."""
    raw = active_sample()
    raw["events"][0]["time_shift"] = 7
    normalize_parcel(raw)
    assert "time_shift" not in caplog.text


def test_time_shift_outside_plausible_range_still_warns(caplog):
    raw = active_sample()
    raw["events"][0]["time_shift"] = -1
    normalize_parcel(raw)
    assert "time_shift=-1" in caplog.text


def test_populated_sensitive_result_field_warns_without_value(caplog):
    raw = delivered_sample()
    raw["signature_url"] = "https://example.invalid/secret-signature.png"
    normalize_parcel(raw)
    normalize_parcel(raw)
    assert caplog.text.count("signature_url") >= 1
    assert "example.invalid" not in caplog.text


def test_populated_sensitive_event_field_warns_without_value(caplog):
    raw = active_sample()
    raw["events"][0]["image_url"] = "https://example.invalid/secret-photo.jpg"
    normalize_parcel(raw)
    assert "image_url" in caplog.text
    assert "example.invalid" not in caplog.text


def test_unrecognised_address_key_warns_without_value(caplog):
    raw = active_sample()
    raw["events"][0]["address"] = {"city": "Test City", "gps_coordinates": "45.0,-75.0"}
    normalize_parcel(raw)
    assert "gps_coordinates" in caplog.text
    assert "45.0" not in caplog.text


def test_known_address_shape_does_not_warn(caplog):
    normalize_parcel(active_sample())
    assert "address" not in caplog.text.lower()


def test_address_bookkeeping_and_residential_keys_are_known(caplog):
    """address_id/id/inserted_on/updated_on/residential are now known keys."""
    raw = active_sample()
    raw["events"][0]["address"] = {
        "city": "Test City",
        "province": "ON",
        "address_id": 123,
        "id": 456,
        "inserted_on": "2026-01-01T00:00:00",
        "updated_on": "2026-01-01T00:00:00",
        "residential": True,
    }
    normalize_parcel(raw)
    assert "unrecognised field" not in caplog.text


def test_residential_flag_is_surfaced_in_raw_events():
    raw = active_sample()
    raw["events"][0]["address"] = {"city": "Test City", "residential": True}
    parcel = normalize_parcel(raw)
    assert parcel["raw"]["events"][0]["residential"] is True


def test_residential_flag_absent_is_omitted_from_raw_events():
    parcel = normalize_parcel(active_sample())
    assert "residential" not in parcel["raw"]["events"][0]


# ---------------------------------------------------------------------------
# timestamp helpers
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    # A naive value is assumed UTC so mixed lists still sort.
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def test_to_iso_timestamp_converts_epoch_milliseconds():
    assert to_iso_timestamp(1784203767167) == "2026-07-16T12:09:27.167000+00:00"
    assert to_iso_timestamp("2026-04-29T13:12:42Z") == "2026-04-29T13:12:42Z"
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp(10**20) is None  # out of range -> None, never raises


def test_canpar_time_helpers_preserve_local_wall_time():
    assert canpar_local_timestamp("20260101 080000") == "2026-01-01T08:00:00"
    assert canpar_local_timestamp("not-a-date") is None
    assert canpar_delivery_window("20260101") == (
        "2026-01-01T00:00:00",
        "2026-01-01T23:59:59",
    )
    assert canpar_delivery_window("not-a-date") == (None, None)


def test_format_dimensions_needs_all_three_axes():
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }
    assert format_dimensions(30, None, 10) is None


# ---------------------------------------------------------------------------
# build_history
# ---------------------------------------------------------------------------


def test_build_history_orders_canpar_events_oldest_to_newest():
    history = build_history(delivered_sample()["events"])
    assert [entry["raw_status"] for entry in history] == [
        "Sorting",
        "Out for delivery",
        "Delivered without signature",
    ]
    assert history[-1]["timestamp"] == "2026-01-01T14:00:00"
    assert history[-1]["status"] == ParcelStatus.DELIVERED


def test_build_history_handles_missing_and_malformed():
    assert build_history(None) == []
    assert build_history([{"code": "SRT"}]) == []  # no usable timestamp
    assert build_history(["not-a-dict"]) == []


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(delivered_sample())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    """A typo here would silently misreport this carrier on the docs site."""
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_match_what_normalize_parcel_actually_returns():
    """Every declared CAPABILITIES entry must come true somewhere in a sample.

    Copy this test into a real carrier's own test_parcels.py verbatim — it
    stays correct for whatever subset of CAPABILITIES that carrier declares.
    """
    delivered = normalize_parcel(delivered_sample())
    active = normalize_parcel(active_sample())
    pickup = normalize_parcel(pickup_sample())
    with_history = normalize_parcel(delivered_sample(), include_history=True)

    if "weight" in CAPABILITIES:
        assert delivered["weight"] is not None
    if "dimensions" in CAPABILITIES:
        assert delivered["dimensions"] is not None
    if "delivery_window" in CAPABILITIES:
        assert active["planned_from"] is not None or active["planned_to"] is not None
    if "pickup_point" in CAPABILITIES:
        assert pickup["pickup_point"] is not None
    if "url" in CAPABILITIES:
        assert delivered["url"] is not None
    if "history" in CAPABILITIES:
        assert with_history["history"] is not None


def test_normalize_delivered_parcel():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["carrier"] == "Canpar"
    assert parcel["barcode"] == "D000000000000000000002"
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["raw_status"] == "NSR"
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-01-01T14:00:00"
    # A delivered parcel drops its ETA — the window is meaningless once it has
    # arrived.
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["url"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None  # opt-in, default off


def test_normalize_history_is_opt_in():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert len(parcel["history"]) == 3
    assert parcel["history"][-1]["status"] == ParcelStatus.DELIVERED


def test_normalize_active_parcel_has_window():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] == ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["delivered"] is False
    assert parcel["planned_from"] == "2026-01-01T00:00:00"
    assert parcel["planned_to"] == "2026-01-01T23:59:59"


def test_normalize_pickup_parcel():
    parcel = normalize_parcel(pickup_sample())
    assert parcel["status"] == ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Test City, ON"


def test_normalize_pending_placeholder():
    """A tracked-but-not-yet-scanned code still yields a full parcel dict."""
    parcel = normalize_parcel({"barcode": "D000000000000000000003"})
    assert parcel["status"] == ParcelStatus.UNKNOWN
    assert parcel["delivered"] is False
    assert parcel["raw_status"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None


def test_normalize_blank_fields_become_none():
    raw = active_sample()
    raw["sender"] = ""
    raw["recipient"] = ""
    parcel = normalize_parcel(raw)
    assert parcel["sender"] is None
    assert parcel["receiver"] is None


def test_normalize_exposes_only_safe_raw_payload():
    raw = active_sample()
    raw["consignee_address"] = {"address_line_1": "Private street"}
    raw["events"][0]["address"] = {"city": "Private city"}
    raw["events"][0]["comment"] = "Private instruction"
    result = normalize_parcel(raw)["raw"]
    assert result == safe_raw_payload(raw)
    assert "consignee_address" not in result
    assert "address" not in result["events"][0]
    assert "comment" not in result["events"][0]


def test_normalize_falls_back_to_status_code_without_text():
    raw = active_sample()
    raw["statusCode"] = None
    assert normalize_parcel(raw)["raw_status"] == "WC"


def test_normalize_uses_ui_summary_status_without_events():
    parcel = normalize_parcel({"barcode": "D000000000000000000003", "status": 6})
    assert parcel["status"] == ParcelStatus.REGISTERED
    assert parcel["raw_status"] is None


def test_unknown_event_is_not_overridden_by_summary_status():
    raw = active_sample()
    raw["events"][0]["code"] = "NEW"
    raw["status"] = 4
    assert normalize_parcel(raw)["status"] == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    """Better to show a parcel with a broken date than to silently drop it."""
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
