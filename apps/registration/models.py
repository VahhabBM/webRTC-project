from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.events.models import Participant

TOKEN_TTL = timedelta(hours=getattr(settings, "EMAIL_VERIFICATION_TTL_HOURS", 24))


class EmailVerificationToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    participant = models.ForeignKey(
        Participant, on_delete=models.CASCADE, related_name="verification_tokens"
    )
    token = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=("token",)),
            models.Index(fields=("participant", "used_at")),
        ]

    @classmethod
    def create_for(cls, participant: Participant) -> EmailVerificationToken:
        raw_token = secrets.token_urlsafe(32)
        return cls.objects.create(
            participant=participant,
            token=raw_token,
            expires_at=timezone.now() + TOKEN_TTL,
        )

    def is_valid(self) -> bool:
        return self.used_at is None and self.expires_at > timezone.now()

    def mark_used(self) -> None:
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])


class TermsAcceptance(models.Model):
    participant = models.OneToOneField(
        Participant, on_delete=models.CASCADE, related_name="terms_acceptance"
    )
    accepted_at = models.DateTimeField(auto_now_add=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)


class DeviceCheckLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        related_name="device_check_logs",
        null=True,
        blank=True,
    )
    participant = models.ForeignKey(
        "events.Participant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_checks",
    )
    camera_working = models.BooleanField(default=False)
    mic_working = models.BooleanField(default=False)
    error_type = models.CharField(max_length=64, blank=True, default="")
    user_agent = models.TextField(blank=True, default="")
    is_in_app_browser = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"DeviceCheck (Cam: {self.camera_working}, Mic: {self.mic_working}, Err: {self.error_type or 'None'})"
