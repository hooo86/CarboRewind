"""Authentication against the Carbonio LDAP directory.

Flow: bind with a read-only service account, search the user with an RFC 4515-escaped filter,
then bind as the found DN with the supplied password. ldap3 is synchronous, so callers run
:meth:`LdapAuthenticator.authenticate` in a worker thread.

Carbonio CE specifics (to verify on the target version): accounts have
``objectClass=zimbraAccount``; administrators carry ``zimbraIsAdminAccount=TRUE``; the
directory listens on 389 (StartTLS) on the LDAP master. The filter is configurable.
"""

from __future__ import annotations

import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass

from ldap3 import NONE, SUBTREE, Connection, Server, Tls
from ldap3.abstract.entry import Entry
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from ..config import Settings

log = logging.getLogger(__name__)

ConnectionFactory = Callable[[str | None, str | None], Connection]


@dataclass(frozen=True)
class LdapIdentity:
    dn: str
    username: str
    email: str | None
    display_name: str | None


class LdapConfigError(RuntimeError):
    """LDAP is misconfigured (e.g. plain-text transport)."""


def build_filter(template: str, username: str) -> str:
    """Insert the escaped username into the filter template (prevents LDAP injection)."""
    return template.replace("{username}", escape_filter_chars(username))


def _default_factory(settings: Settings) -> ConnectionFactory:
    url = settings.ldap_url or ""
    use_ssl = url.lower().startswith("ldaps://")
    if not use_ssl and not settings.ldap_start_tls:
        raise LdapConfigError("LDAP must use ldaps:// or StartTLS")
    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=settings.ldap_ca_file)
    server = Server(url, use_ssl=use_ssl, tls=tls, get_info=NONE, connect_timeout=5)

    def connect(user: str | None, password: str | None) -> Connection:
        conn = Connection(
            server,
            user=user,
            password=password,
            receive_timeout=int(settings.ldap_timeout),
            raise_exceptions=False,
            read_only=True,
        )
        if settings.ldap_start_tls and not use_ssl:
            conn.open()
            conn.start_tls()
        return conn

    return connect


class LdapAuthenticator:
    def __init__(self, settings: Settings, factory: ConnectionFactory | None = None) -> None:
        if not settings.ldap_enabled:
            raise LdapConfigError("LDAP is not configured")
        self._settings = settings
        self._connect = factory or _default_factory(settings)

    def authenticate(self, username: str, password: str) -> LdapIdentity | None:
        """Return the identity on success, None on bad credentials or unknown user."""
        # An empty password would make an *unauthenticated* simple bind succeed (RFC 4513 §5.1.2).
        if not username or not password:
            return None
        s = self._settings
        bind_password = s.ldap_bind_password.get_secret_value() if s.ldap_bind_password else None
        try:
            service = self._connect(s.ldap_bind_dn, bind_password)
            if not service.bind():
                log.error("LDAP service bind failed", extra={"result": service.result})
                return None
            try:
                service.search(
                    s.ldap_user_base or "",
                    build_filter(s.ldap_user_filter, username),
                    search_scope=SUBTREE,
                    attributes=["uid", "mail", "displayName", "cn"],
                    size_limit=2,
                )
                entries = list(service.entries)
            finally:
                service.unbind()
            if len(entries) != 1:  # unknown, or ambiguous: refuse
                return None
            entry = entries[0]
            user = self._connect(entry.entry_dn, password)
            try:
                if not user.bind():
                    return None
            finally:
                user.unbind()
        except LDAPException as exc:
            log.error("LDAP error during authentication", extra={"error": repr(exc)})
            return None
        return LdapIdentity(
            dn=entry.entry_dn,
            username=username,
            email=_first(entry, "mail"),
            display_name=_first(entry, "displayName") or _first(entry, "cn"),
        )


def _first(entry: Entry, attr: str) -> str | None:
    values = entry.entry_attributes_as_dict.get(attr) or []
    return str(values[0]) if values else None
