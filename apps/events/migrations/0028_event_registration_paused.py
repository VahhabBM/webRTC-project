from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0027_registration_receipt")]

    operations = [
        migrations.AddField(
            model_name="event",
            name="registration_paused",
            field=models.BooleanField(
                default=False,
                help_text="Temporarily prevent new public registrations for this event.",
            ),
        ),
    ]
