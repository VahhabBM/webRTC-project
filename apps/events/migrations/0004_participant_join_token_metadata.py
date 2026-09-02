from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0003_pair_no_repeat_partner_constraints")]

    operations = [
        migrations.AddField(
            model_name="participant",
            name="join_token_digest",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=64
            ),
        ),
        migrations.AddField(
            model_name="participant",
            name="join_token_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
