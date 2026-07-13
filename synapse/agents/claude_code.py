"""Compatibility adapter for Claude Code.

New deployments should use ``synaptic-profile-worker``. This module delegates
to that hardened worker so reconnects, output limits, process-group cleanup,
and child environment filtering stay consistent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from synapse.agents.profile_agent import ProfileDispatcherAgent
from synapse.agents.profile_agent import main as profile_main
from synapse.agents.worker_utils import DEFAULT_CHILD_ENV_ALLOWLIST
from synapse.file_utils import atomic_write_text

DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000


def _normalized_profile_config(config: dict[str, Any]) -> dict[str, Any]:
    command = str(config.get("command") or "claude")
    workdir = str(config.get("workdir") or "")
    max_turns = max(1, int(config.get("max_turns") or 10))
    timeout = max(1, int(config.get("timeout") or DEFAULT_TIMEOUT_SECONDS))
    max_output_bytes = max(1, int(config.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES))
    return {
        "default_profile": "claude",
        "timeout": timeout,
        "max_output_bytes": max_output_bytes,
        "profiles": {
            "claude": {
                "command": [command, "-p", "{plan}", "--max-turns", str(max_turns), "--permission-mode", "plan"],
                "workdir": workdir or None,
                "timeout": timeout,
                "max_output_bytes": max_output_bytes,
                "pass_env": list(DEFAULT_CHILD_ENV_ALLOWLIST),
                "env": {},
                "sessions": {},
                "default_session": "",
                "allow_raw_session_id": False,
                "session_pattern": r"^[A-Za-z0-9._:@/-]{1,256}$",
            }
        },
    }


class ClaudeCodeAgent(ProfileDispatcherAgent):
    """Backward-compatible Claude adapter backed by the profile dispatcher."""

    def __init__(self, config: dict[str, Any]) -> None:
        delegated = dict(config)
        delegated["extra"] = {"profile_config": _normalized_profile_config(config)}
        super().__init__(delegated)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude Code SynapticLathe compatibility worker")
    parser.add_argument("--url", default="ws://127.0.0.1:9112/ws")
    parser.add_argument("--name", default="claude-code")
    parser.add_argument("--key", default=os.environ.get("SYNAPTIC_API_KEY", ""))
    parser.add_argument("--command", default="claude")
    parser.add_argument("--workdir", default="")
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_MAX_OUTPUT_BYTES)
    parser.add_argument("--no-reconnect", action="store_true")
    return parser


async def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.max_turns <= 0 or args.timeout <= 0 or args.max_output_bytes <= 0:
        raise SystemExit("max-turns, timeout, and max-output-bytes must be positive")
    profile = {
        "default_profile": "claude",
        "profiles": {
            "claude": {
                "command": [
                    args.command,
                    "-p",
                    "{plan}",
                    "--max-turns",
                    str(args.max_turns),
                    "--permission-mode",
                    "plan",
                ],
                "workdir": args.workdir,
                "timeout": args.timeout,
                "max_output_bytes": args.max_output_bytes,
            }
        },
    }
    fd, raw_path = tempfile.mkstemp(prefix="synaptic-claude-", suffix=".json")
    os.close(fd)
    profile_path = Path(raw_path)
    try:
        atomic_write_text(profile_path, json.dumps(profile), overwrite=True, mode=0o600)
        delegated_argv = [
            "--url",
            args.url,
            "--name",
            args.name,
            "--key",
            args.key,
            "--profiles",
            str(profile_path),
            "--default-profile",
            "claude",
        ]
        if args.no_reconnect:
            delegated_argv.append("--no-reconnect")
        return await profile_main(delegated_argv)
    finally:
        await asyncio.to_thread(profile_path.unlink, missing_ok=True)


def cli() -> None:
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    cli()
