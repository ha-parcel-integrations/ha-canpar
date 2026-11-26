"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.canpar.const import ParcelStatus
from custom_components.canpar.parcels import normalize_parcel, parse_iso, planned_window

from .payloads import active_sample, delivered_sample, event, pickup_sample

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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


def _returning() -> dict:
    sample = active_sample()
    sample["statusCode"] = "RTN"
    sample["events"] = [event("RTN", "20260102 090000"), *sample["events"]]
    return sample


def _problem() -> dict:
    sample = active_sample()
    sample["events"] = [event("HLD", "20260101 100000"), *sample["events"]]
    return sample


def _collected() -> dict:
    sample = pickup_sample()
    sample["statusCode"] = "DEL"
    sample["delivered"] = True
    sample["events"] = [
        event("DEL", "20260102 160000", "Collected"),
        *sample["events"],
    ]
    return sample


def _moment_only() -> dict:
    sample = active_sample()
    sample["estimated_delivery_date"] = "20260101"
    return sample


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "at_pickup_point": pickup_sample,
    "collected": _collected,
    "returning": _returning,
    "problem": _problem,
    "bare": lambda: {"barcode": "D000000000000000000009"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param]())


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    assert parcel["pickup_point"] is None


def test_a_collected_pickup_parcel_stays_pickup(toronto_zone):
    parcel = normalize_parcel(_collected())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-01-02T16:00:00-05:00"
    assert parcel["pickup"] is True


def test_a_home_delivery_is_not_pickup():
    assert normalize_parcel(delivered_sample())["pickup"] is False


def test_the_carrier_flag_does_not_decide_delivered():
    sample = delivered_sample()
    sample["delivered"] = False
    assert normalize_parcel(sample)["delivered"] is True
    sample = active_sample()
    sample["delivered"] = True
    assert normalize_parcel(sample)["delivered"] is False


def test_a_date_covers_the_whole_day():
    parcel = normalize_parcel(_moment_only())
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
    assert start.utcoffset() is not None


def test_a_moment_ends_at_the_end_of_that_day():
    zone = ZoneInfo("America/Toronto")
    start, end = planned_window("2026-01-01T13:00:00", None, zone=zone)
    assert start == "2026-01-01T13:00:00-05:00"
    assert end == "2026-01-01T23:59:59-05:00"
    assert planned_window("2026-01-01T23:59:59", None, zone=zone)[1] is None


def test_a_window_keeps_both_ends_and_swaps_reversed_ones():
    zone = ZoneInfo("America/Toronto")
    assert planned_window(
        "2026-01-01T09:00:00-05:00", "2026-01-01T11:00:00-05:00", zone=zone
    ) == ("2026-01-01T09:00:00-05:00", "2026-01-01T11:00:00-05:00")
    assert planned_window("2026-01-03", "2026-01-01", zone=zone) == (
        "2026-01-01T00:00:00-05:00",
        "2026-01-03T23:59:59-05:00",
    )
    assert planned_window(None, "2026-01-01", zone=zone)[0] == (
        "2026-01-01T00:00:00-05:00"
    )
    assert planned_window("garbage", None, zone=zone) == (None, None)
    assert planned_window(date(2026, 1, 1), datetime(2026, 1, 1, 8), zone=zone)[0] == (
        "2026-01-01T00:00:00-05:00"
    )
    assert planned_window(5, None, zone=zone) == (None, None)
