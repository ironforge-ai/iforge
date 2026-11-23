"""`CORS_ORIGINS` replaces the default, and what that costs in development.

Setting the variable to add one origin silently removes every other. The origin
most often added locally is a LAN address — for a phone, or another machine on
the network — which leaves a checkout whose own dashboard cannot call its
backend. It fails as "We couldn't reach the service" on every panel while auth
still looks fine, because the access gate is server-rendered and never crosses
an origin.

The union is development-only, and these tests pin that narrowness as hard as
they pin the convenience: a backend that trusted localhost in production would
accept a browser running on the same host as any deployment.
"""

from __future__ import annotations

import pytest

from app.core.config import LOCAL_DASHBOARD_ORIGIN, Settings


def _settings(**overrides) -> Settings:
    """A Settings built without reading the developer's own .env file."""
    return Settings(_env_file=None, **overrides)


def test_development_keeps_the_local_dashboard_when_another_origin_is_configured():
    settings = _settings(
        environment="development",
        cors_origins=["http://192.168.1.50:3000"],
    )

    assert LOCAL_DASHBOARD_ORIGIN in settings.cors_origins
    assert "http://192.168.1.50:3000" in settings.cors_origins


def test_development_does_not_duplicate_an_already_listed_local_dashboard():
    settings = _settings(
        environment="development",
        cors_origins=[LOCAL_DASHBOARD_ORIGIN, "http://192.168.1.50:3000"],
    )

    assert settings.cors_origins.count(LOCAL_DASHBOARD_ORIGIN) == 1


def test_development_preserves_the_configured_order():
    """The configured origins stay first; the appended default never displaces one."""
    settings = _settings(
        environment="development",
        cors_origins=["http://a.test", "http://b.test"],
    )

    assert settings.cors_origins[:2] == ["http://a.test", "http://b.test"]


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_get_exactly_what_they_configured(environment):
    """No implicit localhost outside development, whatever the list contains.

    This is the half worth breaking a build over: a deployment that trusted
    `http://localhost:3000` would accept any browser running on the same host.
    """
    settings = _settings(
        environment=environment,
        cors_origins=["https://app.example.com"],
    )

    assert settings.cors_origins == ["https://app.example.com"]
    assert LOCAL_DASHBOARD_ORIGIN not in settings.cors_origins


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_keep_localhost_only_when_asked_explicitly(environment):
    """The rule adds; it never removes. An explicit choice is still honoured."""
    settings = _settings(
        environment=environment,
        cors_origins=[LOCAL_DASHBOARD_ORIGIN],
    )

    assert settings.cors_origins == [LOCAL_DASHBOARD_ORIGIN]


def test_the_development_default_alone_is_unchanged():
    settings = _settings(environment="development")

    assert settings.cors_origins == [LOCAL_DASHBOARD_ORIGIN]
