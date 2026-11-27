"""Transactional live allocation for participants with fresh media readiness."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import networkx as nx
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.events.geography import MAX_SCORE, geographic_score
from apps.events.models import (
    Pair,
    PairStatus,
    Participant,
    RegistrationStatus,
    Round,
)
from apps.events.presence import get_online_participant_ids

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = timedelta(seconds=60)
_RECONNECT_GRACE = timedelta(seconds=60)
_SHORT_TALK = timedelta(minutes=4)
_RECOVERY_LENGTH = timedelta(minutes=6)
_ABSENCE_CACHE_TTL = 120
_GEOGRAPHY_SCALE = 100
_AGE_PREF_SCALE = 12_000
_FAIRNESS_EDGE_CEILING = MAX_SCORE * _GEOGRAPHY_SCALE + 20_000
_PRECONNECT_WINDOW = timedelta(seconds=20)


def _absence_key(pair_id) -> str:
    return f"live-match:absence:{pair_id}"


def _talked_key(pair_id) -> str:
    return f"live-match:talked:{pair_id}"


def _defer_key(participant_id) -> str:
    return f"live-match:defer:{participant_id}"


def _full_key(participant_id) -> str:
    return f"live-match:full:{participant_id}"


def _pair_deadline(pair: Pair):
    return pair.date_ends_at or pair.round.ends_at


def _next_round_open(event, after):
    nxt = (
        Round.objects.filter(event=event, starts_at__gt=after)
        .order_by("starts_at")
        .only("starts_at")
        .first()
    )
    if nxt is None:
        return after + timedelta(hours=12)
    return nxt.starts_at - _PRECONNECT_WINDOW


def _defer_until_next_round(event, after, *participant_ids) -> None:
    until = _next_round_open(event, after).timestamp()
    for pid in participant_ids:
        try:
            cache.set(_defer_key(str(pid)), until, 6 * 60 * 60)
            cache.delete(_full_key(str(pid)))
        except Exception:
            logger.exception("Unable to defer participant %s", pid)


def _mark_needs_full(*participant_ids) -> None:
    for pid in participant_ids:
        try:
            cache.set(_full_key(str(pid)), 1, 60 * 60)
            cache.delete(_defer_key(str(pid)))
        except Exception:
            logger.exception("Unable to mark a full date for %s", pid)


def _is_deferred(participant_id, now) -> bool:
    try:
        until = cache.get(_defer_key(str(participant_id)))
    except Exception:
        logger.exception("Unable to read deferral for %s", participant_id)
        return False
    return until is not None and now.timestamp() < float(until)


def _wants_full(participant_id) -> bool:
    try:
        return bool(cache.get(_full_key(str(participant_id))))
    except Exception:
        logger.exception("Unable to read full-date flag for %s", participant_id)
        return False


def _clear_full(*participant_ids) -> None:
    for pid in participant_ids:
        try:
            cache.delete(_full_key(str(pid)))
        except Exception:
            logger.exception("Unable to clear full-date flag for %s", pid)


def _remember_talked(pair: Pair, now) -> float:
    key = _talked_key(pair.pk)
    try:
        stored = cache.get(key)
        if stored is not None:
            return float(stored)
        start = pair.talk_started_at or pair.created_at
        talked = max(0.0, (now - start).total_seconds())
        cache.set(key, talked, _ABSENCE_CACHE_TTL)
        return talked
    except Exception:
        logger.exception("Unable to record talk time for %s", pair.pk)
        start = pair.talk_started_at or pair.created_at
        return max(0.0, (now - start).total_seconds())


def _gender(value: str | None) -> str:
    if value in ("Male", "Female"):
        return value
    return "Other"


def _cap(event, participant: Participant) -> int:
    if getattr(event, "max_dates_per_participant", None) is not None:
        return max(0, int(event.max_dates_per_participant))
    gender = _gender(participant.gender)
    if gender == "Male":
        return max(0, int(event.male_date_limit))
    if gender == "Female":
        return max(0, int(event.female_date_limit))
    configured = getattr(settings, "LIVE_MATCH_OTHER_DATE_LIMIT", None)
    return max(0, int(configured if configured is not None else event.other_date_limit))


def _within_age_preference(participant: Participant, partner: Participant) -> bool:
    """True when partner.age is inside participant's declared preference range."""
    age = partner.age
    if age is None:
        return True
    lo = participant.age_pref_min
    hi = participant.age_pref_max
    if lo is None and hi is None:
        return True
    if lo is not None and age < lo:
        return False
    if hi is not None and age > hi:
        return False
    return True


