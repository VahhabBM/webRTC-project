from django.contrib import admin

from .models import Event, Pair, Participant, ParticipantTag, Round, Tag

for model in (Event, Tag, ParticipantTag, Round, Pair):
    admin.site.register(model)


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
