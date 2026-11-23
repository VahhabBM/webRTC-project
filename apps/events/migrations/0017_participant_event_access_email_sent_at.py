from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0016_participant_country"),
    ]

    operations = [
        migrations.AddField(
            model_name="participant",
            name="event_access_email_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
