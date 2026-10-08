"""T-49: production TURN configuration.

These tests only inspect generated configuration values. Nothing here opens a
socket to turn.meetakk.com or to any other TURN/STUN server, and the only
credential used is the fake constant below.
"""

import importlib
from pathlib import Path

from django.test import override_settings

from apps.events.turn import TurnCredentialService

FAKE_SHARED_SECRET = "t49-fake-turn-secret-not-a-real-credential"
PRODUCTION_DOMAIN = "turn.meetakk.com"
PRODUCTION_PORT = 3478

EXPECTED_UDP_URL = "turn:turn.meetakk.com:3478"
EXPECTED_TCP_URL = "turn:turn.meetakk.com:3478?transport=tcp"

REPO_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SETTINGS = REPO_ROOT / "config" / "settings" / "production.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

TURN_ENV_VAR_NAMES = [
    "COTURN_REGION1_DOMAIN",
    "COTURN_PORT",
    "COTURN_TLS_PORT",
    "COTURN_SHARED_SECRET",
    "COTURN_CREDENTIAL_TTL_SECONDS",
    "COTURN_UDP_TRANSPORT_PARAM",
    "COTURN_REGION2_DOMAIN",
    "COTURN_REGION2_SHARED_SECRET",
]


def load_production_settings(monkeypatch, **env):
    """Re-import config.settings.production with the given environment."""
    for name in TURN_ENV_VAR_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    module = importlib.import_module("config.settings.production")
    return importlib.reload(module)


def production_turn_overrides(module):
    return {
        "COTURN_REGION1_DOMAIN": module.COTURN_REGION1_DOMAIN,
        "COTURN_PORT": module.COTURN_PORT,
        "COTURN_TLS_PORT": module.COTURN_TLS_PORT,
        "COTURN_UDP_TRANSPORT_PARAM": module.COTURN_UDP_TRANSPORT_PARAM,
        "COTURN_CREDENTIAL_TTL_SECONDS": module.COTURN_CREDENTIAL_TTL_SECONDS,
        "COTURN_REGION2_DOMAIN": module.COTURN_REGION2_DOMAIN,
        "COTURN_REGION2_PORT": module.COTURN_REGION2_PORT,
        "COTURN_REGION2_TLS_PORT": module.COTURN_REGION2_TLS_PORT,
        "COTURN_REGION2_SHARED_SECRET": module.COTURN_REGION2_SHARED_SECRET,
    }


class TestProductionTurnEndpoint:
    @override_settings(
        COTURN_REGION1_DOMAIN=PRODUCTION_DOMAIN,
        COTURN_PORT=PRODUCTION_PORT,
        COTURN_TLS_PORT=5349,
        COTURN_UDP_TRANSPORT_PARAM=False,
        COTURN_SHARED_SECRET=FAKE_SHARED_SECRET,
        COTURN_REGION2_DOMAIN=None,
        COTURN_CREDENTIAL_TTL_SECONDS=1800,
    )
    def test_primary_turn_urls_are_generated_exactly(self):
        service = TurnCredentialService()
        result = service.generate_credentials(participant_id="t49-participant")

        turn_server = result["ice_servers"][1]
        assert turn_server["urls"] == [EXPECTED_UDP_URL, EXPECTED_TCP_URL]

    @override_settings(
        COTURN_REGION1_DOMAIN=PRODUCTION_DOMAIN,
        COTURN_PORT=PRODUCTION_PORT,
        COTURN_TLS_PORT=5349,
        COTURN_UDP_TRANSPORT_PARAM=False,
        COTURN_SHARED_SECRET=FAKE_SHARED_SECRET,
        COTURN_REGION2_DOMAIN=None,
        COTURN_CREDENTIAL_TTL_SECONDS=1800,
    )
    def test_shared_secret_is_not_exposed_to_the_client(self):
        service = TurnCredentialService()
        result = service.generate_credentials(participant_id="t49-participant")

        assert FAKE_SHARED_SECRET not in str(result)
        turn_server = result["ice_servers"][1]
        assert turn_server["username"].endswith(":t49-participant")
        assert turn_server["credential"]


