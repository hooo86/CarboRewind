from datetime import timedelta

import pytest

from carborewind_api.audit import GENESIS, canonical, chain_hash
from carborewind_api.auth.ldap import LdapAuthenticator, LdapConfigError, build_filter
from carborewind_api.auth.service import _lock_duration, normalise_username
from carborewind_api.config import Settings
from carborewind_api.security.passwords import (
    WeakPasswordError,
    check_policy,
    hash_password,
    needs_rehash,
    verify_password,
)
from carborewind_api.security.ratelimit import MemoryRateLimiter

from .conftest import ALICE_DN, Factory, SettingsFactory

# ----------------------------------------------------------------- passwords


def test_hash_and_verify() -> None:
    h = hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "correct horse battery staple")
    assert not verify_password(h, "wrong password!!")
    assert not needs_rehash(h)


def test_verify_without_hash_is_false() -> None:
    assert not verify_password(None, "whatever-password")


def test_verify_garbage_hash_is_false() -> None:
    assert not verify_password("not-a-hash", "whatever-password")


@pytest.mark.parametrize("password", ["short", "x" * 1025])
def test_policy_rejects(password: str) -> None:
    with pytest.raises(WeakPasswordError):
        check_policy(password)


# ----------------------------------------------------------------- lockout / usernames


def test_lock_duration_doubles_and_is_capped() -> None:
    s = Settings(login_max_failures=5, login_lock_base_minutes=1)
    assert _lock_duration(s, 5) == timedelta(minutes=1)
    assert _lock_duration(s, 6) == timedelta(minutes=2)
    assert _lock_duration(s, 8) == timedelta(minutes=8)
    assert _lock_duration(s, 40) == timedelta(days=1)


def test_normalise_username() -> None:
    assert normalise_username("  Admin@Example.TEST ") == "admin@example.test"


# ----------------------------------------------------------------- rate limiter


async def test_memory_rate_limiter() -> None:
    limiter = MemoryRateLimiter()
    assert [await limiter.hit("k", 2, 60) for _ in range(3)] == [True, True, False]
    assert await limiter.hit("other", 2, 60)


# ----------------------------------------------------------------- audit hashing


def test_chain_hash_is_deterministic_and_order_independent() -> None:
    a = {"b": 1, "a": "é"}
    b = {"a": "é", "b": 1}
    assert canonical(a) == canonical(b) == '{"a":"é","b":1}'
    assert chain_hash(GENESIS, a) == chain_hash(GENESIS, b)
    assert chain_hash(GENESIS, a) != chain_hash("1" * 64, a)


# ----------------------------------------------------------------- LDAP


def test_build_filter_escapes_injection() -> None:
    f = build_filter("(uid={username})", "*)(uid=*))(|(uid=*")
    assert f == r"(uid=\2a\29\28uid=\2a\29\29\28|\28uid=\2a)"


def test_ldap_authenticates_admin_by_uid_or_mail(
    ldap_factory: Factory, ldap_settings: SettingsFactory
) -> None:
    auth = LdapAuthenticator(ldap_settings(), ldap_factory)
    identity = auth.authenticate("alice", "alice-password-123")
    assert identity is not None
    assert identity.dn == ALICE_DN
    assert identity.email == "alice@example.test"
    assert identity.display_name == "Alice Admin"
    assert auth.authenticate("alice@example.test", "alice-password-123") is not None


def test_ldap_rejects_bad_password_non_admin_and_unknown(
    ldap_factory: Factory, ldap_settings: SettingsFactory
) -> None:
    auth = LdapAuthenticator(ldap_settings(), ldap_factory)
    assert auth.authenticate("alice", "wrong") is None
    assert auth.authenticate("bob", "bob-password-1234") is None  # not an admin account
    assert auth.authenticate("nobody", "x") is None


def test_ldap_rejects_empty_password(ldap_factory: Factory, ldap_settings: SettingsFactory) -> None:
    auth = LdapAuthenticator(ldap_settings(), ldap_factory)
    assert auth.authenticate("alice", "") is None


def test_ldap_injection_does_not_match_everyone(
    ldap_factory: Factory, ldap_settings: SettingsFactory
) -> None:
    auth = LdapAuthenticator(ldap_settings(), ldap_factory)
    assert auth.authenticate("*", "alice-password-123") is None


def test_ldap_service_bind_failure(ldap_factory: Factory, ldap_settings: SettingsFactory) -> None:
    auth = LdapAuthenticator(ldap_settings(ldap_bind_password="wrong"), ldap_factory)
    assert auth.authenticate("alice", "alice-password-123") is None


def test_ldap_requires_tls(ldap_settings: SettingsFactory) -> None:
    with pytest.raises(LdapConfigError):
        LdapAuthenticator(ldap_settings(ldap_url="ldap://ldap.example.test"))
    # StartTLS on ldap:// is accepted.
    LdapAuthenticator(ldap_settings(ldap_url="ldap://ldap.example.test", ldap_start_tls=True))


def test_ldap_requires_configuration() -> None:
    with pytest.raises(LdapConfigError):
        LdapAuthenticator(Settings())


@pytest.mark.parametrize(
    ("host", "expected"),
    [("192.0.2.1", "192.0.2.1"), ("2001:db8::1", "2001:db8::1"), ("testclient", None)],
)
def test_client_ip_validates(host: str, expected: str | None) -> None:
    from types import SimpleNamespace

    from carborewind_api.api.deps import client_ip

    request = SimpleNamespace(client=SimpleNamespace(host=host))
    assert client_ip(request) == expected  # type: ignore[arg-type]
    assert client_ip(SimpleNamespace(client=None)) is None  # type: ignore[arg-type]


async def test_redis_rate_limiter_uses_atomic_pipeline() -> None:
    from carborewind_api.security.ratelimit import RedisRateLimiter

    counts: dict[str, int] = {}
    expiries: dict[str, int] = {}

    class FakePipeline:
        def __init__(self) -> None:
            self.ops: list[tuple[str, str, int]] = []

        async def __aenter__(self) -> "FakePipeline":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        def incr(self, key: str) -> None:
            self.ops.append(("incr", key, 0))

        def expire(self, key: str, seconds: int) -> None:
            self.ops.append(("expire", key, seconds))

        async def execute(self) -> list[int | bool]:
            results: list[int | bool] = []
            for op, key, arg in self.ops:
                if op == "incr":
                    counts[key] = counts.get(key, 0) + 1
                    results.append(counts[key])
                else:
                    expiries[key] = arg
                    results.append(True)
            return results

    class FakeRedis:
        def pipeline(self, transaction: bool) -> FakePipeline:
            assert transaction
            return FakePipeline()

    limiter = RedisRateLimiter(FakeRedis())  # type: ignore[arg-type]
    assert [await limiter.hit("ip", 2, 60) for _ in range(3)] == [True, True, False]
    (key,) = counts
    assert key.startswith("carborewind:rl:ip:")
    assert expiries[key] == 60
