from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0032_sprint16_rounds_age_feedback"),
    ]

    operations = [
        migrations.AddField(
            model_name="pair",
            name="talk_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pair",
            name="date_ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pair",
            name="recovery",
            field=models.BooleanField(default=False),
        ),
    ]
