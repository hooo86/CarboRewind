"""Shared fixtures: an in-memory (ldap3 MOCK_SYNC) Carbonio-like directory."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from carborewind_api.config import Settings

Factory = Callable[[str | None, str | None], Connection]
SettingsFactory = Callable[..., Settings]

BASE = "ou=people,dc=example,dc=test"
SERVICE_DN = "uid=carborewind,cn=appaccts,cn=zimbra"
ALICE_DN = f"uid=alice,{BASE}"


def _make_ldap_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ldap_url": "ldaps://ldap.example.test",
        "ldap_user_base": BASE,
        "ldap_bind_dn": SERVICE_DN,
        "ldap_bind_password": "service-secret",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def ldap_factory() -> Factory:
    server = Server("mock")
    seed = Connection(server, client_strategy=MOCK_SYNC)
    seed.strategy.add_entry(SERVICE_DN, {"userPassword": "service-secret", "uid": "carborewind"})
    seed.strategy.add_entry(
        ALICE_DN,
        {
            "objectClass": ["inetOrgPerson", "zimbraAccount"],
            "uid": "alice",
            "mail": "alice@example.test",
            "displayName": "Alice Admin",
            "zimbraIsAdminAccount": "TRUE",
            "zimbraAccountStatus": "active",
            "userPassword": "alice-password-123",
        },
    )
    seed.strategy.add_entry(
        f"uid=bob,{BASE}",
        {
            "objectClass": ["inetOrgPerson", "zimbraAccount"],
            "uid": "bob",
            "mail": "bob@example.test",
            "zimbraIsAdminAccount": "FALSE",
            "zimbraAccountStatus": "active",
            "userPassword": "bob-password-1234",
        },
    )

    def factory(user: str | None, password: str | None) -> Connection:
        return Connection(server, user=user, password=password, client_strategy=MOCK_SYNC)

    return factory


@pytest.fixture
def ldap_settings() -> SettingsFactory:
    return _make_ldap_settings
