from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0030_event_reminder_and_exit_settings'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='max_dates_per_participant',
            field=models.PositiveSmallIntegerField(
                blank=True,
                default=None,
                help_text='Maximum dates any participant can attend in this event (across all genders). If set, this caps everyone regardless of gender.',
                null=True,
            ),
        ),
    ]
