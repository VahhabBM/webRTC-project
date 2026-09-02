from django.contrib import admin

from .models import Event, Pair, Participant, ParticipantTag, Round, Tag

for model in (Event, Tag, Participant, ParticipantTag, Round, Pair):
    admin.site.register(model)
