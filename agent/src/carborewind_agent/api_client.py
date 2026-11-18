"""HTTPS client of the CarboRewind API, authenticated by the agent's client certificate."""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import __version__
from .config import AgentSettings


class ApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def tls_context(server_ca: Path | None, client: tuple[Path, Path] | None = None) -> ssl.SSLContext:
    """TLS context verifying the API (``server_ca`` or system CAs), with the agent's client
    certificate when given. Built explicitly: httpx ignores ``cert=`` combined with a
    CA-file ``verify`` in recent versions, which silently dropped the client certificate."""
    context = ssl.create_default_context(cafile=str(server_ca) if server_ca else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if client is not None:
        context.load_cert_chain(certfile=str(client[0]), keyfile=str(client[1]))
    return context


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def enroll(
    settings: AgentSettings,
    token: str,
    server_ca: Path | None = None,
    http: httpx.Client | None = None,
) -> str:
    """Create the agent key pair (kept local), send a CSR, store the issued certificate.

    ``server_ca`` verifies the API's TLS certificate during enrolment (system CAs otherwise).
    Returns the agent id.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "carborewind-agent")]))
        .sign(key, hashes.SHA256())
    )
    client = http or httpx.Client(
        base_url=str(settings.api_url), verify=tls_context(server_ca), timeout=30
    )
    try:
        response = client.post(
            "/api/agent/enroll",
            json={
                "token": token,
                "csr_pem": csr.public_bytes(serialization.Encoding.PEM).decode(),
                "version": __version__,
            },
        )
    finally:
        if http is None:
            client.close()
    if response.status_code != 200:
        raise ApiError(response.status_code, response.text[:200])
    body = response.json()
    _write_private(
        settings.tls_key,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    _write_private(settings.tls_cert, body["certificate_pem"].encode())
    _write_private(settings.tls_ca, body["ca_pem"].encode())
    return str(body["agent_id"])


class AgentApi:
    """Job protocol calls. ``http`` is injected by tests (no client certificate then)."""

    def __init__(
        self,
        settings: AgentSettings,
        http: httpx.Client | None = None,
        server_ca: Path | None = None,
    ) -> None:
        self._http = http or httpx.Client(
            base_url=str(settings.api_url),
            timeout=60,
            verify=tls_context(server_ca, (settings.tls_cert, settings.tls_key)),
        )

    def _post(self, path: str, body: Any = None) -> httpx.Response:
        response = self._http.post(f"/api/agent{path}", json=body)
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text[:200])
        return response

    def inventory(
        self,
        accounts: list[dict[str, Any]],
        complete: bool,
        storage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"accounts": accounts, "complete": complete}
        if storage is not None:
            body["storage"] = storage
        result: dict[str, Any] = self._post("/inventory", body).json()
        return result

    def claim(self) -> dict[str, Any] | None:
        response = self._post("/jobs/claim")
        if response.status_code == 204:
            return None
        job: dict[str, Any] = response.json()
        return job

    def heartbeat(
        self, job_id: str, checkpoint: dict[str, Any] | None, progress: dict[str, Any] | None = None
    ) -> None:
        self._post(f"/jobs/{job_id}/heartbeat", {"checkpoint": checkpoint, "progress": progress})

    def complete(self, job_id: str, payload: dict[str, Any]) -> None:
        self._post(f"/jobs/{job_id}/complete", payload)

    def purge_complete(self, job_id: str, payload: dict[str, Any]) -> None:
        self._post(f"/jobs/{job_id}/purge-complete", payload)

    def restore_complete(self, job_id: str, payload: dict[str, Any]) -> None:
        self._post(f"/jobs/{job_id}/restore-complete", payload)

    def fail(self, job_id: str, error: str, retryable: bool) -> None:
        self._post(f"/jobs/{job_id}/fail", {"error": error[:2000], "retryable": retryable})

    def close(self) -> None:
        self._http.close()
