# Add the participant's country of origin.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0015_pairinterest"),
    ]

    operations = [
        migrations.AddField(
            model_name="participant",
            name="country",
            field=models.CharField(blank=True, default="", max_length=2),
        ),
    ]
