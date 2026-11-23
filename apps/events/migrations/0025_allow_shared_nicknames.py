from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("events", "0024_live_matching")]
    operations = [
        migrations.RemoveConstraint(
            model_name="participant", name="uniq_participant_name_per_event"
        ),
    ]
