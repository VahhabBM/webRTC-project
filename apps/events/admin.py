from django.contrib import admin

from .models import Event, Pair, Participant, ParticipantTag, Round, Tag

for model in (Event, Tag, Participant, ParticipantTag, Round, Pair):
    admin.site.register(model)


class ParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "display_name",
        "event",
        "email",
        "status",
        "token_status",
        "join_token_expires_at",
    )
    readonly_fields = ("join_token_hash", "join_token_digest", "join_token_expires_at")

    @admin.display(description="Token")
    def token_status(self, obj):
        return "Configured" if obj.join_token_digest else "Missing"


admin.site.unregister(Participant)
admin.site.register(Participant, ParticipantAdmin)