def _age_preference_score(left: Participant, right: Participant) -> int:
    """Higher when both participants' ages fit each other's preferred ranges."""
    if not (_within_age_preference(left, right) and _within_age_preference(right, left)):
        return 0
    score = 100
    if left.age is not None and right.age is not None:
        gap = abs(left.age - right.age)
        score += max(0, 40 - gap)
    return score


def _accepts(participant: Participant, partner: Participant) -> bool:
    preference = (participant.want_to_date or "Any").strip()
    prefs = [p.strip() for p in preference.split(",") if p.strip()]
    if "Any" in prefs or "Any/Other" in prefs:
        return True
    return _gender(partner.gender) in prefs


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
    live = (
        Pair.objects.select_for_update()
        .filter(event=event, status__in=(PairStatus.PENDING, PairStatus.ACTIVE))
        .select_related("round")
    )
    for pair in live:
        end = _pair_deadline(pair)
        if end > now:
            continue
        recovery = bool(pair.recovery)
        pair.status = PairStatus.COMPLETED if _both_connected(pair) else PairStatus.CANCELLED
        pair.save(update_fields=["status"])
        try:
            cache.delete(_absence_key(pair.pk))
            cache.delete(_talked_key(pair.pk))
        except Exception:
            logger.exception("Unable to clear live-pair absence marker for %s", pair.pk)
        if recovery:
            _defer_until_next_round(event, end, pair.participant_a_id, pair.participant_b_id)


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


def _absence_state(pair: Pair, missing: bool, now) -> tuple[bool, float | None]:
    """Return (grace expired, absence start timestamp)."""
    key = _absence_key(pair.pk)
    try:
        if not missing:
            cache.delete(key)
            cache.delete(_talked_key(pair.pk))
            return False, None
        started = cache.get(key)
        if started is None:
            cache.set(key, now.timestamp(), _ABSENCE_CACHE_TTL)
            _remember_talked(pair, now)
            return False, now.timestamp()
        started = float(started)
        expired = now.timestamp() - started >= _RECONNECT_GRACE.total_seconds()
        return expired, started
    except Exception:
        logger.exception("Unable to track live-pair absence for %s", pair.pk)
        # A cache outage cannot safely extend a short reconnect grace forever.
        return True, None


