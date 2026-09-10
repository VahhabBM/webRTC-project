import base64
import hashlib
import hmac
import time
from datetime import timedelta

import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.events.turn import generate_ice_servers


@pytest.mark.django_db
class TestTurnCredentials:
    def test_stun_fallback_when_turn_secret_missing(self):
        """تست Fallback به STUN در صورت نبود TURN_SHARED_SECRET"""
        res = generate_ice_servers(
            participant_id="user-123", turn_secret="", turn_urls=[]
        )

        assert res["fallback_only"] is True
        assert res["expires_at"] is None
        assert len(res["ice_servers"]) == 1
        assert "stun:" in res["ice_servers"][0]["urls"][0]

    def test_short_lived_credential_generation_rfc5766(self):
        """تست تولید کریدنشال موقت استاندارد با HMAC-SHA1"""
        secret = "super-secret-turn-key"
        turn_urls = ["turn:relay.example.com:3478?transport=udp"]
        participant_id = "test-participant-456"
        ttl = 1800

        before_time = int(time.time())
        res = generate_ice_servers(
            participant_id=participant_id,
            ttl=ttl,
            turn_secret=secret,
            turn_urls=turn_urls,
        )

        assert res["fallback_only"] is False
        assert res["ttl"] == ttl
        assert res["expires_at"] >= before_time + ttl

        # بررسی فرمت W3C RTCIceServer
        assert len(res["ice_servers"]) == 2
        turn_entry = res["ice_servers"][1]
        assert turn_entry["urls"] == turn_urls

        # بررسی صحت نام کاربری (expiry:user_id)
        username = turn_entry["username"]
        assert username == f"{res['expires_at']}:{participant_id}"

        # بررسی صحت امضای پسورد
        expected_digest = hmac.new(
            secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1
        ).digest()
        expected_credential = base64.b64encode(expected_digest).decode("utf-8")
        assert turn_entry["credential"] == expected_credential

    def test_endpoint_unauthorized_access(self, client):
        """عدم دسترسی به اندپوینت بدون احراز هویت"""
        url = reverse("ice_servers")
        response = client.get(url)
        assert response.status_code == 401
        assert response.json()["code"] == "UNAUTHORIZED"

    @override_settings(
        TURN_SHARED_SECRET="mock-secret",
        TURN_URLS=["turn:turn.example.com:3478"],
    )
    def test_endpoint_authenticated_success(self, client):
        """تست دریافت موفق کانفیگ با سشن معتبر کاربر"""
        from apps.events.models import Event, Participant

        now = timezone.now()
        event = Event.objects.create(
            name="TURN Test Event",
            num_rounds=1,
            round_duration=timedelta(minutes=10),
            break_duration=timedelta(minutes=2),
            start_time=now,
        )
        p = Participant.objects.create(
            event=event, display_name="Ali", join_token_hash="!"
        )

        session = client.session
        session["participant_id"] = str(p.id)
        session.save()

        url = reverse("ice_servers")
        response = client.get(url, {"ttl": "7200"})

        assert response.status_code == 200
        data = response.json()
        assert data["ttl"] == 7200
        assert len(data["ice_servers"]) == 2
        assert str(p.id) in data["ice_servers"][1]["username"]
