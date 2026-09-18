import base64
import hashlib
import hmac
import time

import pytest
from django.test import override_settings

from apps.events.turn import TurnCredentialService

FAKE_SECRET = "staging_super_secret_test_key_12345"
FAKE_DOMAIN = "turn-r1.staging.testdomain.com"


@pytest.mark.django_db
class TestTurnStagingCredentials:
    @pytest.fixture(autouse=True)
    def setup_env(self):
        with override_settings(
            COTURN_REGION1_DOMAIN=FAKE_DOMAIN,
            COTURN_PORT=3478,
            COTURN_TLS_PORT=5349,
            COTURN_SHARED_SECRET=FAKE_SECRET,
            COTURN_CREDENTIAL_TTL_SECONDS=1200,
        ):
            yield

    def test_credential_generation_structure(self):
        """اعتبارسنجی ساختار خروجی طبق استاندارد W3C RTCIceServer"""
        service = TurnCredentialService()
        result = service.generate_credentials(participant_id="user-xyz-99")

        assert result["region"] == "region-1-staging"
        assert result["ttl"] == 1200
        assert "ice_servers" in result

        ice_servers = result["ice_servers"]
        assert len(ice_servers) == 3

        # ۱. سرور STUN
        assert ice_servers[0]["urls"] == [f"stun:{FAKE_DOMAIN}:3478"]

        # ۲. سرور TURN معمولی
        turn_server = ice_servers[1]
        assert f"turn:{FAKE_DOMAIN}:3478?transport=udp" in turn_server["urls"]
        assert f"turn:{FAKE_DOMAIN}:3478?transport=tcp" in turn_server["urls"]
        assert "username" in turn_server
        assert "credential" in turn_server

        # ۳. سرور TURNS امن (TLS)
        turns_server = ice_servers[2]
        assert (
            f"turns:{FAKE_DOMAIN}:5349?transport=tcp" in turns_server["urls"]
        )

    def test_shared_secret_never_leaks(self):
        """اطمینان از اینکه رمز مشترک هرگز در خروجی به کلاینت نشت نمی‌کند"""
        service = TurnCredentialService()
        result = service.generate_credentials(participant_id="secure-user")

        result_str = str(result)
        assert FAKE_SECRET not in result_str

    def test_hmac_sha1_signature_verifiable(self):
        """اعتبارسنجی ریاضی امضای رمز موقت با استفاده از سکرت مشترک"""
        service = TurnCredentialService()
        result = service.generate_credentials(participant_id="partner-42")

        username = result["ice_servers"][1]["username"]
        credential = result["ice_servers"][1]["credential"]

        # بررسی ساختار یوزرنیم (<timestamp>:<participant_id>)
        timestamp_str, uid = username.split(":")
        assert uid == "partner-42"
        assert int(timestamp_str) > int(time.time())

        # بازتولید امضا جهت تطابق با Coturn
        expected_digest = hmac.new(
            FAKE_SECRET.encode("utf-8"),
            username.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        expected_password = base64.b64encode(expected_digest).decode("utf-8")

        assert credential == expected_password
