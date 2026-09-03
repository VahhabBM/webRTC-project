from __future__ import annotations

import secrets
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
