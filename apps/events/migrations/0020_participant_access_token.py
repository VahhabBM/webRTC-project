import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "events",
            "0019_rename_events_wait_e8a2f3_idx_events_wait_event_i_678a27_idx_and_more",
        ),
    ]

    operations = [
        migrations.CreateModel(
            name="ParticipantAccessToken",
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
                ("digest", models.CharField(max_length=64, unique=True)),
                ("token_hash", models.CharField(max_length=256)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "participant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_tokens",
                        to="events.participant",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="participantaccesstoken",
            index=models.Index(
                fields=["participant", "expires_at"],
                name="events_access_parti_24ad09_idx",
            ),
        ),
    ]
