"""Tests for the Canpar config and options flow."""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canpar.config_flow import (
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.canpar.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_TRACKING_CODE,
    DOMAIN,
)


def _hub(parcels):
    return MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN, options={CONF_PARCELS: parcels})


async def _step(hass, entry, name):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": name})


def test_tracking_code_helpers():
    assert normalize_tracking_code("example 123-456") == "EXAMPLE123456"
    assert valid_tracking_code("EXAMPLE123456")
    assert not valid_tracking_code("ABC")


async def test_parcel_list(hass):
    entry = _hub([{CONF_TRACKING_CODE: "EXAMPLE111111"}])
    entry.add_to_hass(hass)
    result = await _step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tracking_codes": ["example222222", "EXAMPLE222222"]})
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EXAMPLE222222"}]


async def test_parcel_list_can_be_cleared(hass):
    entry = _hub([{CONF_TRACKING_CODE: "EXAMPLE111111"}])
    entry.add_to_hass(hass)
    result = await _step(hass, entry, "parcels")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": []}
    )
    assert result["data"][CONF_PARCELS] == []


async def test_settings_keep_parcels(hass):
    entry = _hub([{CONF_TRACKING_CODE: "EXAMPLE111111"}])
    entry.add_to_hass(hass)
    result = await _step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 5, CONF_INCLUDE_HISTORY: True})
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "EXAMPLE111111"}]
