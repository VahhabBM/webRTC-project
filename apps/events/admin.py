from django.contrib import admin, messages
from django.db.models import Count

from .models import Event, Pair, Participant, ParticipantTag, Round, Tag

for model in (ParticipantTag, Round, Pair):
    admin.site.register(model)


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

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(participants_count=Count("participants", distinct=True))
        )

    @admin.display(description="Participants")
    def participants_count(self, obj):
        return obj.participants_count


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