def _absence_expired(pair: Pair, missing: bool, now) -> bool:
    expired, _started = _absence_state(pair, missing, now)
    return expired


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
            "holding": {},
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
            .filter(event=locked_event, status__in=(PairStatus.PENDING, PairStatus.ACTIVE))
            .filter(Q(round=round_obj) | Q(recovery=True, date_ends_at__gt=current))
            .select_related("participant_a", "participant_b", "round")
            .order_by("created_at", "pk")
        )
        occupied: set[str] = set()
        partner_left: set[str] = set()
        for pair in pair_rows:
            a_id, b_id = str(pair.participant_a_id), str(pair.participant_b_id)
            connected = _both_connected(pair)
            if connected:
                if pair.talk_started_at is None:
                    pair.talk_started_at = pair.created_at
                    pair.save(update_fields=["talk_started_at"])
                if pair.status != PairStatus.ACTIVE:
                    pair.status = PairStatus.ACTIVE
                    pair.save(update_fields=["status"])
                missing = a_id not in online_ids or b_id not in online_ids
                abandoned, started = _absence_state(pair, missing, current)
                if abandoned:
                    talked = _remember_talked(pair, current)
                    _cancel(pair)
                    result["cancelled_participant_ids"].update((a_id, b_id))
                    if talked >= _SHORT_TALK.total_seconds():
                        _defer_until_next_round(locked_event, current, a_id, b_id)
                    else:
                        _mark_needs_full(a_id, b_id)
                    continue
                occupied.update((a_id, b_id))
                if missing and started is not None:
                    left = max(
                        0,
                        int(
                            _RECONNECT_GRACE.total_seconds()
                            - (current.timestamp() - started)
                        ),
                    )
                    for pid, other in (
                        (a_id, pair.participant_b),
                        (b_id, pair.participant_a),
                    ):
                        if pid in online_ids and str(other.pk) not in online_ids:
                            result["holding"][pid] = {
                                "name": other.display_name or "Your partner",
                                "seconds_left": left,
                            }
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

        candidates = [
            p
            for p in participant_rows
            if str(p.pk) not in occupied and not _is_deferred(str(p.pk), current)
        ]
        tags = _tag_sets(candidates)
        graph = nx.Graph()
        graph.add_nodes_from(str(p.pk) for p in candidates)
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
                age_score = _age_preference_score(left, right)
                overlap = min(100, len(tags.get(left_id, set()) & tags.get(right_id, set())))
                debt = successful.get(left_id, 0) + successful.get(right_id, 0)
                deterministic = int.from_bytes(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{left_id}:{right_id}").bytes[:2],
                    "big",
                ) % 100
                weight = (
                    -debt * fairness_weight
                    + age_score * _AGE_PREF_SCALE
                    + geo * _GEOGRAPHY_SCALE
                    + overlap * 100
                    + deterministic
                )
                graph.add_edge(left_id, right_id, weight=weight)

        round_duration = round_obj.ends_at - round_obj.starts_at
        min_required_time = min(_CONNECT_TIMEOUT, max(timedelta(seconds=15), round_duration / 4))
        has_time = round_obj.ends_at - current >= min_required_time
        new_pairs = []

        def _create_pair(pid_a, pid_b, *, recovery: bool) -> None:
            pair = Pair.objects.create(
                event=locked_event,
                round=round_obj,
                participant_a=by_id[pid_a],
                participant_b=by_id[pid_b],
                room_id=f"live-{uuid.uuid4().hex}",
                status=PairStatus.PENDING,
                live_allocated=True,
                recovery=recovery,
                date_ends_at=(current + _RECOVERY_LENGTH) if recovery else None,
            )
            new_pairs.append(pair)
            occupied.update((pid_a, pid_b))
            reservations[pid_a] = reservations.get(pid_a, 0) + 1
            reservations[pid_b] = reservations.get(pid_b, 0) + 1
            _clear_full(pid_a, pid_b)

        full_ids = {node for node in graph.nodes if _wants_full(node)}
        if full_ids:
            recovery_graph = nx.Graph()
            for left_id, right_id, data in graph.edges(data=True):
                if left_id in full_ids or right_id in full_ids:
                    recovery_graph.add_edge(left_id, right_id, **data)
            if recovery_graph.number_of_edges():
                matching = nx.max_weight_matching(
                    recovery_graph, maxcardinality=True, weight="weight"
                )
                for pid_a, pid_b in sorted(tuple(sorted(edge)) for edge in matching):
                    _create_pair(pid_a, pid_b, recovery=True)
                    if graph.has_node(pid_a):
                        graph.remove_node(pid_a)
                    if graph.has_node(pid_b):
                        graph.remove_node(pid_b)
            for pid in full_ids:
                if graph.has_node(pid):
                    graph.remove_node(pid)

        if has_time and graph.number_of_edges():
            matching = nx.max_weight_matching(graph, maxcardinality=True, weight="weight")
            for pid_a, pid_b in sorted(tuple(sorted(edge)) for edge in matching):
                _create_pair(pid_a, pid_b, recovery=False)
        result["new_pairs"] = new_pairs

        unmatched = [p for p in participant_rows if str(p.pk) not in occupied]
        total_males = sum(1 for p in participant_rows if _gender(p.gender) == "Male")
        total_females = sum(1 for p in participant_rows if _gender(p.gender) == "Female")
        for participant in unmatched:
            pid = str(participant.pk)
            has_opposite = (
                _gender(participant.gender) == "Male"
                and any(_gender(other.gender) == "Female" and str(other.pk) != pid for other in participant_rows)
            ) or (
                _gender(participant.gender) == "Female"
                and any(_gender(other.gender) == "Male" and str(other.pk) != pid for other in participant_rows)
            )
            if _is_deferred(pid, current):
                reason = "waiting_next_round"
            elif pid in partner_left:
                reason = "partner_left"
            elif reservations.get(pid, 0) >= _cap(locked_event, participant):
                reason = "limit_reached"
            elif _wants_full(pid):
                reason = "looking_for_date"
            elif not has_opposite:
                reason = "waiting_for_partner"
            elif _gender(participant.gender) == "Male" and total_females < total_males:
                reason = "standby_fewer_women"
            elif _gender(participant.gender) == "Female" and total_males < total_females:
                reason = "standby_fewer_men"
            else:
                reason = "no_compatible_partner"
            result["waiting"][pid] = reason
        return result


