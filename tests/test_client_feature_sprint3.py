import re
from datetime import timedelta

from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from apps.events.countries import (
    ALLOWED_REGISTRATION_COUNTRIES,
)
from apps.events.live_dispatch import WAIT_MESSAGES
from apps.events.live_matching import _cap, reconcile_live_round
from apps.events.models import (
    Event,
    EventStatus,
    Participant,
    RegistrationStatus,
    Round,
    RoundStatus,
)
from apps.events.orchestrator import connection_channel_key
from apps.registration.forms import RegistrationError, validate_registration_data


class ClientFeatureSprint3Tests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.now = timezone.now()
        self.event = Event.objects.create(
            name="Exclusive European Speed Dating Night",
            status=EventStatus.RUNNING,
            start_time=self.now - timedelta(minutes=5),
            num_rounds=5,
            round_duration=timedelta(minutes=7),
            break_duration=timedelta(minutes=2),
            male_date_limit=4,
            female_date_limit=8,
            max_dates_per_participant=5,
            live_matching_enabled=True,
        )

    # -------------------------------------------------------------
    # Task 1: Event Name in Registration Header
    # -------------------------------------------------------------
    def test_registration_heading_shows_event_name(self):
        """Registration page renders the event's configured name in the header."""
        response = self.client.get(f"/registration/{self.event.id}/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertIn("<h1>Exclusive European Speed Dating Night</h1>", html)
        self.assertIn(
            "<title>Exclusive European Speed Dating Night - Registration</title>", html
        )
        self.assertNotIn("<h1>SPEED DATING REGISTRATION</h1>", html)

    # -------------------------------------------------------------
    # Task 2: Configurable Max Dates per Participant
    # -------------------------------------------------------------
    def test_max_dates_per_participant_event_setting(self):
        """_cap respects max_dates_per_participant when set, overriding gender limits."""
        male = Participant.objects.create(
            event=self.event,
            display_name="John",
            gender="Male",
            email="john@example.com",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        female = Participant.objects.create(
            event=self.event,
            display_name="Jane",
            gender="Female",
            email="jane@example.com",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        self.assertEqual(_cap(self.event, male), 5)
        self.assertEqual(_cap(self.event, female), 5)

        # When max_dates_per_participant is None, fall back to individual gender limits
        self.event.max_dates_per_participant = None
        self.event.save()
        self.assertEqual(_cap(self.event, male), 4)
        self.assertEqual(_cap(self.event, female), 8)

    def test_limit_reached_notification_message(self):
        """Notification for limit_reached contains warm farewell and informs no more dates."""
        msg = WAIT_MESSAGES.get("limit_reached", "")
        self.assertIn("You have completed your dates for tonight", msg)
        self.assertIn("You will not receive any more dates", msg)
        self.assertIn("We hope you have a wonderful night!", msg)

    # -------------------------------------------------------------
    # Task 3: Cross-Country Matching Fallback
    # -------------------------------------------------------------
    def test_cross_country_pairing_fallback(self):
        """Two participants from different countries (Ireland & Germany) are paired successfully."""
        round_1 = Round.objects.create(
            event=self.event,
            number=1,
            status=RoundStatus.ACTIVE,
            starts_at=self.now - timedelta(minutes=1),
            ends_at=self.now + timedelta(minutes=6),
        )

        p_ireland = Participant.objects.create(
            event=self.event,
            display_name="Saeid",
            gender="Male",
            want_to_date="Female",
            country="IE",
            city="Athlone",
            email="saeid@example.com",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        p_germany = Participant.objects.create(
            event=self.event,
            display_name="Serena",
            gender="Female",
            want_to_date="Male",
            country="DE",
            city="Berlin",
            email="serena@example.com",
            registration_status=RegistrationStatus.CONFIRMED,
        )

        # Mark both participants ready at self.now
        for p in (p_ireland, p_germany):
            chan = f"chan-{p.pk}"
            cache.set(connection_channel_key(p.pk), chan, 300)
            cache.set(
                f"ws:presence:{p.pk}",
                {"channel": chan, "ready": True, "touched": self.now.timestamp()},
                45,
            )

        alloc = reconcile_live_round(self.event, round_1, now=self.now)
        self.assertEqual(len(alloc["new_pairs"]), 1)
        created_pair = alloc["new_pairs"][0]
        pair_pids = {
            str(created_pair.participant_a_id),
            str(created_pair.participant_b_id),
        }
        self.assertEqual(pair_pids, {str(p_ireland.pk), str(p_germany.pk)})

    # -------------------------------------------------------------
    # Task 4: Complete Removal of Persian Text
    # -------------------------------------------------------------
    def test_zero_persian_text_in_templates_and_js(self):
        """Verify video_room.html and call_room.js contain no Persian Unicode characters."""
        persian_pattern = re.compile(r"[\u0600-\u06FF]")
        with open(
            "apps/events/templates/events/video_room.html", encoding="utf-8"
        ) as f:
            content = f.read()
            self.assertFalse(
                persian_pattern.search(content),
                "Persian characters found in video_room.html!",
            )
            self.assertIn("Leave Date", content)
            self.assertNotIn("خروج", content)

        with open("apps/events/static/events/js/call_room.js", encoding="utf-8") as f:
            content_js = f.read()
            self.assertFalse(
                persian_pattern.search(content_js),
                "Persian characters found in call_room.js!",
            )

    # -------------------------------------------------------------
    # Task 5: 5 Allowed Countries Only with 3-Month Notice
    # -------------------------------------------------------------
    def test_allowed_countries_registration_whitelist(self):
        """The 5 whitelisted countries (GB, IE, ES, DE, NL) pass registration validation."""
        for code in ALLOWED_REGISTRATION_COUNTRIES:
            data = {
                "display_name": f"User {code}",
                "age": 28,
                "country": code,
                "city": "Capital",
                "gender": "Male",
                "want_to_date": "Female",
                "email": f"user_{code.lower()}@example.com",
                "email_confirmation": f"user_{code.lower()}@example.com",
                "tag_ids": [],
                "consents": {
                    "age_confirmation": True,
                    "sensitive_data_processing": True,
                    "mutual_contact_sharing": True,
                    "terms_and_privacy": True,
                },
            }
            validated = validate_registration_data(data)
            self.assertEqual(validated["country"], code)

    def test_disallowed_country_rejection_message(self):
        """Any country outside the 5 allowed returns the exact 3-month notice message."""
        disallowed = ["US", "FR", "NZ", "IT", "IR", "CA", "AU"]
        expected_msg = "This country is not currently on our service list. Please check back within the next 3 months."

        for code in disallowed:
            data = {
                "display_name": f"User {code}",
                "age": 28,
                "country": code,
                "city": "City",
                "gender": "Male",
                "want_to_date": "Female",
                "email": f"user_{code.lower()}@example.com",
                "email_confirmation": f"user_{code.lower()}@example.com",
                "tag_ids": [],
                "consents": {
                    "age_confirmation": True,
                    "sensitive_data_processing": True,
                    "mutual_contact_sharing": True,
                    "terms_and_privacy": True,
                },
            }
            with self.assertRaises(RegistrationError) as ctx:
                validate_registration_data(data)
            self.assertEqual(ctx.exception.message, expected_msg)
            self.assertEqual(ctx.exception.code, "country_not_serviced")
