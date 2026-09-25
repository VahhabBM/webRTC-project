"""CLI argument surface shared by manage.py and ``python -m``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from . import DEFAULT_CLIENT_COUNT, MAX_CLIENT_COUNT, MIN_CLIENT_COUNT
from .runner import default_http_base, default_ws_url, run_load_test


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--clients",
        "-n",
        type=int,
        default=DEFAULT_CLIENT_COUNT,
        help=(
            f"Synthetic WebSocket clients to connect (even, "
            f"{MIN_CLIENT_COUNT}-{MAX_CLIENT_COUNT}; default {DEFAULT_CLIENT_COUNT}). "
            "50 is the T-44 acceptance sample. Do not use 900."
        ),
    )
    parser.add_argument(
        "--http-base",
        default=None,
        help=(
            "Origin for T-08 join URLs, e.g. http://127.0.0.1:8000 or "
            "https://staging.example.com. Defaults to "
            "MESSAGE_LAYER_LOAD_TEST_HTTP_BASE or http://127.0.0.1:8000."
        ),
    )
    parser.add_argument(
        "--ws-url",
        default=None,
        help=(
            "T-14 WebSocket URL. Defaults to MESSAGE_LAYER_LOAD_TEST_WS_URL or "
            "derived from --http-base as ws(s)://.../ws/events/."
        ),
    )
    parser.add_argument(
        "--output",
        "-o",
        default="message-layer-load-test-result.json",
        help="Path for the machine-readable JSON result (secrets are stripped).",
    )
    parser.add_argument(
        "--connect-concurrency",
        type=int,
        default=10,
        help="Max simultaneous join+WebSocket handshakes (default 10).",
    )
    parser.add_argument(
        "--clock-sync-samples",
        type=int,
        default=5,
        help="client.clock_sync samples per client used for RTT (default 5).",
    )
    parser.add_argument(
        "--handshake-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for join, socket open, and server.hello (default 10).",
    )
    parser.add_argument(
        "--lifecycle-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for each T-24 lifecycle message (default 15).",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=0.4,
        help="Pause after each orchestrator broadcast (default 0.4).",
    )
    parser.add_argument(
        "--event-name",
        default=None,
        help="Optional name for the throwaway T-44 event.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete the throwaway event after sockets are closed.",
    )
    parser.add_argument(
        "--cookie-name",
        default="sessionid",
        help="Django session cookie name (default sessionid).",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="message_layer_load_test",
        description=(
            "T-44 staging/local tool: connect N synthetic T-14 clients, run one "
            "T-24 round transition (pairing → start → end → partner-switch pairing), "
            "and write latency/loss JSON. Refuses production settings."
        ),
    )
    return add_arguments(parser)


def options_from_namespace(ns: argparse.Namespace | dict[str, Any]) -> dict[str, Any]:
    data = ns if isinstance(ns, dict) else vars(ns)
    http_base = data.get("http_base") or default_http_base()
    return {
        "clients": data["clients"],
        "http_base": http_base,
        "ws_url": data.get("ws_url") or default_ws_url(http_base),
        "output": data.get("output") or "message-layer-load-test-result.json",
        "connect_concurrency": data.get("connect_concurrency") or 10,
        "clock_sync_samples": data.get("clock_sync_samples", 5),
        "handshake_timeout": data.get("handshake_timeout") or 10.0,
        "lifecycle_timeout": data.get("lifecycle_timeout") or 15.0,
        "settle_seconds": data.get("settle_seconds", 0.4),
        "cleanup": bool(data.get("cleanup")),
        "event_name": data.get("event_name"),
        "cookie_name": data.get("cookie_name") or "sessionid",
    }


def print_summary(result: dict[str, Any]) -> None:
    messages = result.get("messages") or {}
    latency = result.get("latency_ms") or {}
    clients = result.get("clients") or {}
    config = result.get("config") or {}
    lines = [
        "T-44 message-layer load test finished.",
        f"  clients: {clients.get('handshake_ok')}/{clients.get('attempted')} handshake ok "
        f"({clients.get('failed')} failed, isolated)",
        f"  event_id: {config.get('event_id')}",
        f"  sent={messages.get('sent')} expected={messages.get('expected')} "
        f"received={messages.get('received')} lost={messages.get('lost')} "
        f"loss_rate={messages.get('loss_rate')}",
        f"  latency_ms: p50={latency.get('p50')} p95={latency.get('p95')} "
        f"mean={latency.get('mean')} n={latency.get('sample_count')}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")


async def _async_main(options: dict[str, Any]) -> int:
    result = await run_load_test(**options)
    print_summary(result)
    sys.stdout.write(json.dumps({"output": options["output"]}, indent=2) + "\n")
    return 0


def execute(options: dict[str, Any]) -> int:
    try:
        return asyncio.run(_async_main(options))
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted.\n")
        return 130
    except Exception as exc:
        sys.stderr.write(f"Load test failed: {exc}\n")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    return execute(options_from_namespace(ns))