def live_room_snapshot(event, now=None) -> dict:
    """Connected, disconnected, and date-state counts for live monitoring."""
    current = now or timezone.now()
    if timezone.is_naive(current):
        current = timezone.make_aware(current, timezone.get_current_timezone())
    online = {str(pid) for pid in get_online_participant_ids(event, current)}
    confirmed = list(
        Participant.objects.filter(
            event=event,
            registration_status=RegistrationStatus.CONFIRMED,
        ).only("id", "joined_at")
    )
    total = len(confirmed)
    connected = sum(1 for person in confirmed if str(person.pk) in online)
    disconnected = sum(
        1 for person in confirmed if person.joined_at and str(person.pk) not in online
    )

    def _percent(count: int) -> str:
        return f"{(count * 100 / total):.1f}%" if total else "0.0%"

    states = {
        "on_date": 0,
        "holding": 0,
        "recovery": 0,
        "waiting_next_round": 0,
        "looking_for_date": 0,
    }
    pair_states: dict[str, str] = {}
    seen: set[str] = set()
    live = Pair.objects.filter(
        event=event,
        status__in=(PairStatus.PENDING, PairStatus.ACTIVE),
    ).select_related("round")
    for pair in live:
        end = _pair_deadline(pair)
        if end <= current:
            continue
        a_id, b_id = str(pair.participant_a_id), str(pair.participant_b_id)
        missing = a_id not in online or b_id not in online
        if pair.recovery:
            label, kind = "6-minute recovery date", "recovery"
        elif missing and _both_connected(pair):
            started = None
            try:
                started = cache.get(_absence_key(pair.pk))
            except Exception:
                logger.exception("Unable to read hold time for %s", pair.pk)
            remaining = int(_RECONNECT_GRACE.total_seconds())
            if started is not None:
                remaining = max(
                    0,
                    int(_RECONNECT_GRACE.total_seconds() - (current.timestamp() - float(started))),
                )
            label, kind = f"Holding for reconnect ({remaining}s left)", "holding"
        else:
            label, kind = "On a date", "on_date"
        pair_states[str(pair.pk)] = label
        for pid in (a_id, b_id):
            if pid not in seen:
                states[kind] += 1
                seen.add(pid)
    for pid in online:
        if pid in seen:
            continue
        if _is_deferred(pid, current):
            states["waiting_next_round"] += 1
        elif _wants_full(pid):
            states["looking_for_date"] += 1
    return {
        "connected_count": connected,
        "connected_percent": _percent(connected),
        "disconnected_count": disconnected,
        "disconnected_percent": _percent(disconnected),
        "confirmed_count": total,
        "states": states,
        "pair_states": pair_states,
    }
