"""Synthetic, privacy-safe Canpar payloads based on observed field shapes."""
from __future__ import annotations

ACTIVE_CODE = "D000000000000000000001"
DELIVERED_CODE = "D000000000000000000002"


def event(code: str, timestamp: str, description: str = "") -> dict:
    """Return one synthetic Canpar event; timestamps deliberately lack offsets."""
    return {
        "code": code,
        "local_date_time": timestamp,
        "time_shift": 3,
        "code_description_en": description,
        "web_description_en": description,
        "address": {"city": "Test City", "province": "ON"},
        "comment": None,
        "employee_num": "TEST",
        "route_num": None,
        "image_url": None,
    }


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A delivered parcel with newest-first events."""
    return {
        "barcode": code,
        "statusCode": "NSR",
        "delivered": True,
        "status": 2,
        "estimated_delivery_date": "20260101",
        "events": [
            event("NSR", "20260101 140000", "Delivered without signature"),
            event("WC ", "20260101 080000", "Out for delivery"),
            event("SRT", "20251231 220000", "Sorting"),
        ],
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel."""
    return {
        "barcode": code,
        "statusCode": "WC ",
        "delivered": False,
        "status": 1,
        "estimated_delivery_date": "20260101",
        "events": [event("WC ", "20260101 080000", "Out for delivery")],
    }


def pickup_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel at a SMARTSpot pickup facility."""
    return {
        "barcode": code,
        "statusCode": "DRP",
        "delivered": False,
        "status": 1,
        "events": [event("DRP", "20260101 080000", "Ready for pickup")],
    }
