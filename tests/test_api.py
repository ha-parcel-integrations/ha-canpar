"""Tests for the Canpar API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.canpar.api import CanparApiClient, CanparApiError

CODE = "D000000000000000000002"


def _session_returning(status: int, body: object = None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0) if isinstance(body, str) else None, return_value=None if isinstance(body, str) else body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


async def test_get_parcel_posts_expected_envelope():
    session = _session_returning(200, {"error": None, "result": [{"barcode": CODE}]})
    assert (await CanparApiClient(session).async_get_parcel(CODE))["barcode"] == CODE
    assert session.post.call_args.kwargs["json"] == {"barcode": CODE, "track_shipment": True}


async def test_get_parcel_returns_none_for_empty_result():
    client = CanparApiClient(_session_returning(200, {"error": None, "result": []}))
    assert await client.async_get_parcel(CODE) is None


@pytest.mark.parametrize("body", [{"error": "Fatal error", "result": None}, {"error": None, "result": None}, {"error": None, "result": [{}, {}]}])
async def test_get_parcel_rejects_bad_envelopes(body):
    with pytest.raises(CanparApiError):
        await CanparApiClient(_session_returning(200, body)).async_get_parcel(CODE)


async def test_get_parcel_rejects_http_and_json_errors():
    with pytest.raises(CanparApiError):
        await CanparApiClient(_session_returning(500, {})).async_get_parcel(CODE)
    with pytest.raises(CanparApiError):
        await CanparApiClient(_session_returning(200, "not json")).async_get_parcel(CODE)


async def test_get_parcel_propagates_network_error():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await CanparApiClient(session).async_get_parcel(CODE)
