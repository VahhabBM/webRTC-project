from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0031_event_max_dates_per_participant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="num_rounds",
            field=models.PositiveSmallIntegerField(
                default=5,
                validators=[MinValueValidator(1), MaxValueValidator(15)],
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="participant_age_min",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Minimum participant age for this event (inclusive). Leave blank for 18+ only.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="participant_age_max",
            field=models.PositiveSmallIntegerField(
                blank=True,
                help_text="Maximum participant age for this event (inclusive).",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="RoundFeedback",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("rating", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("comment", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="round_feedbacks",
                        to="events.event",
                    ),
                ),
                (
                    "participant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="round_feedbacks",
                        to="events.participant",
                    ),
                ),
                (
                    "round",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feedbacks",
                        to="events.round",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["event", "round"], name="events_round_event_6f0c2f_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("round", "participant"),
                        name="uniq_round_feedback_per_participant",
                    )
                ],
            },
        ),
    ]