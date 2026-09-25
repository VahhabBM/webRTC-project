# Generated for T-22: Event state machine, transition audit log, leader lease.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0006_alter_event_scoring_weights"),
    ]

    operations = [
        # ------------------------------------------------------------------ #
        # LeaderLease — distributed leader election table                     #
        # ------------------------------------------------------------------ #
        migrations.CreateModel(
            name="LeaderLease",
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
                ("lease_name", models.CharField(max_length=255, unique=True)),
                ("leader_id", models.CharField(max_length=255)),
                ("acquired_at", models.DateTimeField()),
                ("expires_at", models.DateTimeField()),
                ("version", models.PositiveIntegerField(default=1)),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["lease_name", "expires_at"],
                        name="events_lead_lease_n_expires_idx",
                    ),
                ],
            },
        ),
        # ------------------------------------------------------------------ #
        # EventTransitionLog — two-phase audit trail                          #
        # ------------------------------------------------------------------ #
        migrations.CreateModel(
            name="EventTransitionLog",
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
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transition_logs",
                        to="events.event",
                    ),
                ),
                ("from_status", models.CharField(max_length=30)),
                ("to_status", models.CharField(max_length=30)),
                ("transitioned_at", models.DateTimeField(auto_now_add=True)),
                ("leader_id", models.CharField(max_length=255)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("is_complete", models.BooleanField(default=False)),
            ],
            options={
                "ordering": ("transitioned_at",),
                "indexes": [
                    models.Index(
                        fields=["event", "transitioned_at"],
                        name="events_tlog_event_ts_idx",
                    ),
                    models.Index(
                        fields=["is_complete"],
                        name="events_tlog_complete_idx",
                    ),
                ],
            },
        ),
        # ------------------------------------------------------------------ #
        # Extend EventStatus choices (no DB column change needed — choices    #
        # are validated in Python only; PostgreSQL stores them as plain text) #
        # ------------------------------------------------------------------ #
        migrations.AlterField(
            model_name="event",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("scheduled", "Scheduled"),
                    ("active", "Active"),
                    ("completed", "Completed"),
                    ("cancelled", "Cancelled"),
                    ("registration_open", "Registration Open"),
                    ("locked", "Locked"),
                    ("running", "Running"),
                    ("paused", "Paused"),
                ],
                default="draft",
                max_length=20,
            ),
        ),
    ]
