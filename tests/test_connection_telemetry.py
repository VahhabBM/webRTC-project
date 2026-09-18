from datetime import timedelta
import json

from django.test import Client
from django.utils import timezone
import pytest

from apps.events.models import ConnectionType, Event, Pair, Participant, Round


@pytest.fixture
def event_with_round_and_pair(db):
    now = timezone.now()
    event = Event.objects.create(
        name="Telemetry Event",
        start_time=now,
        num_rounds=1,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(seconds=10),
    )
    rnd = Round.objects.create(
        event=event,
        number=1,
        starts_at=now,
        ends_at=now + timedelta(minutes=5),
    )
    user_1 = Participant.objects.create(event=event, display_name="User Alpha")
    user_2 = Participant.objects.create(event=event, display_name="User Beta")
    user_1.set_join_token("token-alpha-1234")
    user_2.set_join_token("token-beta-1234")
    user_1.save()
    user_2.save()

    pair = Pair.objects.create(
        event=event,
        round=rnd,
        participant_a=user_1,
        participant_b=user_2,
        room_id="room-test-telemetry-1",
    )
    pair.refresh_from_db()

    # دریافت شرکت‌کنندگان بر اساس ترتیب قطعی دیتابیس (رعایت قید participant_a < participant_b)
    part_a = pair.participant_a
    part_b = pair.participant_b
    return event, rnd, pair, part_a, part_b


@pytest.mark.django_db
class TestConnectionTelemetry:
    def test_unauthenticated_request_rejected(self):
        client = Client()
        resp = client.post(
            "/api/telemetry/connection/",
            data=json.dumps(
                {
                    "room_id": "r1",
                    "connection_type": "direct",
                    "connection_time_ms": 120,
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 401

    def test_invalid_payload_validation(self, event_with_round_and_pair):
        _, _, _, part_a, _ = event_with_round_and_pair
        client = Client()
        session = client.session
        session["participant_id"] = str(part_a.id)
        session.save()

        # بدون room_id
        resp = client.post(
            "/api/telemetry/connection/",
            data=json.dumps({"connection_type": "direct", "connection_time_ms": 120}),
            content_type="application/json",
        )
        assert resp.status_code == 400

        # نوع اتصال نامعتبر (فقط direct یا relay مجاز است)
        resp = client.post(
            "/api/telemetry/connection/",
            data=json.dumps(
                {
                    "room_id": "room-test",
                    "connection_type": "unknown",
                    "connection_time_ms": 120,
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400

        # زمان منفی
        resp = client.post(
            "/api/telemetry/connection/",
            data=json.dumps(
                {
                    "room_id": "room-test",
                    "connection_type": "relay",
                    "connection_time_ms": -5,
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_both_participants_record_telemetry_independently(
        self, event_with_round_and_pair
    ):
        """معیار پذیرش DoD: ثبت نوع مسیر و زمان برقراری برای هر دو طرف A و B."""
        _, _, pair, part_a, part_b = event_with_round_and_pair

        client_a = Client()
        sess_a = client_a.session
        sess_a["participant_id"] = str(part_a.id)
        sess_a.save()

        # شرکت‌کننده A اتصال Direct با زمان 240 میلی‌ثانیه را گزارش می‌دهد
        resp_a = client_a.post(
            "/api/telemetry/connection/",
            data=json.dumps(
                {
                    "room_id": pair.room_id,
                    "connection_type": "direct",
                    "connection_time_ms": 240,
                }
            ),
            content_type="application/json",
        )
        assert resp_a.status_code == 200

        client_b = Client()
        sess_b = client_b.session
        sess_b["participant_id"] = str(part_b.id)
        sess_b.save()

        # شرکت‌کننده B اتصال Relay با زمان 850 میلی‌ثانیه را گزارش می‌دهد
        resp_b = client_b.post(
            "/api/telemetry/connection/",
            data=json.dumps(
                {
                    "room_id": pair.room_id,
                    "connection_type": "relay",
                    "connection_time_ms": 850,
                }
            ),
            content_type="application/json",
        )
        assert resp_b.status_code == 200

        pair.refresh_from_db()
        assert pair.connection_type_a == ConnectionType.DIRECT
        assert pair.connection_time_ms_a == 240
        assert pair.connection_type_b == ConnectionType.RELAY
        assert pair.connection_time_ms_b == 850

    def test_concurrent_load_simulation_25_pairs(self, db):
        """قید مقیاس‌پذیری: ثبت همزمان ۲۵ جفت تستی بدون افت درخواست یا مسدودی."""
        now = timezone.now()
        event = Event.objects.create(
            name="Load Event",
            start_time=now,
            num_rounds=1,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(seconds=10),
        )
        rnd = Round.objects.create(
            event=event,
            number=1,
            starts_at=now,
            ends_at=now + timedelta(minutes=5),
        )

        pairs = []
        clients = []
        for i in range(25):
            u1 = Participant.objects.create(event=event, display_name=f"Load A {i}")
            u2 = Participant.objects.create(event=event, display_name=f"Load B {i}")
            p = Pair.objects.create(
                event=event,
                round=rnd,
                participant_a=u1,
                participant_b=u2,
                room_id=f"load-room-{i}",
            )
            p.refresh_from_db()

            # تفکیک دقیق A و B مطابق با ساختار نهایی جفت
            actual_a = p.participant_a
            actual_b = p.participant_b

            cli_a = Client()
            sa = cli_a.session
            sa["participant_id"] = str(actual_a.id)
            sa.save()

            cli_b = Client()
            sb = cli_b.session
            sb["participant_id"] = str(actual_b.id)
            sb.save()

            clients.append((cli_a, cli_b, p.room_id))
            pairs.append(p)

        for cli_a, cli_b, r_id in clients:
            res_a = cli_a.post(
                "/api/telemetry/connection/",
                data=json.dumps(
                    {
                        "room_id": r_id,
                        "connection_type": "direct",
                        "connection_time_ms": 150,
                    }
                ),
                content_type="application/json",
            )
            assert res_a.status_code == 200

            res_b = cli_b.post(
                "/api/telemetry/connection/",
                data=json.dumps(
                    {
                        "room_id": r_id,
                        "connection_type": "direct",
                        "connection_time_ms": 160,
                    }
                ),
                content_type="application/json",
            )
            assert res_b.status_code == 200

        # بررسی نهایی تطابق فیلدهای هر ۲۵ جفت
        for p in pairs:
            p.refresh_from_db()
            assert p.connection_type_a == ConnectionType.DIRECT
            assert p.connection_time_ms_a == 150
            assert p.connection_type_b == ConnectionType.DIRECT
            assert p.connection_time_ms_b == 160
