from __future__ import annotations

import csv
import hashlib
import re
import secrets
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from apps.registration.services.email_service import EmailService

from .auth import scheduled_event_end
from .countries import COUNTRY_NAMES
from .models import (
    DisconnectionCause,
    DisconnectionLog,
    Event,
    EventRegistrationStatsSnapshot,
    EventStatus,
    OperatorActionLog,
    Pair,
    Participant,
    ParticipantIncidentNote,
    ParticipantStatus,
    ParticipantTag,
    RegistrationStatus,
    Round,
    Tag,
    WaitlistInvitation,
)

for model in (ParticipantTag, Round, Pair):
    admin.site.register(model)


@admin.register(DisconnectionLog)
class DisconnectionLogAdmin(admin.ModelAdmin):
    list_display = (
        "participant",
        "event",
        "cause",
        "disconnected_at",
        "reconnected_at",
    )
    list_filter = ("cause", "disconnected_at", "event")
    search_fields = ("participant__display_name", "participant__email")


@admin.register(ParticipantIncidentNote)
class ParticipantIncidentNoteAdmin(admin.ModelAdmin):
    list_display = ("participant", "operator", "event", "created_at", "short_note")
    search_fields = ("participant__display_name", "participant__email", "note")
    list_filter = ("created_at", "event")

    def short_note(self, obj):
        return obj.note[:60]


# States in which the matching report action is permitted.
_MATCHING_RUNNABLE_STATES = frozenset(
    {EventStatus.SCHEDULED, EventStatus.ACTIVE, EventStatus.COMPLETED}
)


class EventAdminForm(forms.ModelForm):
    """ModelForm for Event that validates the unique_tags toggle.

    Prevents enabling unique_tags on an existing event when one or more of its
    participants already have more than one tag selected.  Silently modifying
    existing participant data is intentionally avoided; the admin must resolve
    the conflict manually before enabling the restriction.
    """

    class Meta:
        model = Event
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        # Only validate when the instance already exists in the DB
        # (i.e. we are editing, not creating).
        if not self.instance.pk:
            return cleaned

        enabling_unique = cleaned.get("unique_tags", False)
        was_unique = (
            Event.objects.filter(pk=self.instance.pk)
            .values_list("unique_tags", flat=True)
            .first()
        )

        # Only block when the admin is turning the flag ON for the first time.
        if enabling_unique and not was_unique:
            # Find participants in this event that have more than one tag.
            multi_tag_participants = (
                ParticipantTag.objects.filter(participant__event=self.instance)
                .values("participant_id")
                .annotate(tag_count=Count("id"))
                .filter(tag_count__gt=1)
            )
            if multi_tag_participants.exists():
                count = multi_tag_participants.count()
                raise ValidationError(
                    f"Cannot enable 'Unique tag selection': {count} participant(s) in "
                    f"this event already have more than one tag selected. "
                    f"Remove the extra tags from those participants first, "
                    f"then enable this setting."
                )
        return cleaned


