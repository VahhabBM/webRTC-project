import json
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.events.email_notifications import send_event_matches_summary_emails
from apps.events.matching import MatchParticipant, _accepts
from apps.events.models import (
    Event,
    EventStatus,
    Pair,
    PairInterest,
    Participant,
    ParticipantIncidentNote,
    RegistrationStatus,
)
from apps.registration.capacity import count_reserved_seats, has_registration_capacity
from apps.registration.forms import validate_registration_data
from apps.registration.legal_documents import REQUIRED_CONSENTS


@pytest.fixture
def sample_event(db):
    return Event.objects.create(
        name="Sprint Speed Dating",
        start_time=timezone.now() + timedelta(days=2),
        num_rounds=5,
        status=EventStatus.SCHEDULED,
        male_registration_cap=2,
        female_registration_cap=1,
    )


@pytest.mark.django_db
class TestGenderRegistrationCaps:
    def test_gender_caps_enforced(self, sample_event):
        # Initial check
        assert has_registration_capacity(sample_event, gender="Male") is True
        assert has_registration_capacity(sample_event, gender="Female") is True

        # Register 1st male
        Participant.objects.create(
            event=sample_event,
            display_name="Male 1",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        assert has_registration_capacity(sample_event, gender="Male") is True

        # Register 2nd male (hits cap of 2)
        Participant.objects.create(
            event=sample_event,
            display_name="Male 2",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        assert count_reserved_seats(sample_event, gender="Male") == 2
        assert has_registration_capacity(sample_event, gender="Male") is False

        # Female cap is 1
        assert has_registration_capacity(sample_event, gender="Female") is True
        Participant.objects.create(
            event=sample_event,
            display_name="Female 1",
            gender="Female",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        assert has_registration_capacity(sample_event, gender="Female") is False


class TestWantToDateAndAgePreferences:
    def test_multi_select_want_to_date_validation(self):
        data = {
            "display_name": "Alex",
            "age": 25,
            "country": "GB",
            "city": "London",
            "gender": "Male",
            "want_to_date": ["Male", "Female"],
            "age_pref_min": 22,
            "age_pref_max": 35,
            "email": "alex@example.com",
            "email_confirmation": "alex@example.com",
            "consents": {c: True for c in REQUIRED_CONSENTS},
        }
        res = validate_registration_data(None, data)
        assert res["want_to_date"] == "Any"
        assert res["age_pref_min"] == 22
        assert res["age_pref_max"] == 35

    def test_single_or_specific_multi_date_preference(self):
        data = {
            "display_name": "Sam",
            "age": 30,
            "country": "GB",
            "city": "London",
            "gender": "Female",
            "want_to_date": ["Male"],
            "age_pref_min": "25",
            "age_pref_max": "40",
            "email": "sam@example.com",
            "email_confirmation": "sam@example.com",
            "consents": {c: True for c in REQUIRED_CONSENTS},
        }
        res = validate_registration_data(None, data)
        assert res["want_to_date"] == "Male"
        assert res["age_pref_min"] == 25
        assert res["age_pref_max"] == 40

    def test_matching_accepts_multi_value_and_single(self):
        seeker_any = MatchParticipant(
            pid="p1", profile=None, gender="Male", want_to_date="Any"
        )
        seeker_mf = MatchParticipant(
            pid="p2", profile=None, gender="Male", want_to_date="Female,Male"
        )
        seeker_f = MatchParticipant(
            pid="p3", profile=None, gender="Male", want_to_date="Female"
        )

        target_f = MatchParticipant(pid="p4", profile=None, gender="Female")
        target_m = MatchParticipant(pid="p5", profile=None, gender="Male")

        assert _accepts(seeker_any, target_f) is True
        assert _accepts(seeker_any, target_m) is True

        assert _accepts(seeker_mf, target_f) is True
        assert _accepts(seeker_mf, target_m) is True

        assert _accepts(seeker_f, target_f) is True
        assert _accepts(seeker_f, target_m) is False


@pytest.mark.django_db
class TestMatchesSummaryEmail:
    def test_send_consolidated_matches_email(self, sample_event):
        p1 = Participant.objects.create(
            event=sample_event,
            display_name="Alice",
            email="alice@example.com",
            whatsapp="+44 7111111111",
            gender="Female",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        p2 = Participant.objects.create(
            event=sample_event,
            display_name="Bob",
            email="bob@example.com",
            whatsapp="+44 7222222222",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        p3 = Participant.objects.create(
            event=sample_event,
            display_name="Charlie",
            email="charlie@example.com",
            whatsapp="+44 7333333333",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
        )

        from apps.events.models import Round

        round1 = Round.objects.create(
            event=sample_event,
            number=1,
            starts_at=timezone.now() - timedelta(minutes=25),
            ends_at=timezone.now() - timedelta(minutes=15),
        )
        round2 = Round.objects.create(
            event=sample_event,
            number=2,
            starts_at=timezone.now() - timedelta(minutes=14),
            ends_at=timezone.now() - timedelta(minutes=5),
        )

        # Mutual pair between Alice and Bob in round 1
        pair1 = Pair.objects.create(
            event=sample_event,
            round=round1,
            participant_a=p1,
            participant_b=p2,
            room_id="room-1",
        )
        PairInterest.objects.create(
            pair=pair1,
            participant_a_interested=True,
            participant_b_interested=True,
        )

        # Non-mutual pair between Alice and Charlie in round 2 (Alice said yes, Charlie said no)
        pair2 = Pair.objects.create(
            event=sample_event,
            round=round2,
            participant_a=p1,
            participant_b=p3,
            room_id="room-2",
        )
        PairInterest.objects.create(
            pair=pair2,
            participant_a_interested=True,
            participant_b_interested=False,
        )

        mail.outbox.clear()
        sent_count = send_event_matches_summary_emails(sample_event.pk)
        assert sent_count == 3  # All 3 participants got an email

        # Alice's email should list Bob and Bob's WhatsApp
        alice_email = next(m for m in mail.outbox if "alice@example.com" in m.to)
        assert "Bob" in alice_email.body
        assert "+44 7222222222" in alice_email.body
        assert "Charlie" not in alice_email.body

        # Bob's email should list Alice and Alice's WhatsApp
        bob_email = next(m for m in mail.outbox if "bob@example.com" in m.to)
        assert "Alice" in bob_email.body
        assert "+44 7111111111" in bob_email.body

        # Charlie's email has no matches note
        charlie_email = next(m for m in mail.outbox if "charlie@example.com" in m.to)
        assert "did not have any mutual matches" in charlie_email.body

        # Resend without force should send 0 because matches_email_sent_at is set
        p1.refresh_from_db()
        assert p1.matches_email_sent_at is not None
        assert send_event_matches_summary_emails(sample_event.pk) == 0


@pytest.mark.django_db
class TestIncidentReporting:
    def test_report_incident_endpoint(self, sample_event, client):
        p1 = Participant.objects.create(
            event=sample_event,
            display_name="Reporter",
            email="reporter@example.com",
            gender="Female",
        )
        p2 = Participant.objects.create(
            event=sample_event,
            display_name="Offender",
            email="offender@example.com",
            gender="Male",
        )
        from apps.events.models import Round

        round_obj = Round.objects.create(
            event=sample_event,
            number=1,
            starts_at=timezone.now() - timedelta(minutes=15),
            ends_at=timezone.now() - timedelta(minutes=5),
        )
        Pair.objects.create(
            event=sample_event,
            round=round_obj,
            participant_a=p1,
            participant_b=p2,
            room_id="room-incident-test",
        )

        session = client.session
        session["participant_id"] = str(p1.pk)
        session.save()

        response = client.post(
            "/api/report-incident/",
            data=json.dumps(
                {
                    "report": "Inappropriate comment during date",
                    "room_id": "room-incident-test",
                    "round_number": 1,
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "reported": True}

        # Check DB
        note = ParticipantIncidentNote.objects.filter(event=sample_event).first()
        assert note is not None
        assert note.participant == p2  # Attributed to offender
        assert "Inappropriate comment during date" in note.note
        assert "Reporter" in note.note