class TestProductionTurnSettings:
    def test_turn_address_and_credentials_come_from_environment(self, monkeypatch):
        module = load_production_settings(
            monkeypatch,
            COTURN_REGION1_DOMAIN="turn.example-from-env.test",
            COTURN_PORT="3999",
            COTURN_TLS_PORT="5999",
            COTURN_SHARED_SECRET=FAKE_SHARED_SECRET,
            COTURN_CREDENTIAL_TTL_SECONDS="600",
        )

        assert module.COTURN_REGION1_DOMAIN == "turn.example-from-env.test"
        assert module.COTURN_PORT == 3999
        assert module.COTURN_TLS_PORT == 5999
        assert module.COTURN_SHARED_SECRET == FAKE_SHARED_SECRET
        assert module.COTURN_CREDENTIAL_TTL_SECONDS == 600

    def test_settings_defaults_generate_exact_primary_turn_urls(self, monkeypatch):
        module = load_production_settings(monkeypatch)

        assert module.COTURN_REGION1_DOMAIN == PRODUCTION_DOMAIN
        assert module.COTURN_PORT == PRODUCTION_PORT
        assert module.COTURN_UDP_TRANSPORT_PARAM is False

        with override_settings(
            COTURN_SHARED_SECRET=FAKE_SHARED_SECRET,
            **production_turn_overrides(module),
        ):
            service = TurnCredentialService()
            result = service.generate_credentials(participant_id="t49-defaults")

        assert result["ice_servers"][1]["urls"] == [
            EXPECTED_UDP_URL,
            EXPECTED_TCP_URL,
        ]

    def test_second_region_is_disabled_by_default(self, monkeypatch):
        module = load_production_settings(monkeypatch)

        assert module.COTURN_REGION2_DOMAIN is None

        with override_settings(
            COTURN_SHARED_SECRET=FAKE_SHARED_SECRET,
            **production_turn_overrides(module),
        ):
            service = TurnCredentialService()
            result = service.generate_credentials(participant_id="t49-single-region")

        assert result["regions"] == ["region-1-staging"]
        assert len(result["ice_servers"]) == 3

    def test_no_shared_secret_is_hardcoded_in_production_settings(self, monkeypatch):
        module = load_production_settings(monkeypatch)

        assert module.COTURN_SHARED_SECRET == ""
        assert module.COTURN_REGION2_SHARED_SECRET == ""


class TestTurnEnvironmentDocumentation:
    def test_env_example_lists_variable_names_without_secret_values(self):
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        entries = [line.strip().lstrip("#").strip() for line in lines]

        for name in TURN_ENV_VAR_NAMES:
            assert any(entry.startswith(f"{name}=") for entry in entries), (
                f"{name} is missing from .env.example"
            )

        for name in ("COTURN_SHARED_SECRET", "COTURN_REGION2_SHARED_SECRET"):
            for entry in entries:
                if entry.startswith(f"{name}="):
                    assert entry == f"{name}=", (
                        f".env.example must not carry a value for {name}"
                    )

    def test_production_settings_file_contains_no_literal_credential(self):
        source = PRODUCTION_SETTINGS.read_text(encoding="utf-8")

        assert 'os.environ.get("COTURN_SHARED_SECRET", "")' in source
        assert FAKE_SHARED_SECRET not in source


class TestStagingTurnBehaviourPreserved:
    @override_settings(
        COTURN_REGION1_DOMAIN="turn-r1.staging.testdomain.com",
        COTURN_PORT=3478,
        COTURN_TLS_PORT=5349,
        COTURN_SHARED_SECRET=FAKE_SHARED_SECRET,
        COTURN_REGION2_DOMAIN=None,
    )
    def test_staging_keeps_explicit_udp_transport_param(self):
        """T-35/T-36 default stays `?transport=udp` when the flag is unset."""
        service = TurnCredentialService()
        result = service.generate_credentials(participant_id="staging-participant")

        assert result["ice_servers"][1]["urls"] == [
            "turn:turn-r1.staging.testdomain.com:3478?transport=udp",
            "turn:turn-r1.staging.testdomain.com:3478?transport=tcp",
        ]
