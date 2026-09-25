"""Staging/local CLI for the T-44 message-layer load test.

Not part of the production WebSocket path. See docs/message-layer-load-test.md.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tools.message_layer_load_test.cli import (
    add_arguments,
    execute,
    options_from_namespace,
)
from tools.message_layer_load_test.provision import ProvisionError


class Command(BaseCommand):
    help = (
        "T-44: connect N synthetic T-14 clients to the message layer, run one "
        "T-24 round transition, and write latency/loss JSON. Staging/local only."
    )

    def add_arguments(self, parser):
        add_arguments(parser)

    def handle(self, *args, **options):
        try:
            code = execute(options_from_namespace(options))
        except ProvisionError as exc:
            raise CommandError(str(exc)) from exc
        if code:
            raise CommandError(f"Load test exited with status {code}.")
