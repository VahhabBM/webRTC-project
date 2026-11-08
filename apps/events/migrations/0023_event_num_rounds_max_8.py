import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0022_participant_access_token"),
    ]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="num_rounds",
            field=models.PositiveSmallIntegerField(
                default=5,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(8),
                ],
            ),
        ),
    ]
