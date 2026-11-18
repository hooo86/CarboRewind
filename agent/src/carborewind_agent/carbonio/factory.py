"""Build a :class:`CarbonioClient` from the agent settings."""

from __future__ import annotations

import ssl

import httpx

from ..config import AgentSettings
from ..repository.keyfile import KeyFileError, read_secret
from .client import CarbonioClient
from .soap import SoapClient


def build_client(
    settings: AgentSettings, transport: httpx.BaseTransport | None = None
) -> CarbonioClient:
    """``transport`` is injected by tests (simulator); production uses real TLS sockets."""
    if transport is None:
        verify: ssl.SSLContext | bool = ssl.create_default_context(
            cafile=str(settings.carbonio_ca_file) if settings.carbonio_ca_file else None
        )
    else:
        verify = True
    timeout = httpx.Timeout(settings.carbonio_timeout, connect=10)
    admin_url = str(settings.soap_admin_url)
    mailbox = str(settings.mailbox_url).rstrip("/")
    admin_http = httpx.Client(timeout=timeout, verify=verify, transport=transport)
    mail_http = httpx.Client(timeout=timeout, verify=verify, transport=transport)
    rest_http = httpx.Client(base_url=mailbox, timeout=timeout, verify=verify, transport=transport)
    try:
        password = read_secret(settings.carbonio_admin_password_file)
    except KeyFileError:
        for client in (admin_http, mail_http, rest_http):
            client.close()
        raise
    return CarbonioClient(
        SoapClient(admin_http, admin_url),
        SoapClient(mail_http, f"{mailbox}/service/soap"),
        rest_http,
        settings.carbonio_admin_name,
        password,
    )
