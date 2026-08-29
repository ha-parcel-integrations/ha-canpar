"""Diagnostics support for the Canpar parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import CanparConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
# Fields from Canpar's address-bearing consumer response.
TO_REDACT = {
    # canonical fields we publish ourselves
    "tracking_code",
    "trackingNumber",
    "barcode",
    "sender",
    "receiver",
    "recipient",
    "url",
    # carrier payload fields
    "tracking_url_en",
    "tracking_url_fr",
    "reference_num",
    "consignee_address",
    "deliveryAddress",
    "address",
    "postal_code",
    "address_id",
    "address_line_1",
    "address_line_2",
    "address_line_3",
    "city",
    "province",
    "street",
    "email",
    "name",
    "attention",
    "phone",
    "extension",
    "comment",
    "employee_num",
    "route_num",
    "signed_by",
    "signature",
    "signature_url",
    "image_url",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CanparConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Canpar config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
