"""Minimal client for the Carbonio (Zimbra-derived) SOAP API, JSON flavour.

Carbonio inherits Zimbra's "SOAP" API, which accepts a JSON envelope::

    {"Header": {"context": {"_jsns": "urn:zimbra", "authToken": "…"}},
     "Body":   {"<Name>Request": {"_jsns": "urn:zimbraMail", …}}}

and answers ``{"Body": {"<Name>Response": {…}}}`` or ``{"Body": {"Fault": {…}}}`` with the
service error code in ``Fault.Detail.Error.Code`` (e.g. ``mail.MUST_RESYNC``).

ASSUMPTION (to verify with ``carborewind-agent carbonio probe`` on the target Carbonio CE
version): JSON envelope and fault layout as above, admin service on port 7071
(``/service/admin/soap``), user service on 443 (``/service/soap``).
No data from the caller is ever interpolated into a shell command: everything goes over HTTPS.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

NS_ACCOUNT = "urn:zimbraAccount"
NS_ADMIN = "urn:zimbraAdmin"
NS_MAIL = "urn:zimbraMail"

# Transient conditions worth a retry (network errors and these HTTP statuses).
_RETRY_STATUS = {502, 503, 504}


class SoapFaultError(Exception):
    """A Carbonio service error, e.g. ``account.NO_SUCH_ACCOUNT``."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class SoapClient:
    """Stateless request/response over one endpoint; auth token passed per call."""

    def __init__(
        self,
        http: httpx.Client,
        url: str,
        *,
        retries: int = 3,
        backoff: float = 0.5,
    ) -> None:
        self._http = http
        self._url = url
        self._retries = retries
        self._backoff = backoff

    def invoke(
        self,
        name: str,
        namespace: str,
        body: dict[str, Any] | None = None,
        *,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Call ``<name>Request`` and return the content of ``<name>Response``."""
        context: dict[str, Any] = {"_jsns": "urn:zimbra", "format": {"type": "js"}}
        if token:
            context["authToken"] = token
        envelope = {
            "Header": {"context": context},
            "Body": {f"{name}Request": {"_jsns": namespace, **(body or {})}},
        }
        response = self._post(envelope, name)
        try:
            payload = response.json()
        except ValueError as exc:
            snippet = " ".join(response.text.split())[:160]
            if response.status_code == 413:
                raise SoapFaultError(
                    "client.TOO_LARGE",
                    f"{name}: request too large for Carbonio (HTTP 413): {snippet!r}",
                ) from exc
            raise SoapFaultError(
                "client.BAD_RESPONSE",
                f"non-JSON answer to {name} (HTTP {response.status_code}): {snippet!r}",
            ) from exc
        fault = payload.get("Body", {}).get("Fault")
        if fault is not None:
            code = fault.get("Detail", {}).get("Error", {}).get("Code", "service.FAILURE")
            reason = fault.get("Reason", {}).get("Text", "")
            raise SoapFaultError(code, reason)
        try:
            result: dict[str, Any] = payload["Body"][f"{name}Response"]
        except KeyError as exc:
            raise SoapFaultError("client.BAD_RESPONSE", f"no {name}Response in answer") from exc
        return result

    def _post(self, envelope: dict[str, Any], name: str) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._http.post(self._url, json=envelope)
            except httpx.TransportError as exc:
                if attempt > self._retries:
                    raise SoapFaultError("client.NETWORK", f"{name}: {exc!r}") from exc
            else:
                if response.status_code not in _RETRY_STATUS or attempt > self._retries:
                    # SOAP faults come back as HTTP 500 with a JSON body: let invoke parse it.
                    return response
            delay = self._backoff * 2 ** (attempt - 1)
            log.warning("retrying SOAP call", extra={"call": name, "attempt": attempt})
            time.sleep(delay)


def content(value: Any) -> str | None:
    """Zimbra JSON wraps scalar elements as ``[{"_content": "…"}]`` or ``{"_content": …}``."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("_content")
    return None if value is None else str(value)
