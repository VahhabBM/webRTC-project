"""Transactional live allocation for participants with fresh media readiness."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import networkx as nx
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.events.geography import MAX_SCORE, geographic_score
from apps.events.models import (
    Pair,
    PairStatus,
    Participant,
    RegistrationStatus,
)
from apps.events.presence import get_online_participant_ids

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = timedelta(seconds=60)
_RECONNECT_GRACE = timedelta(seconds=15)
_ABSENCE_CACHE_TTL = 120
_GEOGRAPHY_SCALE = 100
_FAIRNESS_EDGE_CEILING = MAX_SCORE * _GEOGRAPHY_SCALE + 20_000
_PRECONNECT_WINDOW = timedelta(seconds=20)


def _absence_key(pair_id) -> str:
    return f"live-match:absence:{pair_id}"


def _gender(value: str | None) -> str:
    if value in ("Male", "Female"):
        return value
    return "Other"


def _cap(event, participant: Participant) -> int:
    gender = _gender(participant.gender)
    if gender == "Male":
        return max(0, int(event.male_date_limit))
    if gender == "Female":
        return max(0, int(event.female_date_limit))
    configured = getattr(settings, "LIVE_MATCH_OTHER_DATE_LIMIT", None)
    return max(0, int(configured if configured is not None else event.other_date_limit))


def _accepts(participant: Participant, partner: Participant) -> bool:
    preference = (participant.want_to_date or "Any").strip()
    return preference == "Any" or preference == _gender(partner.gender)


def _both_connected(pair: Pair) -> bool:
    return bool(
        pair.connection_type_a
        and pair.connection_type_b
        and pair.media_received_a
        and pair.media_received_b
    )


def _cancel(pair: Pair) -> None:
    if pair.status != PairStatus.CANCELLED:
        pair.status = PairStatus.CANCELLED
        pair.save(update_fields=["status"])
    try:
        cache.delete(_absence_key(pair.pk))
    except Exception:
        logger.exception("Unable to clear live-pair absence marker for %s", pair.pk)


def _mark_expired_pairs(event, now) -> None:
    expired = Pair.objects.select_for_update().filter(
        event=event,
        round__ends_at__lte=now,
        status__in=(PairStatus.PENDING, PairStatus.ACTIVE),
    )
    for pair in expired:
        pair.status = PairStatus.COMPLETED if _both_connected(pair) else PairStatus.CANCELLED
        pair.save(update_fields=["status"])
        try:
            cache.delete(_absence_key(pair.pk))
        except Exception:
            logger.exception("Unable to clear live-pair absence marker for %s", pair.pk)


def _discard_unconnected_static_pairs(event, now) -> None:
    """Free future/current generated schedule slots, retaining every media record."""
    Pair.objects.filter(
        event=event,
        live_allocated=False,
        status=PairStatus.PENDING,
        round__ends_at__gt=now,
        connection_type_a__isnull=True,
        connection_type_b__isnull=True,
    ).delete()


def _absence_expired(pair: Pair, missing: bool, now) -> bool:
    key = _absence_key(pair.pk)
    try:
        if not missing:
            cache.delete(key)
            return False
        started = cache.get(key)
        if started is None:
            cache.set(key, now.timestamp(), _ABSENCE_CACHE_TTL)
            return False
        return now.timestamp() - float(started) >= _RECONNECT_GRACE.total_seconds()
    except Exception:
        logger.exception("Unable to track live-pair absence for %s", pair.pk)
        # A cache outage cannot safely extend a short reconnect grace forever.
        return True


def _tag_sets(participants: list[Participant]) -> dict[str, set[str]]:
    ids = [p.pk for p in participants]
    rows = Participant.tags.through.objects.filter(participant_id__in=ids).values_list(
        "participant_id", "tag_id"
    )
    result: dict[str, set[str]] = {str(pid): set() for pid in ids}
    for participant_id, tag_id in rows:
        result[str(participant_id)].add(str(tag_id))
    return result


def _successful_counts(event) -> dict[str, int]:
    counts: dict[str, int] = {}
    rows = Pair.objects.filter(event=event).filter(
        connection_type_a__isnull=False,
        connection_type_b__isnull=False,
        media_received_a=True,
        media_received_b=True,
    ).values_list("participant_a_id", "participant_b_id")
    for a_id, b_id in rows:
        counts[str(a_id)] = counts.get(str(a_id), 0) + 1
        counts[str(b_id)] = counts.get(str(b_id), 0) + 1
    return counts


def reconcile_live_round(event, round_obj, now=None) -> dict:
    """Reconcile current round pairs and allocate ready compatible participants.

    Event row locking serializes allocation attempts across workers. The result
    contains newly created pairs, IDs canceled this pass, and truthful waiting
    reasons for online participants who remain unmatched.
    """
    current = now or timezone.now()
    if timezone.is_naive(current):
        current = timezone.make_aware(current, timezone.get_current_timezone())

    with transaction.atomic():
        from apps.events.models import Event

        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        online_ids = get_online_participant_ids(locked_event, current)
        result = {
            "new_pairs": [],
            "cancelled_participant_ids": set(),
            "waiting": {},
            "online_ids": online_ids,
        }
        if not locked_event.live_matching_enabled:
            return result

        # Validate the selected round before any destructive cleanup. In particular,
        # an early task invocation cannot remove static schedule rows.
        round_obj = type(round_obj).objects.select_for_update().get(pk=round_obj.pk)
        if round_obj.event_id != locked_event.pk:
            return result
        if current < round_obj.starts_at - _PRECONNECT_WINDOW:
            return result

        _mark_expired_pairs(locked_event, current)
        if current >= round_obj.ends_at:
            return result
        _discard_unconnected_static_pairs(locked_event, current)

        successful = _successful_counts(locked_event)
        participant_rows = list(
            Participant.objects.filter(
                event=locked_event,
                registration_status=RegistrationStatus.CONFIRMED,
                pk__in=online_ids,
            ).order_by("pk")
        )
        by_id = {str(participant.pk): participant for participant in participant_rows}

        # Expire failed or abandoned live assignments; successful calls remain active.
        pair_rows = list(
            Pair.objects.select_for_update()
            .filter(round=round_obj, status__in=(PairStatus.PENDING, PairStatus.ACTIVE))
            .select_related("participant_a", "participant_b", "round")
            .order_by("created_at", "pk")
        )
        occupied: set[str] = set()
        partner_left: set[str] = set()
        for pair in pair_rows:
            a_id, b_id = str(pair.participant_a_id), str(pair.participant_b_id)
            connected = _both_connected(pair)
            if connected:
                if pair.status != PairStatus.ACTIVE:
                    pair.status = PairStatus.ACTIVE
                    pair.save(update_fields=["status"])
                missing = a_id not in online_ids or b_id not in online_ids
                abandoned = _absence_expired(pair, missing, current)
                if abandoned:
                    _cancel(pair)
                    result["cancelled_participant_ids"].update((a_id, b_id))
                    partner_left.update(pid for pid in (a_id, b_id) if pid in online_ids)
                    continue
                # Successful telemetry keeps its date debt even if a participant
                # leaves; only the live assignment is released after grace.
                occupied.update((a_id, b_id))
                continue

            if pair.live_allocated:
                missing = a_id not in online_ids or b_id not in online_ids
                deadline_base = max(pair.created_at, round_obj.starts_at)
                timed_out = current >= deadline_base + _CONNECT_TIMEOUT
                abandoned = _absence_expired(pair, missing, current)
                if timed_out or abandoned:
                    _cancel(pair)
                    result["cancelled_participant_ids"].update((a_id, b_id))
                    partner_left.update(pid for pid in (a_id, b_id) if pid in online_ids)
                    continue
                # Pending live pairs reserve both participants while both devices
                # are fresh, and for the explicit short reconnect grace otherwise.
                occupied.update((a_id, b_id))
            elif pair.status == PairStatus.ACTIVE:
                # Static rows with telemetry are historical media evidence.
                occupied.update((a_id, b_id))

        all_event_pairs = list(
            Pair.objects.filter(event=locked_event).values_list(
                "participant_a_id", "participant_b_id"
            )
        )
        historical_neighbors: dict[str, set[str]] = {}
        for a_id, b_id in all_event_pairs:
            a_key, b_key = str(a_id), str(b_id)
            historical_neighbors.setdefault(a_key, set()).add(b_key)
            historical_neighbors.setdefault(b_key, set()).add(a_key)

        # Successful dates and active pending reservations both occupy a cap slot.
        reservations = {pid: count for pid, count in successful.items()}
        live_pending = Pair.objects.filter(
            event=locked_event,
            status__in=(PairStatus.PENDING, PairStatus.ACTIVE),
        ).exclude(
            connection_type_a__isnull=False,
            connection_type_b__isnull=False,
            media_received_a=True,
            media_received_b=True,
        ).values_list("participant_a_id", "participant_b_id")
        for a_id, b_id in live_pending:
            for pid in (str(a_id), str(b_id)):
                reservations[pid] = reservations.get(pid, 0) + 1

        candidates = [p for p in participant_rows if str(p.pk) not in occupied]
        tags = _tag_sets(candidates)
        graph = nx.Graph()
        graph.add_nodes_from(str(p.pk) for p in candidates)
        valid_edges: set[tuple[str, str]] = set()
        fairness_weight = (len(candidates) + 1) * _FAIRNESS_EDGE_CEILING
        sorted_candidates = sorted(candidates, key=lambda p: str(p.pk))
        for i, left in enumerate(sorted_candidates):
            left_id = str(left.pk)
            if reservations.get(left_id, 0) >= _cap(locked_event, left):
                continue
            for right in sorted_candidates[i + 1 :]:
                right_id = str(right.pk)
                if _gender(left.gender) == _gender(right.gender):
                    continue
                if {_gender(left.gender), _gender(right.gender)} != {"Male", "Female"}:
                    continue
                if reservations.get(right_id, 0) >= _cap(locked_event, right):
                    continue
                if right_id in historical_neighbors.get(left_id, set()):
                    continue
                if not (_accepts(left, right) and _accepts(right, left)):
                    continue

                geo = geographic_score(left, right)
                overlap = min(100, len(tags.get(left_id, set()) & tags.get(right_id, set())))
                debt = successful.get(left_id, 0) + successful.get(right_id, 0)
                deterministic = int.from_bytes(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{left_id}:{right_id}").bytes[:2],
                    "big",
                ) % 100
                weight = (
                    -debt * fairness_weight
                    + geo * _GEOGRAPHY_SCALE
                    + overlap * 100
                    + deterministic
                )
                graph.add_edge(left_id, right_id, weight=weight)
                valid_edges.add((left_id, right_id))

        has_time = round_obj.ends_at - current >= _CONNECT_TIMEOUT
        new_pairs = []
        if has_time and valid_edges:
            matching = nx.max_weight_matching(graph, maxcardinality=True, weight="weight")
            for pid_a, pid_b in sorted(tuple(sorted(edge)) for edge in matching):
                # UUID order is the canonical direction expected by the Pair model.
                a, b = by_id[pid_a], by_id[pid_b]
                pair = Pair.objects.create(
                    event=locked_event,
                    round=round_obj,
                    participant_a=a,
                    participant_b=b,
                    room_id=f"live-{uuid.uuid4().hex}",
                    status=PairStatus.PENDING,
                    live_allocated=True,
                )
                new_pairs.append(pair)
                occupied.update((pid_a, pid_b))
                reservations[pid_a] = reservations.get(pid_a, 0) + 1
                reservations[pid_b] = reservations.get(pid_b, 0) + 1
        result["new_pairs"] = new_pairs

        unmatched = [p for p in participant_rows if str(p.pk) not in occupied]
        for participant in unmatched:
            pid = str(participant.pk)
            has_opposite = (
                _gender(participant.gender) == "Male"
                and any(_gender(other.gender) == "Female" and str(other.pk) != pid for other in participant_rows)
            ) or (
                _gender(participant.gender) == "Female"
                and any(_gender(other.gender) == "Male" and str(other.pk) != pid for other in participant_rows)
            )
            if pid in partner_left:
                reason = "partner_left"
            elif reservations.get(pid, 0) >= _cap(locked_event, participant):
                reason = "limit_reached"
            elif not has_opposite:
                reason = "waiting_for_partner"
            else:
                reason = "no_compatible_partner"
            result["waiting"][pid] = reason
        return result
