from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0026_complete_legacy_pending_registrations")]

    operations = [
        migrations.AddField(
            model_name="participant",
            name="registration_receipt_pending",
            field=models.BooleanField(default=False, db_index=True),
        ),
        migrations.AddField(
            model_name="participant",
            name="registration_receipt_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
