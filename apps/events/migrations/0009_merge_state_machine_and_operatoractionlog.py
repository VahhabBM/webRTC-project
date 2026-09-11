# Merge migration: resolves the two parallel 0007 leaf nodes.
#   0008_rename_...  (descends from 0007_event_state_machine)
#   0007_operatoractionlog

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        (
            "events",
            "0008_rename_events_tlog_event_ts_idx_events_even_event_i_bf42a7_idx_and_more",
        ),
        ("events", "0007_operatoractionlog"),
    ]

    operations = []
