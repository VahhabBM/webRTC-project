import datetime

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F


def backfill_legacy_registration_status(apps, schema_editor):
    Participant = apps.get_model("events", "Participant")
    Participant.objects.filter(
        joined_at__isnull=False, email_verified_at__isnull=True
    ).update(email_verified_at=F("joined_at"))
    Participant.objects.filter(
        joined_at__isnull=True, registration_status="confirmed"
    ).update(registration_status="waitlisted")


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0015_pairinterest"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="num_rounds",
            field=models.PositiveSmallIntegerField(
                default=5,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(7),
                ],
            ),
        ),
        migrations.AlterField(
            model_name="event",
            name="round_duration",
            field=models.DurationField(
                default=datetime.timedelta(minutes=7),
                validators=[
                    django.core.validators.MinValueValidator(
                        datetime.timedelta(seconds=1)
                    )
                ],
            ),
        ),
        migrations.AlterField(
            model_name="event",
            name="break_duration",
            field=models.DurationField(
                default=datetime.timedelta(minutes=2),
                validators=[
                    django.core.validators.MinValueValidator(
                        datetime.timedelta(0)
                    )
                ],
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="male_date_limit",
            field=models.PositiveSmallIntegerField(default=5),
        ),
        migrations.AddField(
            model_name="event",
            name="female_date_limit",
            field=models.PositiveSmallIntegerField(default=7),
        ),
        migrations.AddField(
            model_name="event",
            name="other_date_limit",
            field=models.PositiveSmallIntegerField(default=5),
        ),
        migrations.AddField(
            model_name="event",
            name="intro_video_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Optional HTTPS video URL played before the first round.",
                validators=[django.core.validators.URLValidator(schemes=["https"])],
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="intro_video_duration_seconds",
            field=models.PositiveSmallIntegerField(
                default=0,
                validators=[django.core.validators.MaxValueValidator(300)],
            ),
        ),
        migrations.AddField(
            model_name="participant",
            name="registration_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending email verification"),
                    ("confirmed", "Confirmed"),
                    ("waitlisted", "Waitlisted"),
                    ("withdrawn", "Withdrawn"),
                ],
                db_index=True,
                default="confirmed",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="participant",
            name="email_verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="participant",
            name="reservation_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="participant",
            name="intro_video_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            backfill_legacy_registration_status, migrations.RunPython.noop
        ),
        migrations.CreateModel(
            name="WaitlistInvitation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="meetakk_waitlist_invitations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="waitlist_invitations",
                        to="events.event",
                    ),
                ),
                (
                    "participant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="waitlist_invitations",
                        to="events.participant",
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(
                        fields=["event", "created_at"],
                        name="events_wait_e8a2f3_idx",
                    )
                ],
            },
        ),
    ]
