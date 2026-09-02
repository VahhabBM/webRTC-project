from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth.hashers import check_password, make_password
from django.core.validators import MinValueValidator
from django.db import models


class EventStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    SCHEDULED = "scheduled", "Scheduled"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class ParticipantStatus(models.TextChoices):
    WAITING = "waiting", "Waiting"
    READY = "ready", "Ready"
    ACTIVE = "active", "Active"
    DISCONNECTED = "disconnected", "Disconnected"
    COMPLETED = "completed", "Completed"


class RoundStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class PairStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class Event(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=20, choices=EventStatus.choices, default=EventStatus.DRAFT
    )
    num_rounds = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    round_duration = models.DurationField(
        validators=[MinValueValidator(timedelta(seconds=1))]
    )
    break_duration = models.DurationField(
        validators=[MinValueValidator(timedelta(seconds=0))]
    )
    start_time = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-start_time",)
        indexes = [
            models.Index(fields=("status", "start_time")),
        ]

    def __str__(self) -> str:
        return self.name


class Tag(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    name = models.CharField(max_length=80, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class Participant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="participants"
    )
    display_name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=ParticipantStatus.choices,
        default=ParticipantStatus.WAITING,
    )
    join_token_hash = models.CharField(max_length=256)
    joined_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tags = models.ManyToManyField(
        Tag, through="ParticipantTag", related_name="participants"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("event", "display_name"), name="uniq_participant_name_per_event"
            ),
        ]
        indexes = [
            models.Index(fields=("event", "status")),
            models.Index(fields=("event", "created_at")),
        ]

    def __str__(self) -> str:
        return self.display_name

    def set_join_token(self, raw_token: str) -> None:
        if not raw_token:
            raise ValueError("Join token must not be empty")
        self.join_token_hash = make_password(raw_token)

    def verify_join_token(self, raw_token: str) -> bool:
        return bool(raw_token) and check_password(raw_token, self.join_token_hash)


class ParticipantTag(models.Model):
    participant = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="participant_tags"
    )
    tag = models.ForeignKey(
        Tag, on_delete=models.CASCADE, related_name="participant_tags"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("participant", "tag"), name="uniq_participant_tag"
            ),
        ]
        indexes = [
            models.Index(fields=("tag", "participant")),
        ]


class Round(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="rounds")
    number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(
        max_length=20, choices=RoundStatus.choices, default=RoundStatus.SCHEDULED
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("event", "number")
        constraints = [
            models.UniqueConstraint(
                fields=("event", "number"), name="uniq_round_number_per_event"
            ),
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")),
                name="round_ends_after_start",
            ),
        ]
        indexes = [
            models.Index(fields=("event", "status")),
            models.Index(fields=("event", "starts_at")),
        ]


class Pair(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="pairs")
    round = models.ForeignKey(Round, on_delete=models.CASCADE, related_name="pairs")
    participant_a = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="pairs_as_a"
    )
    participant_b = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="pairs_as_b"
    )
    status = models.CharField(
        max_length=20, choices=PairStatus.choices, default=PairStatus.PENDING
    )
    room_id = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(participant_a__lt=models.F("participant_b")),
                name="pair_participants_are_ordered",
            ),
            models.UniqueConstraint(
                fields=("event", "participant_a", "participant_b"),
                name="uniq_pair_per_event",
            ),
            models.UniqueConstraint(
                fields=("round", "participant_a"),
                name="uniq_pair_participant_a_per_round",
            ),
            models.UniqueConstraint(
                fields=("round", "participant_b"),
                name="uniq_pair_participant_b_per_round",
            ),
        ]
        indexes = [
            models.Index(fields=("round", "status")),
            models.Index(fields=("participant_a", "round")),
            models.Index(fields=("participant_b", "round")),
        ]

    def save(self, *args, **kwargs):
        if self.round_id and self.event_id is None:
            self.event_id = self.round.event_id
        if (
            self.participant_a_id
            and self.participant_b_id
            and self.participant_a_id > self.participant_b_id
        ):
            self.participant_a_id, self.participant_b_id = (
                self.participant_b_id,
                self.participant_a_id,
            )
        super().save(*args, **kwargs)
