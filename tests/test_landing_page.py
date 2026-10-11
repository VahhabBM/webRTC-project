"""
Tests for the Meetakk landing page (GET /).

Acceptance criteria verified here:
1. GET / returns HTTP 200 and renders the landing page.
2. The response contains Meetakk branding and the expected primary CTA.
3. The CTA points to the real /join/<token>/ participant-entry flow.
4. Existing participant-join, clock-sync, and video-room routes remain intact.
5. The URL reversal for 'landing-page' resolves to '/'.
6. Existing named URL reversals are not broken.
"""

import pytest
from django.test import Client
from django.urls import reverse

# ---------------------------------------------------------------------------
# Landing page — basic rendering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_landing_page_returns_200():
    """GET / must return HTTP 200."""
    response = Client().get("/")
    assert response.status_code == 200


@pytest.mark.django_db
def test_landing_page_content_type_is_html():
    response = Client().get("/")
    assert "text/html" in response["Content-Type"]


@pytest.mark.django_db
def test_landing_page_contains_brand_name():
    """The response body must include the Meetakk brand name."""
    response = Client().get("/")
    assert b"Meetakk" in response.content


@pytest.mark.django_db
def test_landing_page_contains_headline():
    """The hero headline must be present."""
    response = Client().get("/")
    assert b"Meaningful connections" in response.content


@pytest.mark.django_db
def test_landing_page_contains_join_cta():
    """The primary CTA linking to the join flow must appear on the page."""
    response = Client().get("/")
    # The page must reference the /join/ path so users can enter their token
    assert b"/join/" in response.content


@pytest.mark.django_db
def test_landing_page_contains_how_it_works_section():
    response = Client().get("/")
    assert b"how-it-works" in response.content


@pytest.mark.django_db
def test_landing_page_contains_features_section():
    response = Client().get("/")
    assert b"features" in response.content


@pytest.mark.django_db
def test_landing_page_references_static_css():
    """The template must load the landing CSS via Django's static tag."""
    response = Client().get("/")
    assert b"landing.css" in response.content


# ---------------------------------------------------------------------------
# URL reversals — landing page
# ---------------------------------------------------------------------------


def test_landing_page_url_reversal():
    """The named URL 'landing-page' must resolve to the root path."""
    assert reverse("landing-page") == "/"


# ---------------------------------------------------------------------------
# Existing routes must remain accessible / URL reversals must not be broken
# ---------------------------------------------------------------------------


def test_participant_join_url_reversal():
    """The 'participant-join' URL pattern must still resolve correctly."""
    url = reverse("participant-join", kwargs={"token": "testtoken"})
    assert url == "/join/testtoken/"


def test_video_room_url_reversal():
    url = reverse("video-room")
    assert url == "/room/"


def test_clock_sync_url_reversal():
    url = reverse("clock-sync")
    assert url == "/clock-sync/"


def test_participant_me_url_reversal():
    url = reverse("participant-me")
    assert url == "/participant/me/"


def test_health_url_reversal():
    url = reverse("health-check")
    assert url == "/health/"


def test_registration_register_url_reversal():
    """Registration route must still resolve with an event_id."""
    import uuid

    event_id = uuid.uuid4()
    url = reverse("registration:register", kwargs={"event_id": event_id})
    assert url == f"/registration/events/{event_id}/register/"


# ---------------------------------------------------------------------------
# Existing routes — HTTP-level smoke tests (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_join_route_exists_and_rejects_invalid_token():
    """The /join/<token>/ endpoint must still exist (400 for invalid token)."""
    response = Client().get("/join/no-such-token/")
    assert response.status_code == 400


@pytest.mark.django_db
def test_participant_me_requires_auth():
    """The /participant/me/ endpoint must require authentication."""
    response = Client().get("/participant/me/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_video_room_requires_auth():
    """The /room/ endpoint must require authentication."""
    response = Client().get("/room/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_clock_sync_requires_auth():
    """The /clock-sync/ endpoint must require authentication."""
    response = Client().get("/clock-sync/")
    assert response.status_code == 401


@pytest.mark.django_db
def test_health_endpoint_still_works():
    """The /health/ endpoint must still return 200 after the landing change."""
    response = Client().get("/health/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
