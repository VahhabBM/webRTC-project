import django.db.models.deletion
from django.db import migrations, models


def populate_pair_events(apps, schema_editor):
    Pair = apps.get_model("events", "Pair")
    Round = apps.get_model("events", "Round")

    round_events = dict(Round.objects.values_list("id", "event_id"))
    for pair in Pair.objects.only("id", "round_id").iterator():
        pair.event_id = round_events[pair.round_id]
        pair.save(update_fields=["event_id"])


PAIR_EVENT_TRIGGER_SQL = """
CREATE FUNCTION events_pair_validate_constraints()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM events_round
        WHERE id = NEW.round_id
          AND event_id = NEW.event_id
    ) THEN
        RAISE EXCEPTION 'Pair event must match round event'
            USING ERRCODE = '23514';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM events_participant
        WHERE id = NEW.participant_a_id
          AND event_id = NEW.event_id
    ) OR NOT EXISTS (
        SELECT 1
        FROM events_participant
        WHERE id = NEW.participant_b_id
          AND event_id = NEW.event_id
    ) THEN
        RAISE EXCEPTION 'Pair participants must belong to pair event'
            USING ERRCODE = '23514';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            NEW.round_id::text || ':' || LEAST(
                NEW.participant_a_id,
                NEW.participant_b_id
            )::text,
            0
        )
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            NEW.round_id::text || ':' || GREATEST(
                NEW.participant_a_id,
                NEW.participant_b_id
            )::text,
            0
        )
    );

    IF EXISTS (
        SELECT 1
        FROM events_pair
        WHERE round_id = NEW.round_id
          AND id <> NEW.id
          AND (
              participant_a_id IN (
                  NEW.participant_a_id,
                  NEW.participant_b_id
              )
              OR participant_b_id IN (
                  NEW.participant_a_id,
                  NEW.participant_b_id
              )
          )
    ) THEN
        RAISE EXCEPTION 'A participant may belong to only one pair per round'
            USING ERRCODE = '23505';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER events_pair_validate_constraints_trigger
BEFORE INSERT OR UPDATE OF event_id, round_id, participant_a_id, participant_b_id
ON events_pair
FOR EACH ROW
EXECUTE FUNCTION events_pair_validate_constraints();
"""

PAIR_EVENT_TRIGGER_REVERSE_SQL = """
DROP TRIGGER IF EXISTS events_pair_validate_constraints_trigger ON events_pair;
DROP FUNCTION IF EXISTS events_pair_validate_constraints();
"""


class Migration(migrations.Migration):
    dependencies = [
        (
            "events",
            "0002_remove_pair_pair_participants_are_ordered_and_distinct_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="pair",
            name="event",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="pairs",
                to="events.event",
            ),
        ),
        migrations.RunPython(populate_pair_events, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="pair",
            name="event",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="pairs",
                to="events.event",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="pair",
            name="pair_participants_are_distinct",
        ),
        migrations.RemoveConstraint(
            model_name="pair",
            name="uniq_pair_per_round",
        ),
        migrations.AddConstraint(
            model_name="pair",
            constraint=models.CheckConstraint(
                condition=models.Q(("participant_a__lt", models.F("participant_b"))),
                name="pair_participants_are_ordered",
            ),
        ),
        migrations.AddConstraint(
            model_name="pair",
            constraint=models.UniqueConstraint(
                fields=("event", "participant_a", "participant_b"),
                name="uniq_pair_per_event",
            ),
        ),
        migrations.AddConstraint(
            model_name="pair",
            constraint=models.UniqueConstraint(
                fields=("round", "participant_a"),
                name="uniq_pair_participant_a_per_round",
            ),
        ),
        migrations.AddConstraint(
            model_name="pair",
            constraint=models.UniqueConstraint(
                fields=("round", "participant_b"),
                name="uniq_pair_participant_b_per_round",
            ),
        ),
        migrations.RunSQL(
            sql=PAIR_EVENT_TRIGGER_SQL,
            reverse_sql=PAIR_EVENT_TRIGGER_REVERSE_SQL,
        ),
    ]
