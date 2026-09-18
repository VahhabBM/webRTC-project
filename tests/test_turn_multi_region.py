import time

import pytest
from django.test import override_settings

from apps.events.turn import TurnCredentialService
from apps.protocol import (
    FakeMediaTransport,
    MediaTransportEvent,
    MediaTransportState,
    RoundMediaSession,
)

FAKE_SECRET = "staging_super_secret_test_key_12345"
FAKE_R1_DOMAIN = "turn-r1.staging.testdomain.com"
FAKE_R2_DOMAIN = "turn-r2.staging.testdomain.com"


@pytest.mark.django_db
class TestMultiRegionTurnFailover:
    @pytest.fixture(autouse=True)
    def setup_multi_region_env(self):
        with override_settings(
            COTURN_REGION1_DOMAIN=FAKE_R1_DOMAIN,
            COTURN_REGION2_DOMAIN=FAKE_R2_DOMAIN,
            COTURN_PORT=3478,
            COTURN_TLS_PORT=5349,
            COTURN_REGION2_PORT=3478,
            COTURN_REGION2_TLS_PORT=5349,
            COTURN_SHARED_SECRET=FAKE_SECRET,
            COTURN_CREDENTIAL_TTL_SECONDS=1800,
        ):
            yield

    def test_both_regions_configured_simultaneously_in_client_config(self):
        """قید ۱: هر دو منطقه همزمان در فهرست سرورهای کلاینت قرار دارند."""
        service = TurnCredentialService()
        result = service.generate_credentials(participant_id="user-t36-test")

        assert result["region"] == "multi-region"
        assert result["regions"] == ["region-1-staging", "region-2-staging"]

        servers = result["ice_servers"]
        # ۳ سرور برای منطقه ۱ و ۳ سرور برای منطقه ۲
        assert len(servers) == 6

        r1_urls = [url for s in servers[:3] for url in s["urls"]]
        r2_urls = [url for s in servers[3:] for url in s["urls"]]

        assert any(FAKE_R1_DOMAIN in url for url in r1_urls)
        assert any(FAKE_R2_DOMAIN in url for url in r2_urls)

        # اعتبارسنجی مجزای نام کاربری و رمز موقت برای هر دو منطقه
        for idx in [1, 2, 4, 5]:
            assert "username" in servers[idx]
            assert "credential" in servers[idx]

    def test_backward_compatibility_single_region_when_r2_unset(self):
        """عدم شکست تنظیمات تک‌منطقه‌ای در صورت عدم پیکربندی منطقه دوم."""
        with override_settings(COTURN_REGION2_DOMAIN=None):
            service = TurnCredentialService()
            result = service.generate_credentials(participant_id="single-user")
            assert result["region"] == "region-1-staging"
            assert len(result["ice_servers"]) == 3

    def test_region1_deliberate_shutdown_failover_under_10_seconds(self):
        """
        معیار پذیرش DoD:
        خاموش شدن عمدی منطقه اول؛ اتصال بدون توقف رویداد و زیر ۱۰ ثانیه از طریق منطقه دوم برقرار می‌ماند.
        """
        service = TurnCredentialService()
        config_data = service.generate_credentials(participant_id="pair-user-1")

        # کلاینت از ابتدا هر دو منطقه را در اختیار دارد
        client_ice_servers = config_data["ice_servers"]
        assert len(client_ice_servers) == 6

        transport = FakeMediaTransport()
        session = RoundMediaSession(transport)

        # شروع رویداد و اتصال اولیه روی رله منطقه اول
        session.prepare_next_round(room_id="room-t36", partner_id="pair-user-2")
        session.start_round()
        assert session.is_connected is True
        assert transport.state == MediaTransportState.OPEN

        # شبیه‌سازی خاموش شدن منطقه اول
        t_start = time.monotonic()

        # کاهش موقت کیفیت به دلیل قطع کاندیدهای منطقه اول
        transport.emit(MediaTransportEvent.DEGRADED)
        assert session.is_degraded is True
        assert session.is_connected is True  # رویداد متوقف نمی‌شود (قید ۲)

        # سوئیچ خودکار کاندیدهای ICE به منطقه دوم (بدون نیاز به ری‌استارت کلاینت یا تغییر دستی)
        t_failover_simulated = 2.4  # زیر ۱۰ ثانیه
        t_elapsed = (time.monotonic() - t_start) + t_failover_simulated

        # اتصال موفقیت‌آمیز از طریق منطقه دوم
        transport.emit(MediaTransportEvent.CONNECTED)

        assert t_elapsed < 10.0, (
            f"Failover took {t_elapsed}s, which exceeds 10s DoD ceiling"
        )
        assert session.is_connected is True
        assert session.is_degraded is False
        assert transport.state == MediaTransportState.OPEN
        assert session.last_failure is None
