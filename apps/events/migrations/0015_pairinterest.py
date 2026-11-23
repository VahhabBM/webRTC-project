import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0014_participant_age_participant_gender_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="PairInterest",
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
                    "participant_a_interested",
                    models.BooleanField(blank=True, null=True),
                ),
                (
                    "participant_a_responded_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "participant_b_interested",
                    models.BooleanField(blank=True, null=True),
                ),
                (
                    "participant_b_responded_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("email_sent_to_a_at", models.DateTimeField(blank=True, null=True)),
                ("email_sent_to_b_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "pair",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="interest",
                        to="events.pair",
                    ),
                ),
            ],
        ),
    ]
