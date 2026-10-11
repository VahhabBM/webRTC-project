# Generated migration for unique_tags field on Event

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0011_participantincidentnote_disconnectionlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="unique_tags",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, each participant can select only one tag for this event. "
                    "Use this for age ranges or other mutually exclusive categories. "
                    "When disabled, the existing multiple-tag behavior remains unchanged."
                ),
                verbose_name="Unique tag selection",
            ),
        ),
    ]
