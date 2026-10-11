# Merge migration: resolves two parallel 0012 leaves.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0012_event_unique_tags"),
        ("events", "0012_pair_path_type"),
    ]

    operations = []
