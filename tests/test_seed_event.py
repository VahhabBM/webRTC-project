import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.events.management.commands.seed_event import SAMPLE_TAGS
from apps.events.models import Event, Participant, ParticipantTag, Tag


@pytest.mark.django_db(transaction=True)
def test_seed_event_creates_complete_dataset():
    call_command("seed_event", participants=10, seed=7)
    event = Event.objects.get()
    assert Participant.objects.filter(event=event).count() == 10
    assert Tag.objects.count() == len(SAMPLE_TAGS)
    assert ParticipantTag.objects.filter(participant__event=event).exists()
    assert event.rounds.count() == event.num_rounds
    assert (
        len(
            set(
                ParticipantTag.objects.filter(participant__event=event).values_list(
                    "tag_id", flat=True
                )
            )
        )
        > 1
    )


@pytest.mark.django_db(transaction=True)
def test_seed_event_repeat_reuses_tags_and_preserves_existing_data():
    call_command("seed_event", participants=3, seed=1)
    first_event = Event.objects.get()
    call_command("seed_event", participants=4, seed=2)
    assert Event.objects.count() == 2
    assert Participant.objects.filter(event=first_event).count() == 3
    assert Tag.objects.count() == len(SAMPLE_TAGS)


@pytest.mark.django_db(transaction=True)
def test_seed_event_tokens_are_hashed():
    call_command("seed_event", participants=1, seed=1)
    participant = Participant.objects.get()
    assert participant.join_token_hash != "seed"
    assert participant.join_token_hash.startswith(("pbkdf2_", "argon2", "bcrypt"))


@pytest.mark.django_db(transaction=True)
def test_seed_event_rejects_production_without_confirmation(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    with pytest.raises(CommandError, match="confirm-production"):
        call_command("seed_event", participants=1)
    assert not Event.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_seed_event_allows_confirmed_production(monkeypatch):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.production")
    call_command("seed_event", participants=1, confirm_production=True)
    assert Event.objects.count() == 1


@pytest.mark.django_db
def test_invalid_count_fails_before_writes():
    with pytest.raises(CommandError):
        call_command("seed_event", participants=0)
    assert not Event.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_seed_event_rolls_back_on_generation_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(ParticipantTag.objects, "bulk_create", fail)
    with pytest.raises(RuntimeError, match="simulated failure"):
        call_command("seed_event", participants=5, seed=3)
    assert not Event.objects.exists()
    assert not Tag.objects.exists()
