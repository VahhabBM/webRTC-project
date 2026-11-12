"""Dispatch live allocations and truthful waiting states without resetting calls."""

from django.core.cache import cache
from django.utils import timezone

from apps.events.live_matching import reconcile_live_round
from apps.events.orchestrator import (
    OrchestratorRealtime,
    datetime_to_unix_ms,
    participant_channel_group,
)
from apps.events.scheduler import EventPhase, RoundScheduler
from apps.protocol.constants import MessageType
from apps.protocol.schemas import build_server_waiting

WAIT_MESSAGES = {
    "partner_left": "Your partner is no longer available. We are looking for another compatible person who is online.",
    "limit_reached": "You have reached your event date limit. You can remain here until the event ends.",
    "no_compatible_partner": "No new compatible partner is available right now. We will check again automatically.",
    "waiting_for_partner": "You are ready. We are waiting for another compatible person to be online. We will check again automatically.",
}


def dispatch_live_event(event, *, now=None):
    now = now or timezone.now()
    orch = OrchestratorRealtime(event)
    phase, round_obj, _remaining = RoundScheduler(event).get_current_phase(now)
    allocations = None
    if event.live_matching_enabled and round_obj and phase in (
        EventPhase.PRECONNECT, EventPhase.IN_ROUND
    ):
        allocations = reconcile_live_round(event, round_obj, now=now)

    result = orch.dispatch(now=now)
    if allocations is None:
        return result

    rooms = {pair.room_id for pair in allocations["new_pairs"]}
    new_participants = {
        str(pid)
        for pair in allocations["new_pairs"]
        for pid in (pair.participant_a_id, pair.participant_b_id)
    }
    stamp = datetime_to_unix_ms(now)
    waiting = dict(allocations["waiting"])
    for pid in allocations["cancelled_participant_ids"]:
        if pid in allocations["online_ids"] and pid not in new_participants:
            waiting[pid] = "partner_left"
    for pid, reason in waiting.items():
        key = f"live:wait:{event.pk}:{round_obj.pk}:{pid}:{reason}"
        if cache.add(key, True, timeout=10):
            delivered = orch._publish(
                participant_channel_group(pid),
                build_server_waiting(
                    reason=reason, round_number=round_obj.number,
                    server_ts=stamp, next_check_ts=stamp + 10000,
                    message=WAIT_MESSAGES[reason],
                ),
            )
            if not delivered:
                cache.delete(key)

    # Revisit every currently allocated room on each tick. Shared room and
    # participant keys deduplicate initial lifecycle sends while allowing a
    # newly assigned room to publish after the round's global broadcast.
    if MessageType.SERVER_PAIRING not in result.get("attempted", ()):
        for group, message in orch.pairing_jobs(round_obj):
            room_id = message["payload"]["room_id"]
            participant_id = group.removeprefix("participant_")
            orch._publish_once(
                orch.pairing_delivery_key(round_obj.number, room_id, participant_id),
                group,
                message,
            )
    if (
        phase == EventPhase.IN_ROUND
        and MessageType.SERVER_ROUND_START not in result.get("attempted", ())
    ):
        for group, message in orch.round_start_jobs(round_obj, now=now):
            room_id = message["payload"]["room_id"]
            participant_id = group.removeprefix("participant_")
            orch._publish_once(
                orch.round_start_delivery_key(
                    round_obj.number, room_id, participant_id
                ),
                group,
                message,
            )
    result["allocated"] = len(rooms)
    result["waiting"] = len(waiting)
    return result
