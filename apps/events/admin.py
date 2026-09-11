from __future__ import annotations

import csv

from django.contrib import admin, messages
from django.db.models import Count
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from .models import Event, EventStatus, Pair, Participant, ParticipantTag, Round, Tag

for model in (ParticipantTag, Round, Pair):
    admin.site.register(model)

# States in which the matching report action is permitted.
_MATCHING_RUNNABLE_STATES = frozenset(
    {EventStatus.SCHEDULED, EventStatus.ACTIVE, EventStatus.COMPLETED}
)


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

    fields = (
        "name",
        "description",
        "status",
        "start_time",
        "num_rounds",
        "round_duration",
        "break_duration",
    )

    # Override the change-form template to inject the "Run Matching Report" button.
    change_form_template = "admin/events/event/change_form.html"

    # ------------------------------------------------------------------ #
    # Queryset / display helpers                                           #
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
    # Custom URL: matching report page                                     #
    # ------------------------------------------------------------------ #

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<uuid:object_id>/matching-report/",
                self.admin_site.admin_view(self.matching_report_view),
                name="events_event_matching_report",
            ),
        ]
        return custom + urls

    # ------------------------------------------------------------------ #
    # Inject matching-report URL into the change-form context              #
    # ------------------------------------------------------------------ #

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            extra_context["matching_report_url"] = reverse(
                "admin:events_event_matching_report", args=[object_id]
            )
        return super().changeform_view(request, object_id, form_url, extra_context)

    # ------------------------------------------------------------------ #
    # Matching report view                                                 #
    # ------------------------------------------------------------------ #

    def matching_report_view(self, request, object_id):
        """Admin action page: generate a T-20 schedule and display a quality report.

        GET  – show the run-matching form (disabled when state is wrong).
        POST action=run  – generate in-memory, display report; require confirmation
                           if a schedule already exists in the DB.
        POST action=lock – generate again (deterministic) and persist to DB if
                           violations == 0; disabled (returns error) otherwise.
        """
        from django.shortcuts import get_object_or_404

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
                            schedule, pids, num_rounds=event.num_rounds
                        )
                        report = build_report(schedule, violations)

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
