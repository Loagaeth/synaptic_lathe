"""SynapticLathe compatibility entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from synapse.runner import main, run

__all__ = ["main", "run"]


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    if not Path(config_path).exists():
        print(f"Config file not found: {config_path}")
        print("Run `python -m synapse.setup_wizard` or copy config.example.yaml to config.yaml and edit it.")
        sys.exit(1)
    run(config_path)
