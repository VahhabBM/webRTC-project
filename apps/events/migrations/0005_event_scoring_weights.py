from __future__ import annotations

from django.db import migrations, models


def _default_scoring_weights():
    return {"tag_overlap": 1.0}


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0004_participant_join_token_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="scoring_weights",
            field=models.JSONField(default=_default_scoring_weights),
        ),
    ]