class RegistrationInvitationForm(forms.Form):
    recipients = forms.CharField(
        label="Recipient email addresses",
        widget=forms.Textarea(attrs={"rows": 6, "cols": 80}),
        help_text="Enter one email address per line, or separate addresses with commas.",
    )

    def clean_recipients(self):
        raw_value = self.cleaned_data["recipients"]
        addresses = []
        seen = set()
        validator = EmailValidator()
        for address in re.split(r"[,;\s]+", raw_value):
            address = address.strip()
            if not address or address.casefold() in seen:
                continue
            try:
                validator(address)
            except ValidationError as exc:
                raise forms.ValidationError(
                    "Enter valid email addresses; one or more entries are invalid."
                ) from exc
            seen.add(address.casefold())
            addresses.append(address)

        if not addresses:
            raise forms.ValidationError("Enter at least one email address.")
        if len(addresses) > 50:
            raise forms.ValidationError("You can invite up to 50 people at a time.")
        return addresses


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "status",
        "start_time",
        "num_rounds",
        "round_duration",
        "break_duration",
        "participants_count",
        "created_at",
    )
    list_editable = ("status",)
    list_filter = ("status", "start_time")
    search_fields = ("name", "description")
    ordering = ("-start_time",)

    form = EventAdminForm

    fields = (
        "name",
        "description",
        "status",
        "start_time",
        "num_rounds",
        "round_duration",
        "break_duration",
        "male_date_limit",
        "female_date_limit",
        "other_date_limit",
        "intro_video_url",
        "intro_video_duration_seconds",
        "unique_tags",
    )

    # Override the change-form template to inject custom tool buttons.
    change_form_template = "admin/events/event/change_form.html"
    change_list_template = "admin/events/event/change_list.html"

    # ------------------------------------------------------------------ #
    # Queryset / display helpers                                         #
    # ------------------------------------------------------------------ #

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(participants_count=Count("participants", distinct=True))
        )

    @admin.display(description="Participants")
    def participants_count(self, obj):
        return obj.participants_count

    # ------------------------------------------------------------------ #
    # Custom URLs: matching report & live monitoring                     #
    # ------------------------------------------------------------------ #

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "operations-dashboard/",
                self.admin_site.admin_view(self.operations_dashboard_view),
                name="events_event_operations_dashboard",
            ),
            path(
                "<uuid:object_id>/matching-report/",
                self.admin_site.admin_view(self.matching_report_view),
                name="events_event_matching_report",
            ),
            path(
                "<uuid:object_id>/live-monitoring/",
                self.admin_site.admin_view(self.operator_monitoring_view),
                name="events_event_live_monitoring",
            ),
            path(
                "<uuid:object_id>/registration-stats/",
                self.admin_site.admin_view(self.registration_stats_view),
                name="events_event_registration_stats",
            ),
            path(
                "<uuid:object_id>/waitlist/",
                self.admin_site.admin_view(self.waitlist_view),
                name="events_event_waitlist",
            ),
            path(
                "<uuid:object_id>/registration-invitations/",
                self.admin_site.admin_view(self.registration_invitation_view),
                name="events_event_registration_invitations",
            ),
            path(
                "<uuid:object_id>/waitlist/<uuid:participant_id>/invite/",
                self.admin_site.admin_view(self.invite_waitlisted_participant),
                name="events_event_waitlist_invite",
            ),
        ]
        return custom + urls

    # ------------------------------------------------------------------ #
    # Inject action URLs into the change-form context                    #
    # ------------------------------------------------------------------ #

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["operations_dashboard_url"] = reverse(
            "admin:events_event_operations_dashboard"
        )
        if object_id:
            extra_context["matching_report_url"] = reverse(
                "admin:events_event_matching_report", args=[object_id]
            )
            extra_context["live_monitoring_url"] = reverse(
                "admin:events_event_live_monitoring", args=[object_id]
            )
            extra_context["registration_stats_url"] = reverse(
                "admin:events_event_registration_stats", args=[object_id]
            )
            extra_context["waitlist_url"] = reverse(
                "admin:events_event_waitlist", args=[object_id]
            )
            extra_context["registration_invitations_url"] = reverse(
                "admin:events_event_registration_invitations", args=[object_id]
            )
        return super().changeform_view(request, object_id, form_url, extra_context)

    def operations_dashboard_view(self, request):
        """Show staff a live, event-level overview and its operational tools."""
        if not request.user.has_perm("events.view_event"):
            return HttpResponseForbidden(
                "You do not have permission to view the event dashboard."
            )

        now = timezone.now()
        annotated_events = Event.objects.select_related(
            "registration_stats_snapshot"
        ).annotate(
            registrations_count=Count("participants", distinct=True),
            confirmed_count=Count(
                "participants",
                filter=Q(
                    participants__registration_status=RegistrationStatus.CONFIRMED
                ),
                distinct=True,
            ),
            waitlisted_count=Count(
                "participants",
                filter=Q(
                    participants__registration_status=RegistrationStatus.WAITLISTED
                ),
                distinct=True,
            ),
        )
        live_statuses = (
            EventStatus.ACTIVE,
            EventStatus.RUNNING,
            EventStatus.PAUSED,
        )
        upcoming_statuses = (
            EventStatus.REGISTRATION_OPEN,
            EventStatus.SCHEDULED,
            EventStatus.LOCKED,
        )
        active_events = list(
            annotated_events.filter(status__in=live_statuses).order_by("start_time")
        )
        upcoming_events = list(
            annotated_events.filter(
                status__in=upcoming_statuses, start_time__gte=now
            ).order_by("start_time")[:25]
        )
        recent_events = list(
            annotated_events.filter(
                status__in=(EventStatus.COMPLETED, EventStatus.CANCELLED)
            ).order_by("-start_time")[:10]
        )
        overview_statuses = (*live_statuses, *upcoming_statuses)

        context = {
            **self.admin_site.each_context(request),
            "title": "Operations Dashboard",
            "opts": self.model._meta,
            "now": now,
            "active_events": active_events,
            "upcoming_events": upcoming_events,
            "recent_events": recent_events,
            "active_count": Event.objects.filter(status__in=live_statuses).count(),
            "upcoming_count": Event.objects.filter(
                status__in=upcoming_statuses, start_time__gte=now
            ).count(),
            "can_change_events": self.has_change_permission(request),
            "confirmed_count": Participant.objects.filter(
                event__status__in=overview_statuses,
                registration_status=RegistrationStatus.CONFIRMED,
            ).count(),
            "waitlisted_count": Participant.objects.filter(
                event__status__in=overview_statuses,
                registration_status=RegistrationStatus.WAITLISTED,
            ).count(),
            "event_changelist_url": reverse("admin:events_event_changelist"),
        }
        return TemplateResponse(
            request, "admin/events/event/operations_dashboard.html", context
        )

    # ------------------------------------------------------------------ #
    # Live monitoring view (T-40 & T-41)                                 #
    # ------------------------------------------------------------------ #

    def operator_monitoring_view(self, request, object_id):
        """Admin action page: T-40/T-41 Live Monitoring, Incidents & Operator Controls."""
        from apps.events.operator import OperatorService

        event = get_object_or_404(Event, pk=object_id)
        service = OperatorService(event, is_leader=True)

        if request.method == "POST":
            action = request.POST.get("action")
            try:
                if action == "add_incident_note":
                    p_id = request.POST.get("participant_id")
                    note_text = request.POST.get("note", "").strip()
                    if p_id and note_text:
                        participant = get_object_or_404(
                            Participant, pk=p_id, event=event
                        )
                        ParticipantIncidentNote.objects.create(
                            event=event,
                            participant=participant,
                            operator=request.user
                            if request.user.is_authenticated
                            else None,
                            note=note_text,
                        )
                        messages.success(
                            request,
                            f"یادداشت رخداد برای {participant.display_name} ذخیره شد.",
                        )
                elif action == "pause":
                    service.pause()
                    messages.success(request, "Round successfully paused.")
                elif action == "resume":
                    service.resume()
                    messages.success(request, "Round successfully resumed.")
                elif action == "extend":
                    seconds = int(request.POST.get("seconds", 60))
                    service.extend(extra_seconds=seconds)
                    messages.success(request, f"Round extended by {seconds} seconds.")
            except Exception as exc:  # noqa: BLE001
                messages.error(request, f"Operator action failed: {exc}")
            return redirect("admin:events_event_live_monitoring", object_id=event.pk)

        # T-41: جستجوی شرکت‌کننده بر اساس نام یا ایمیل
        search_query = request.GET.get("q", "").strip()
        search_results = []
        if search_query:
            search_results = (
                event.participants.filter(
                    Q(display_name__icontains=search_query)
                    | Q(email__icontains=search_query)
                )
                .prefetch_related("disconnection_logs", "incident_notes")
                .distinct()
            )

        # T-41: فهرست شرکت‌کنندگان قطع‌شده زنده
        disconnected_participants = (
            event.participants.filter(status=ParticipantStatus.DISCONNECTED)
            .prefetch_related("disconnection_logs", "incident_notes")
            .order_by("-updated_at")
        )

        now = timezone.now()
        active_round = (
            event.rounds.filter(starts_at__lte=now, ends_at__gte=now).first()
            or event.rounds.filter(starts_at__gt=now).order_by("starts_at").first()
        )

        action_logs = (
            OperatorActionLog.objects.filter(event=event)
            .select_related("round")
            .order_by("-performed_at")[:20]
        )
        pairs = []
        if active_round:
            pairs = active_round.pairs.select_related(
                "participant_a", "participant_b"
            ).all()

        context = {
            **self.admin_site.each_context(request),
            "title": f"Live Monitoring \u2013 {event.name}",
            "opts": self.model._meta,
            "app_label": self.model._meta.app_label,
            "event": event,
            "active_round": active_round,
            "action_logs": action_logs,
            "pairs": pairs,
            "disconnected_participants": disconnected_participants,
            "search_query": search_query,
            "search_results": search_results,
            "incident_causes": DisconnectionCause.choices,
        }
        return TemplateResponse(
            request,
            "admin/events/event/live_monitoring.html",
            context,
        )

    def registration_stats_view(self, request, object_id):
        event = get_object_or_404(Event, pk=object_id)
        if not self.has_view_permission(request, event):
            return HttpResponseForbidden(
                "You do not have permission to view registration statistics."
            )

        snapshot = EventRegistrationStatsSnapshot.objects.filter(event=event).first()
        total = snapshot.total_registrations if snapshot else event.participants.count()
        statistics_unavailable = (
            snapshot is None
            and total == 0
            and scheduled_event_end(event) <= timezone.now() - timedelta(days=2)
        )

        def rows_from_counts(counts, label_for):
            result = []
            for value, count in sorted(counts.items(), key=lambda item: item[0] or ""):
                label = label_for(value)
                result.append(
                    {
                        "label": label,
                        "count": count,
                        "percent": f"{(count * 100 / total):.1f}%" if total else "0.0%",
                    }
                )
            return result

        if snapshot:
            gender_counts = snapshot.gender_counts
            country_counts = snapshot.country_counts
        elif statistics_unavailable:
            gender_counts = {}
            country_counts = {}
        else:
            gender_counts = dict(
                event.participants.values("gender")
                .annotate(count=Count("id"))
                .values_list("gender", "count")
            )
            country_counts = dict(
                event.participants.values("country")
                .annotate(count=Count("id"))
                .values_list("country", "count")
            )

        gender_rows = rows_from_counts(
            gender_counts,
            lambda value: (
                "Other (small groups)"
                if value == "__other__"
                else value or "Not provided"
            ),
        )
        country_rows = rows_from_counts(
            country_counts,
            lambda value: (
                "Other (small groups)"
                if value == "__other__"
                else COUNTRY_NAMES.get(value or "", value or "Not provided")
            ),
        )
        return TemplateResponse(
            request,
            "admin/events/event/registration_stats.html",
            {
                **self.admin_site.each_context(request),
                "title": f"Registration statistics – {event.name}",
                "event": event,
                "total": total,
                "gender_rows": gender_rows,
                "country_rows": country_rows,
                "statistics_unavailable": statistics_unavailable,
                "statistics_retained": snapshot is not None,
                "opts": self.model._meta,
                "app_label": self.model._meta.app_label,
            },
        )

    def waitlist_view(self, request, object_id):
        event = get_object_or_404(Event, pk=object_id)
        if not self.has_view_permission(request, event):
            return HttpResponseForbidden(
                "You do not have permission to view the waitlist."
            )
        eligible = (
            event.participants.filter(
                registration_status=RegistrationStatus.WAITLISTED,
                gender="Female",
                email_verified_at__isnull=False,
            )
            .exclude(email="")
            .order_by("created_at")
        )
        return TemplateResponse(
            request,
            "admin/events/event/waitlist.html",
            {
                **self.admin_site.each_context(request),
                "title": f"Waitlist – {event.name}",
                "event": event,
                "participants": eligible,
                "can_invite": self.has_change_permission(request, event),
                "opts": self.model._meta,
                "app_label": self.model._meta.app_label,
            },
        )

    def registration_invitation_view(self, request, object_id):
        event = get_object_or_404(Event, pk=object_id)
        if not self.has_view_permission(request, event):
            return HttpResponseForbidden(
                "You do not have permission to view registration invitations."
            )

        registration_url = request.build_absolute_uri(
            reverse("registration:register-ui", args=[event.pk])
        )
        can_send = self.has_change_permission(request, event) and event.status in {
            EventStatus.REGISTRATION_OPEN,
            EventStatus.SCHEDULED,
        }
        form = RegistrationInvitationForm(
            request.POST if request.method == "POST" else None
        )
        sent_count = 0
        failed_recipients = []

        if request.method == "POST":
            if not self.has_change_permission(request, event):
                return HttpResponseForbidden(
                    "You do not have permission to send registration invitations."
                )
            if not can_send:
                self.message_user(
                    request,
                    "Registration invitations can only be sent for scheduled or open events.",
                    level=messages.ERROR,
                )
            elif form.is_valid():
                for recipient_email in form.cleaned_data["recipients"]:
                    try:
                        EmailService.send_registration_invitation_email(
                            recipient_email=recipient_email,
                            event_title=event.name,
                            registration_url=registration_url,
                        )
                    except Exception:
                        failed_recipients.append(recipient_email)
                    else:
                        sent_count += 1

                if sent_count:
                    self.message_user(
                        request,
                        f"Registration link sent to {sent_count} recipient(s).",
                        level=messages.SUCCESS,
                    )
                if failed_recipients:
                    self.message_user(
                        request,
                        f"Could not send {len(failed_recipients)} invitation email(s). "
                        "Only failed addresses remain in the form for retry.",
                        level=messages.ERROR,
                    )
                    form = RegistrationInvitationForm(
                        initial={"recipients": "\n".join(failed_recipients)}
                    )
                else:
                    form = RegistrationInvitationForm()

        return TemplateResponse(
            request,
            "admin/events/event/registration_invitations.html",
            {
                **self.admin_site.each_context(request),
                "title": f"Registration invitations – {event.name}",
                "event": event,
                "registration_url": registration_url,
                "can_send": can_send,
                "form": form,
                "opts": self.model._meta,
                "app_label": self.model._meta.app_label,
            },
        )

    def invite_waitlisted_participant(self, request, object_id, participant_id):
        if request.method != "POST":
            return HttpResponseForbidden("Invitations must be sent using POST.")
        event = get_object_or_404(Event, pk=object_id)
        if not self.has_change_permission(request, event):
            return HttpResponseForbidden(
                "You do not have permission to send invitations."
            )

        now = timezone.now()
        with transaction.atomic():
            locked_event = Event.objects.select_for_update().get(pk=event.pk)
            participant = get_object_or_404(
                Participant.objects.select_for_update(),
                pk=participant_id,
                event=locked_event,
                gender="Female",
                registration_status=RegistrationStatus.WAITLISTED,
                email_verified_at__isnull=False,
            )
            if not participant.email:
                raise Http404
            WaitlistInvitation.objects.filter(
                participant=participant,
                accepted_at__isnull=True,
                revoked_at__isnull=True,
            ).update(revoked_at=now)
            token = secrets.token_urlsafe(32)
            invitation = WaitlistInvitation.objects.create(
                event=locked_event,
                participant=participant,
                token_digest=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                created_by=request.user,
                expires_at=now
                + timedelta(
                    hours=getattr(settings, "WAITLIST_INVITATION_TTL_HOURS", 48)
                ),
            )
            invitation_url = request.build_absolute_uri(
                reverse("registration:accept-waitlist-invitation", args=[token])
            )
            email_args = {
                "recipient_email": participant.email,
                "name": participant.display_name,
                "event_title": locked_event.name,
                "message": (
                    f"A place is available for {locked_event.name}. Accept your "
                    f"invitation within {getattr(settings, 'WAITLIST_INVITATION_TTL_HOURS', 48)} hours "
                    f"using this personal link:\n\n{invitation_url}\n\n"
                    "If the remaining place is taken before you accept, your waitlist status will remain unchanged."
                ),
            }

        try:
            EmailService.send_waitlist_invitation_email(**email_args)
        except Exception:
            WaitlistInvitation.objects.filter(pk=invitation.pk).update(
                revoked_at=timezone.now()
            )
            self.message_user(
                request,
                "The invitation email could not be sent. The link was revoked.",
                level=messages.ERROR,
            )
        else:
            self.message_user(
                request, "Waitlist invitation sent.", level=messages.SUCCESS
            )
        return redirect("admin:events_event_waitlist", object_id)

    # ------------------------------------------------------------------ #
    # Matching report view                                               #
    # ------------------------------------------------------------------ #

    def matching_report_view(self, request, object_id):
        """Admin action page: generate a T-20 schedule and display a quality report.

        GET  – show the run-matching form (disabled when state is wrong).
        POST action=run  – generate in-memory, display report; require confirmation
                           if a schedule already exists in the DB.
        POST action=lock – generate again (deterministic) and persist to DB if
                           violations == 0; disabled (returns error) otherwise.
        """
        from apps.events.matching import (
            MatchingError,
            generate_schedule,
            validate_schedule,
        )
        from apps.events.report import build_report
        from apps.events.scheduling import build_match_input, persist_schedule

        event = get_object_or_404(Event, pk=object_id)

        can_run = event.status in _MATCHING_RUNNABLE_STATES
        has_schedule = event.rounds.exists()

        report = None
        error_message = None
        schedule_locked = False
        needs_confirmation = False

        if request.method == "POST":
            action = request.POST.get("action", "")
            confirmed = request.POST.get("confirmed") == "yes"

            if action in ("run", "lock"):
                if not can_run:
                    error_message = (
                        f"Matching is not available for events in "
                        f"'{event.get_status_display()}' state. "
                        f"The event must be Scheduled, Active, or Completed."
                    )
                elif has_schedule and not confirmed:
                    # Surface the confirmation gate; do not run yet.
                    needs_confirmation = True
                else:
                    try:
                        match_input = build_match_input(event)

                        if len(match_input.participants) < 2:
                            raise MatchingError(
                                f"Cannot run matching: event has fewer than 2 "
                                f"participants (got {len(match_input.participants)})."
                            )

                        schedule = generate_schedule(match_input)
                        pids = frozenset(mp.pid for mp in match_input.participants)
                        violations = validate_schedule(
                            schedule,
                            pids,
                            num_rounds=event.num_rounds,
                            match_input=match_input,
                        )
                        report = build_report(
                            schedule,
                            violations,
                            round_duration=event.round_duration,
                            break_duration=event.break_duration,
                        )

                        if action == "lock":
                            if report.can_lock:
                                persist_schedule(event, schedule)
                                schedule_locked = True
                                has_schedule = True
                                self.message_user(
                                    request,
                                    f"Schedule for '{event.name}' locked and persisted "
                                    f"({report.total_pairs} pairs across "
                                    f"{len(report.rounds)} rounds).",
                                    level=messages.SUCCESS,
                                )
                            else:
                                error_message = (
                                    f"Cannot lock result: "
                                    f"{report.violation_count} constraint violation(s) "
                                    f"were detected. All violations must be zero before "
                                    f"the schedule can be locked."
                                )
                    except MatchingError as exc:
                        error_message = str(exc)
                    except Exception as exc:  # noqa: BLE001
                        error_message = f"Unexpected error during matching: {exc}"

        context = {
            **self.admin_site.each_context(request),
            "title": f"Matching Report \u2013 {event.name}",
            "event": event,
            "report": report,
            "error_message": error_message,
            "can_run": can_run,
            "has_schedule": has_schedule,
            "schedule_locked": schedule_locked,
            "needs_confirmation": needs_confirmation,
            "opts": self.model._meta,
            "app_label": self.model._meta.app_label,
        }
        return TemplateResponse(
            request,
            "admin/events/event/matching_report.html",
            context,
        )


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "usage_count", "created_at")
    search_fields = ("name",)
    ordering = ("name",)

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(usage_count=Count("participants", distinct=True))
        )

    @admin.display(description="Usage Count")
    def usage_count(self, obj):
        return obj.usage_count

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.participants.exists():
            return False
        return super().has_delete_permission(request, obj)

    def delete_model(self, request, obj):
        if obj.participants.exists():
            self.message_user(
                request,
                f"Tag '{obj.name}' cannot be deleted because it is assigned to participants.",
                level=messages.ERROR,
            )
            return
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        used_tags = queryset.filter(participants__isnull=False).distinct()
        if used_tags.exists():
            names = ", ".join(t.name for t in used_tags)
            self.message_user(
                request,
                f"Deletion cancelled for assigned tags: ({names}).",
                level=messages.ERROR,
            )
            queryset = queryset.exclude(id__in=used_tags)

        if queryset.exists():
            super().delete_queryset(request, queryset)


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "country_name",
        "email",
        "status",
        "display_tags",
        "created_at",
        "event",
        "token_status",
    )
    list_editable = ("status",)
    list_filter = ("status", "tags", "event")
    search_fields = ("display_name", "email")
    list_per_page = 50
    actions = ["export_as_csv"]

    readonly_fields = (
        "join_token_hash",
        "join_token_digest",
        "join_token_expires_at",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("event")
            .prefetch_related("tags")
        )

    @admin.display(description="Tags")
    def display_tags(self, obj):
        tags = [tag.name for tag in obj.tags.all()]
        return ", ".join(tags) if tags else "-"

    @admin.display(description="Country")
    def country_name(self, obj):
        return COUNTRY_NAMES.get(obj.country, obj.country or "-")

    @admin.display(description="Token")
    def token_status(self, obj):
        return "Configured" if obj.join_token_digest else "Missing"

    @admin.action(description="Export selected participants to CSV (Excel UTF-8)")
    def export_as_csv(self, request, queryset):
        optimized_qs = queryset.select_related("event").prefetch_related("tags")

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        response["Content-Disposition"] = (
            f'attachment; filename="participants_{timestamp}.csv"'
        )
        response.write("\ufeff")

        writer = csv.writer(response)
        writer.writerow(
            [
                "\u0634\u0646\u0627\u0633\u0647",
                "\u0646\u0627\u0645 \u0648 \u0646\u0627\u0645 \u062e\u0627\u0646\u0648\u0627\u062f\u06af\u06cc",
                "\u0627\u06cc\u0645\u06cc\u0644",
                "\u0648\u0636\u0639\u06cc\u062a",
                "\u0631\u0648\u06cc\u062f\u0627\u062f",
                "\u062a\u06af\u200c\u0647\u0627",
                "\u062a\u0627\u0631\u06cc\u062e \u062b\u0628\u062a\u200c\u0646\u0627\u0645",
            ]
        )

        for p in optimized_qs:
            tags = ", ".join(t.name for t in p.tags.all())
            created = p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else ""
            writer.writerow(
                [
                    str(p.id),
                    p.display_name,
                    p.email,
                    p.get_status_display(),
                    p.event.name if p.event else "",
                    tags,
                    created,
                ]
            )

        return response
