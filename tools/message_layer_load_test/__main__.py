"""``python -m tools.message_layer_load_test`` entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
    import django

    django.setup()
    from tools.message_layer_load_test.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
