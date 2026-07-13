"""Command line entry points for SynapticLathe."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from synapse.runner import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synaptic-server", description="Run a SynapticLathe server")
    parser.add_argument("config", nargs="?", default="config.yaml", help="Path to config.yaml")
    return parser


def server_cli(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        print("Run `synaptic-setup` or copy config.example.yaml to config.yaml and edit it.", file=sys.stderr)
        raise SystemExit(1)
    run(str(config_path))


if __name__ == "__main__":
    server_cli()
