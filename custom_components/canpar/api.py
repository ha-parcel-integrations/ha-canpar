"""Client for Canpar's public, code-based tracking endpoint."""
from __future__ import annotations

from typing import Any

import aiohttp

from .const import TRACKING_API_URL


class CanparApiError(Exception):
    """Raised for an unexpected Canpar response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class CanparApiClient:
    """Look up a parcel without credentials or browser state."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with Home Assistant's shared session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Return a parcel, ``None`` for a known-empty lookup, or raise."""
        async with self._session.post(
            TRACKING_API_URL,
            json={"barcode": tracking_code, "track_shipment": True},
            headers={"Accept": "application/json"},
        ) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                raise CanparApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise CanparApiError(f"HTTP {response.status}", status_code=response.status)
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise CanparApiError("unparseable JSON response") from err

        if not isinstance(payload, dict):
            raise CanparApiError("unexpected body (not a JSON object)")
        if payload.get("error") is not None:
            raise CanparApiError("carrier rejected the tracking code")
        result = payload.get("result")
        if result == []:
            return None
        if not isinstance(result, list) or len(result) != 1:
            raise CanparApiError("unexpected result envelope")
        parcel = result[0]
        if not isinstance(parcel, dict):
            raise CanparApiError("unexpected parcel payload")
        return parcel
