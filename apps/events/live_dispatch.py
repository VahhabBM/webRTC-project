"""Dispatch live allocations and truthful waiting states without resetting calls."""

from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.events.live_matching import reconcile_live_round
from apps.events.models import Pair, PairStatus
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
    "waiting_next_round": "Your date is complete. Your next date starts with the next round.",
    "looking_for_date": "We're finding your next date. Please stay here.",
    "limit_reached": "You have completed your dates for tonight. You will not receive any more dates. We hope you have a wonderful night!",
    "no_compatible_partner": "No new compatible partner is available right now. We will check again automatically.",
    "waiting_for_partner": "You are ready. We are waiting for another compatible person to be online. We will check again automatically.",
    "standby_fewer_women": "You are in the Main Room for this round because there are fewer women than men active in this round. You will automatically enter your next date in approximately 7 minutes.",
    "standby_fewer_men": "You are in the Main Room for this round because there are fewer men than women active in this round. You will automatically enter your next date in approximately 7 minutes.",
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
            waiting.setdefault(pid, "partner_left")
    for pid, info in (allocations.get("holding") or {}).items():
        name = (info or {}).get("name") or "Your partner"
        key = f"live:hold:{event.pk}:{pid}"
        if not cache.add(key, True, timeout=70):
            continue
        orch._publish(
            participant_channel_group(pid),
            build_server_waiting(
                reason="partner_holding",
                round_number=round_obj.number,
                server_ts=stamp,
                next_check_ts=stamp + 60000,
                message=(
                    f"{name}'s connection dropped. We'll hold this date for 1 minute. "
                    f"If {name} comes back in time, you'll continue together. "
                    "If not, we'll match you with your next date."
                ),
            ),
        )
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
    _publish_recovery_interest(orch, event, now)
    return result


def _publish_recovery_interest(orch, event, now) -> None:
    """A recovery date can end after its round, so the round-end prompt would be late."""
    ended = Pair.objects.filter(
        event=event,
        recovery=True,
        status=PairStatus.COMPLETED,
        date_ends_at__lte=now,
        date_ends_at__gte=now - timedelta(seconds=60),
    ).select_related("participant_a", "participant_b", "round", "interest")
    for pair in ended:
        if not cache.add(f"live:recovery-interest:{pair.pk}", True, timeout=120):
            continue
        for group, message in orch.recovery_interest_jobs(pair, now=now):
            orch._publish(group, message)
