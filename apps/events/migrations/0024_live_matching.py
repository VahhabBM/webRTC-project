"""Preserve cancelled attempts while allowing a replacement in the same round."""

from importlib import import_module

from django.db import migrations, models


def update_pair_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    original = import_module(
        "apps.events.migrations.0003_pair_no_repeat_partner_constraints"
    ).PAIR_EVENT_TRIGGER_SQL
    sql = original.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    sql = sql.replace(
        "    PERFORM pg_advisory_xact_lock(",
        "    IF NEW.status = 'cancelled' THEN RETURN NEW; END IF;\n"
        "    PERFORM pg_advisory_xact_lock(",
        1,
    ).replace(
        "WHERE round_id = NEW.round_id\n          AND id <> NEW.id",
        "WHERE round_id = NEW.round_id\n          AND status <> 'cancelled'\n          AND id <> NEW.id",
    ).replace(
        "CREATE TRIGGER events_pair_validate_constraints_trigger",
        "DROP TRIGGER IF EXISTS events_pair_validate_constraints_trigger ON events_pair;\n"
        "CREATE TRIGGER events_pair_validate_constraints_trigger",
    ).replace(
        "UPDATE OF event_id, round_id, participant_a_id, participant_b_id",
        "UPDATE OF event_id, round_id, participant_a_id, participant_b_id, status",
    )
    schema_editor.execute(sql)


class Migration(migrations.Migration):
    dependencies = [("events", "0023_event_num_rounds_max_8")]
    operations = [
        migrations.AddField(
            model_name="participant", name="city",
            field=models.CharField(max_length=120, blank=True, default=""),
        ),
        migrations.AddField(
            model_name="event", name="live_matching_enabled",
            field=models.BooleanField(default=True, help_text="Allocate pairs from currently online, media-ready participants."),
        ),
        migrations.AddField(
            model_name="pair", name="live_allocated", field=models.BooleanField(default=False),
        ),
        migrations.AddField(model_name="pair", name="media_received_a", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="pair", name="media_received_b", field=models.BooleanField(default=False)),
        migrations.RemoveConstraint(model_name="pair", name="uniq_pair_participant_a_per_round"),
        migrations.RemoveConstraint(model_name="pair", name="uniq_pair_participant_b_per_round"),
        migrations.AddConstraint(
            model_name="pair",
            constraint=models.UniqueConstraint(fields=("round", "participant_a"), condition=~models.Q(status="cancelled"), name="uniq_pair_participant_a_per_round"),
        ),
        migrations.AddConstraint(
            model_name="pair",
            constraint=models.UniqueConstraint(fields=("round", "participant_b"), condition=~models.Q(status="cancelled"), name="uniq_pair_participant_b_per_round"),
        ),
        # Forward-only: restoring old constraints would reject valid replacement history.
        migrations.RunPython(update_pair_trigger),
    ]
