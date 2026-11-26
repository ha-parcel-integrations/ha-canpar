"""Tests for Canpar sensor property logic."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from custom_components.canpar.const import ParcelStatus
from custom_components.canpar.sensor import (
    CanparAwaitingPickupSensor,
    CanparDeliveredParcelsSensor,
    CanparEnRouteToPickupPointSensor,
    CanparIncomingParcelsSensor,
    CanparLastUpdateSensor,
    CanparNextDeliverySensor,
    CanparParcelSensor,
)


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict], delivered: list[dict] | None = None) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    return coordinator


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    pickup: bool = False,
    planned_from: str | None = None,
) -> dict:
    return {
        "carrier": "Canpar",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = CanparIncomingParcelsSensor(coordinator, _entry(), lambda _: None, set())
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_awaiting_pickup_counts_every_parcel_at_pickup_point():
    ready = _parcel("READY", ParcelStatus.AT_PICKUP_POINT, pickup=True)
    not_ready = _parcel("TRANSIT", ParcelStatus.IN_TRANSIT, pickup=False)
    not_pickup = _parcel("NOT_PICKUP", ParcelStatus.AT_PICKUP_POINT, pickup=False)
    sensor = CanparAwaitingPickupSensor(
        _coordinator([ready, not_ready, not_pickup]), _entry()
    )
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_en_route_counts_only_active_pickup_parcels_not_yet_at_the_point():
    en_route = _parcel("ROUTE", ParcelStatus.IN_TRANSIT, pickup=True)
    arrived = _parcel("HERE", ParcelStatus.AT_PICKUP_POINT, pickup=True)
    returning = _parcel("BACK", ParcelStatus.RETURNING, pickup=True)
    delivered = _parcel("DONE", ParcelStatus.DELIVERED, pickup=False)
    home = _parcel("HOME", ParcelStatus.IN_TRANSIT, pickup=False)
    sensor = CanparEnRouteToPickupPointSensor(
        _coordinator([en_route, arrived, returning, delivered, home]), _entry()
    )
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"] == [en_route]


def test_en_route_zero_when_no_parcels():
    sensor = CanparEnRouteToPickupPointSensor(_coordinator([]), _entry())
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["parcels"] == []


def test_awaiting_pickup_zero_when_no_parcels():
    sensor = CanparAwaitingPickupSensor(_coordinator([]), _entry())
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["parcels"] == []


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = CanparParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_parcel_sensor_missing_barcode():
    sensor = CanparParcelSensor(_coordinator([_parcel("A")]), _entry(), "OTHER")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_earliest():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="2099-05-02T10:00:00Z"),
            _parcel("B", planned_from="2099-05-01T10:00:00Z"),
        ]
    )
    sensor = CanparNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2099, 5, 1, 10, 0, tzinfo=UTC)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_exposes_the_contents():
    parcel = _parcel("A", planned_from="2099-05-01T10:00:00Z")
    parcel["contents"] = "Running shoes"
    sensor = CanparNextDeliverySensor(_coordinator([parcel]), _entry())
    assert sensor.extra_state_attributes["contents"] == "Running shoes"


def test_next_delivery_skips_a_window_that_has_ended():
    coordinator = _coordinator(
        [
            _parcel("OLD", planned_from="2000-05-01T10:00:00Z"),
            _parcel("NEW", planned_from="2099-05-01T10:00:00Z"),
        ]
    )
    sensor = CanparNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "NEW"


def test_next_delivery_none_without_moments():
    sensor = CanparNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_skips_unparseable_moment():
    coordinator = _coordinator(
        [
            _parcel("A", planned_from="not-a-date"),
            _parcel("B", planned_from="2099-05-01T10:00:00Z"),
        ]
    )
    sensor = CanparNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_delivered_sensor():
    coordinator = _coordinator(
        [], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)]
    )
    sensor = CanparDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"][0]["barcode"] == "D"


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
    coordinator.last_success_time = moment
    sensor = CanparLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment
