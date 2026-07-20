"""Profile dispatcher worker.

This worker lets a local machine expose a small allowlist of command profiles
to SynapticLathe. The server routes tasks; only this local config decides which
executables and fixed arguments are available.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from string import Formatter
from typing import Any

import yaml

from synapse.agents.base import BaseAgent
from synapse.agents.worker_utils import (
    WORKER_MAX_OUTPUT_BYTES,
    WORKER_MAX_TIMEOUT_SECONDS,
    WORKER_WS_MAX_MESSAGE_BYTES,
    WORKER_WS_MAX_QUEUE,
    LimitedByteBuffer,
    bounded_task_timeout,
    build_child_env,
    child_process_group_id,
    child_process_group_kwargs,
    default_worker_lock_file,
    print_registration_banner,
    receive_registration_ack,
    run_worker_message_loop,
    sanitize_process_text,
    terminate_child_process,
    validate_child_env_names,
    validate_websocket_url,
    validate_worker_agent_name,
    wait_for_instance_lock,
    websocket_headers_kwargs,
    worker_registration_payload,
)
from synapse.logging import synapse_logger

DEFAULT_WS_URL = "ws://127.0.0.1:9112/ws"
DEFAULT_AGENT_NAME = "profile-dispatcher"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
DEFAULT_SESSION_PATTERN = r"^[A-Za-z0-9._:@/-]{1,256}$"
READ_CHUNK_BYTES = 8192

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROFILE_TAG_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,32}$")
_PLACEHOLDER_NAMES = {"plan", "profile", "tool", "session_id", "session_alias", "source"}
_PLAN_JSON_SELECTOR_KEYS = {"profile", "tool", "session_id", "session", "ssid"}
_MAX_PROFILES = 64
_MAX_PROFILE_CONFIG_BYTES = 1_048_576
_MAX_SESSION_SELECTOR_CHARS = 4096


class ProfileConfigError(ValueError):
    """Raised when a local profile configuration is invalid."""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _bounded_config_int(value: Any, label: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ProfileConfigError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProfileConfigError(f"{label} must be an integer") from exc
    if not 1 <= number <= maximum:
        raise ProfileConfigError(f"{label} must be between 1 and {maximum}")
    return number


def _validate_session_pattern(value: Any, label: str) -> str:
    pattern = str(value or DEFAULT_SESSION_PATTERN)
    if len(pattern) > 256 or any(token in pattern for token in ("(", ")", "|", "*", "+", "?")):
        raise ProfileConfigError(f"{label} must be a bounded character-class pattern")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ProfileConfigError(f"{label} is invalid: {exc}") from exc
    return pattern


def _normalize_dir(value: str, label: str) -> str:
    if not value:
        return ""
    path = Path(value).expanduser()
    if not path.is_dir():
        raise ProfileConfigError(f"{label} does not exist or is not a directory: {value}")
    return str(path.resolve())


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileConfigError(f"{label} must be a mapping")
    return value


def _normalize_command(value: Any, label: str) -> list[str]:
    if isinstance(value, str):
        try:
            result = shlex.split(value, posix=os.name != "nt")
        except ValueError as exc:
            raise ProfileConfigError(f"{label} is not a valid command string: {exc}") from exc
    elif isinstance(value, list):
        result = [str(item) for item in value]
    else:
        raise ProfileConfigError(f"{label} must be a string or list")
    if not result:
        raise ProfileConfigError(f"{label} must not be empty")
    if len(result) > 128 or any(len(item) > 8192 or "\0" in item for item in result):
        raise ProfileConfigError(f"{label} has too many or oversized command arguments")
    return result


def _normalize_string_list(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ProfileConfigError(f"{label} must be a list")
    return validate_child_env_names(str(item) for item in value)


def _normalize_profile_tags(value: Any, label: str) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 8:
        raise ProfileConfigError(f"{label} must be a list with at most 8 tags")
    result = []
    for raw_tag in value:
        tag = str(raw_tag)
        if not _PROFILE_TAG_RE.fullmatch(tag):
            raise ProfileConfigError(f"{label} contains invalid tag: {tag!r}")
        if tag not in result:
            result.append(tag)
    return result


def _normalize_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileConfigError(f"{label} must be a boolean")
    return value


def _normalize_env(value: Any, label: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ProfileConfigError(f"{label} must be a mapping")
    env = {}
    for raw_name, raw_value in value.items():
        name = validate_child_env_names([str(raw_name)])[0]
        if raw_value is not None:
            env[name] = str(raw_value)
    return env


def _normalize_sessions(value: Any, label: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ProfileConfigError(f"{label} must be a mapping")
    sessions = {}
    for raw_alias, raw_session_id in value.items():
        alias = str(raw_alias)
        if not _PROFILE_NAME_RE.fullmatch(alias):
            raise ProfileConfigError(f"{label} has invalid session alias: {alias!r}")
        session_id = str(raw_session_id)
        if not session_id or len(session_id) > 4096 or "\0" in session_id:
            raise ProfileConfigError(f"{label}.{alias} must be 1-4096 characters without NUL")
        sessions[alias] = session_id
    return sessions


def _placeholder_names(args: Sequence[str]) -> set[str]:
    names: set[str] = set()
    formatter = Formatter()
    for arg in args:
        for _, field_name, _, _ in formatter.parse(arg):
            if field_name:
                names.add(field_name)
    return names


def _validate_placeholders(args: Sequence[str], profile_name: str) -> None:
    try:
        unknown = _placeholder_names(args) - _PLACEHOLDER_NAMES
    except ValueError as exc:
        raise ProfileConfigError(f"profile {profile_name!r} has malformed command placeholders") from exc
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ProfileConfigError(f"profile {profile_name!r} has unknown command placeholder(s): {names}")
    formatter = Formatter()
    for arg in args:
        for _, field_name, format_spec, conversion in formatter.parse(arg):
            if field_name and (format_spec or conversion):
                raise ProfileConfigError(
                    f"profile {profile_name!r} placeholders do not allow conversion or format specifications"
                )


def describe_profile_capabilities(profile_config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return non-secret profile metadata for server-side discovery.

    The output intentionally excludes command argv, workdir, env values, and real
    session ids. It is safe to expose through /context/agents.
    """
    default_timeout = int(profile_config.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    default_max_output_bytes = int(profile_config.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES)
    profiles = profile_config.get("profiles", {}) or {}
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(profiles):
        profile = profiles[name]
        command = [str(arg) for arg in profile.get("command", [])]
        placeholders = _placeholder_names(command)
        sessions = profile.get("sessions", {}) or {}
        timeout = int(profile.get("timeout") or default_timeout)
        max_output_bytes = int(profile.get("max_output_bytes") or default_max_output_bytes)
        default_session = str(profile.get("default_session") or "")
        public_default_session = default_session if default_session in sessions else ""
        allow_raw_session_id = bool(profile.get("allow_raw_session_id", False))
        supports_session = bool(
            sessions
            or default_session
            or allow_raw_session_id
            or {"session_id", "session_alias"}.intersection(placeholders)
        )
        session_required = bool({"session_id", "session_alias"}.intersection(placeholders)) and not default_session
        hints = []
        if name.lower() == "reasonix":
            hints.append("may_initialize_mcp")
            hints.append("avoid_short_timeout")
        if allow_raw_session_id:
            hints.append("raw_session_id_allowed")
        result[name] = {
            "name": name,
            "timeout": timeout,
            "suggested_timeout": timeout,
            "max_output_bytes": max_output_bytes,
            "supports_session": supports_session,
            "session_required": session_required,
            "session_aliases": sorted(str(alias) for alias in sessions),
            "default_session_alias": public_default_session,
            "allow_raw_session_id": allow_raw_session_id,
            "plan_delivery": "placeholder" if "plan" in placeholders else "argv_tail",
            "hints": hints,
            "tags": list(profile.get("tags", [])),
            "advisory_safe": bool(profile.get("advisory_safe", False)),
        }
    return result


def _load_raw_config(path: str) -> dict[str, Any]:
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise ProfileConfigError(f"profile config file not found: {path}")
    if config_path.stat().st_size > _MAX_PROFILE_CONFIG_BYTES:
        raise ProfileConfigError("profile config file is too large")
    with config_path.open("r", encoding="utf-8") as fh:
        if config_path.suffix.lower() == ".json":
            data = json.load(fh)
        else:
            data = yaml.safe_load(fh)
    if data is None:
        data = {}
    return _require_mapping(data, "profile config")


def load_profile_config(path: str, *, global_workdir: str = "", global_pass_env: Sequence[str] = ()) -> dict[str, Any]:
    """Load and validate a local profile dispatcher config."""
    raw = _load_raw_config(path)
    profiles_raw = _require_mapping(raw.get("profiles"), "profiles")
    if not profiles_raw:
        raise ProfileConfigError("profiles must not be empty")
    if len(profiles_raw) > _MAX_PROFILES:
        raise ProfileConfigError(f"profiles may contain at most {_MAX_PROFILES} entries")

    default_timeout = _bounded_config_int(
        raw.get("timeout", DEFAULT_TIMEOUT_SECONDS),
        "timeout",
        maximum=WORKER_MAX_TIMEOUT_SECONDS,
    )
    default_max_output_bytes = _bounded_config_int(
        raw.get("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES),
        "max_output_bytes",
        maximum=WORKER_MAX_OUTPUT_BYTES,
    )

    profiles = {}
    for profile_name, raw_profile in profiles_raw.items():
        name = str(profile_name)
        if not _PROFILE_NAME_RE.fullmatch(name):
            raise ProfileConfigError(f"invalid profile name: {name!r}")
        profile = _require_mapping(raw_profile, f"profiles.{name}")
        command = _normalize_command(profile.get("command"), f"profiles.{name}.command")
        _validate_placeholders(command, name)
        timeout = _bounded_config_int(
            profile.get("timeout", default_timeout),
            f"profiles.{name}.timeout",
            maximum=WORKER_MAX_TIMEOUT_SECONDS,
        )
        max_output_bytes = _bounded_config_int(
            profile.get("max_output_bytes", default_max_output_bytes),
            f"profiles.{name}.max_output_bytes",
            maximum=WORKER_MAX_OUTPUT_BYTES,
        )
        session_pattern = _validate_session_pattern(
            profile.get("session_pattern", DEFAULT_SESSION_PATTERN),
            f"profiles.{name}.session_pattern",
        )
        profiles[name] = {
            "command": command,
            "workdir": _normalize_dir(str(profile.get("workdir") or global_workdir or ""), f"profiles.{name}.workdir"),
            "timeout": timeout,
            "pass_env": [
                *validate_child_env_names(global_pass_env),
                *_normalize_string_list(profile.get("pass_env"), f"profiles.{name}.pass_env"),
            ],
            "env": _normalize_env(profile.get("env"), f"profiles.{name}.env"),
            "sessions": _normalize_sessions(profile.get("sessions"), f"profiles.{name}.sessions"),
            "default_session": str(profile.get("default_session") or ""),
            "allow_raw_session_id": _normalize_bool(
                profile.get("allow_raw_session_id", False),
                f"profiles.{name}.allow_raw_session_id",
            ),
            "session_pattern": session_pattern,
            "max_output_bytes": max_output_bytes,
            "tags": _normalize_profile_tags(profile.get("tags"), f"profiles.{name}.tags"),
            "advisory_safe": _normalize_bool(
                profile.get("advisory_safe", False),
                f"profiles.{name}.advisory_safe",
            ),
        }

    for name, profile in profiles.items():
        default_session = profile["default_session"]
        if default_session and default_session not in profile["sessions"]:
            if not profile["allow_raw_session_id"]:
                raise ProfileConfigError(
                    f"profiles.{name}.default_session must name a configured alias or allow raw session ids"
                )
            if not re.fullmatch(profile["session_pattern"], default_session):
                raise ProfileConfigError(f"profiles.{name}.default_session does not match session_pattern")
        if len(default_session) > 256 or "\0" in default_session:
            raise ProfileConfigError(f"profiles.{name}.default_session is invalid")

    default_profile = str(raw.get("default_profile") or "")
    if default_profile and default_profile not in profiles:
        raise ProfileConfigError(f"default_profile {default_profile!r} is not defined")

    return {
        "default_profile": default_profile,
        "profiles": profiles,
        "timeout": default_timeout,
        "max_output_bytes": default_max_output_bytes,
    }


def _parse_plan_json(raw_plan: str) -> dict[str, Any] | None:
    stripped = raw_plan.strip()
    if not stripped.startswith("{"):
        return None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    if not ("plan" in value and _PLAN_JSON_SELECTOR_KEYS.intersection(value)):
        return None
    return value


def build_profile_request(payload: Mapping[str, Any], default_profile: str = "") -> dict[str, str]:
    """Build a dispatcher request from a SynapticLathe task payload.

    Explicit task payload fields win. JSON-in-plan is kept as a compatibility
    path for callers that can only send a text plan.
    """
    raw_plan = payload.get("plan", "")
    if not isinstance(raw_plan, str) or not raw_plan:
        raise ProfileConfigError("Missing task plan")

    plan_json = _parse_plan_json(raw_plan)
    profile = payload.get("profile") or payload.get("tool")
    session_alias = payload.get("session_id") or payload.get("session") or payload.get("ssid")
    tool = payload.get("tool") or ""
    plan = raw_plan

    if plan_json:
        plan = str(plan_json.get("plan") or "")
        profile = profile or plan_json.get("profile") or plan_json.get("tool")
        tool = str(tool or plan_json.get("tool") or profile or "")
        session_alias = (
            session_alias or plan_json.get("session_id") or plan_json.get("session") or plan_json.get("ssid")
        )

    profile_name = str(profile or default_profile or "")
    if not profile_name:
        raise ProfileConfigError("Missing profile/tool and no default_profile is configured")
    if not _PROFILE_NAME_RE.fullmatch(profile_name):
        raise ProfileConfigError(f"Invalid profile/tool name: {profile_name!r}")
    if not plan or "\0" in plan:
        raise ProfileConfigError("Missing or invalid inner plan")
    tool_name = str(tool or profile_name)
    if not _PROFILE_NAME_RE.fullmatch(tool_name):
        raise ProfileConfigError(f"Invalid tool name: {tool_name!r}")
    session_selector = str(session_alias or "")
    if len(session_selector) > _MAX_SESSION_SELECTOR_CHARS or "\0" in session_selector:
        raise ProfileConfigError("Session selector is invalid or too large")
    source = str(payload.get("from") or "")
    if source and not _PROFILE_NAME_RE.fullmatch(source):
        raise ProfileConfigError("Source Agent name is invalid")
    return {
        "profile": profile_name,
        "tool": tool_name,
        "plan": plan,
        "session_alias": session_selector,
        "source": source,
    }


def _resolve_session(profile: Mapping[str, Any], request: Mapping[str, str]) -> tuple[str, str]:
    requested_alias = request.get("session_alias", "") or str(profile.get("default_session") or "")
    if not requested_alias:
        return "", ""

    sessions = profile.get("sessions", {}) or {}
    if requested_alias in sessions:
        return requested_alias, str(sessions[requested_alias])

    if profile.get("allow_raw_session_id"):
        pattern = re.compile(str(profile.get("session_pattern") or DEFAULT_SESSION_PATTERN))
        if pattern.fullmatch(requested_alias):
            return requested_alias, requested_alias

    raise ProfileConfigError(f"Session alias is not allowed for profile {request['profile']!r}: {requested_alias!r}")


def build_profile_command(profile: Mapping[str, Any], request: Mapping[str, str]) -> tuple[list[str], str]:
    """Build shell-free argv for a profile request."""
    command = [str(arg) for arg in profile["command"]]
    placeholders = _placeholder_names(command)
    session_fields = {"session_id", "session_alias"}
    needs_session_resolution = bool(session_fields.intersection(placeholders) or profile.get("default_session"))
    if needs_session_resolution:
        session_alias, session_id = _resolve_session(profile, request)
    else:
        session_alias = ""
        session_id = ""

    values = {
        "plan": request["plan"],
        "profile": request["profile"],
        "tool": request["tool"],
        "session_id": session_id,
        "session_alias": session_alias,
        "source": request.get("source", ""),
    }

    if "session_id" in placeholders and not session_id:
        raise ProfileConfigError(f"profile {request['profile']!r} requires a session_id")

    uses_plan_placeholder = "plan" in placeholders
    try:
        argv = [arg.format(**values) for arg in command]
    except KeyError as exc:
        raise ProfileConfigError(f"Unknown command placeholder: {exc.args[0]}") from exc
    if not uses_plan_placeholder:
        argv.extend(["--", request["plan"]])
    return argv, session_alias


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SynapticLathe local profile dispatcher worker")
    parser.add_argument(
        "--url",
        default=os.environ.get("SYNAPTIC_WS_URL", os.environ.get("SYNAPTIC_URL", DEFAULT_WS_URL)),
        help="SynapticLathe WebSocket URL. Env: SYNAPTIC_WS_URL or SYNAPTIC_URL.",
    )
    parser.add_argument(
        "--name",
        default=os.environ.get("SYNAPTIC_AGENT_NAME", DEFAULT_AGENT_NAME),
        help="Agent name to register on the server. Env: SYNAPTIC_AGENT_NAME.",
    )
    parser.add_argument(
        "--key",
        default=os.environ.get("SYNAPTIC_API_KEY", ""),
        help="Worker API key. Prefer env SYNAPTIC_API_KEY.",
    )
    parser.add_argument(
        "--profiles",
        default=os.environ.get("SYNAPTIC_PROFILE_CONFIG", os.environ.get("SYNAPTIC_PROFILES_FILE", "")),
        help="YAML/JSON profile config file. Env: SYNAPTIC_PROFILE_CONFIG or SYNAPTIC_PROFILES_FILE.",
    )
    parser.add_argument(
        "--default-profile",
        default=os.environ.get("SYNAPTIC_DEFAULT_PROFILE", ""),
        help="Override config default_profile. Env: SYNAPTIC_DEFAULT_PROFILE.",
    )
    parser.add_argument(
        "--workdir",
        default=os.environ.get("SYNAPTIC_WORKDIR", ""),
        help="Fallback workdir for profiles without workdir. Env: SYNAPTIC_WORKDIR.",
    )
    parser.add_argument(
        "--pass-env",
        action="append",
        default=_env_list("SYNAPTIC_PROFILE_PASS_ENV"),
        help=(
            "Global env var name passed to child profiles. Repeatable. "
            "Env SYNAPTIC_PROFILE_PASS_ENV is comma-separated."
        ),
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get("SYNAPTIC_PROFILE_LOCK_FILE", os.environ.get("SYNAPTIC_WORKER_LOCK_FILE", "")),
        help="Single-instance lock file. Default is derived from URL and agent name.",
    )
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        default=_env_bool("SYNAPTIC_ALLOW_DUPLICATE_WORKER", False),
        help="Disable local single-instance protection. Env: SYNAPTIC_ALLOW_DUPLICATE_WORKER=1.",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_false",
        dest="reconnect",
        default=_env_bool("SYNAPTIC_RECONNECT", True),
        help="Exit instead of reconnecting when the WebSocket disconnects.",
    )
    parser.add_argument(
        "--reconnect-initial-delay",
        type=float,
        default=_env_float("SYNAPTIC_RECONNECT_INITIAL_DELAY", 1.0),
        help="Initial reconnect delay in seconds. Env: SYNAPTIC_RECONNECT_INITIAL_DELAY.",
    )
    parser.add_argument(
        "--reconnect-max-delay",
        type=float,
        default=_env_float("SYNAPTIC_RECONNECT_MAX_DELAY", 30.0),
        help="Maximum reconnect delay in seconds. Env: SYNAPTIC_RECONNECT_MAX_DELAY.",
    )
    parser.add_argument(
        "--lock-retry-interval",
        type=float,
        default=_env_float("SYNAPTIC_LOCK_RETRY_INTERVAL", 60.0),
        help="Seconds between duplicate-worker lock retries. Env: SYNAPTIC_LOCK_RETRY_INTERVAL.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.profiles:
        parser.error("--profiles is required, or set SYNAPTIC_PROFILE_CONFIG")
    if args.reconnect_initial_delay <= 0:
        parser.error("--reconnect-initial-delay must be positive")
    if args.reconnect_max_delay < args.reconnect_initial_delay:
        parser.error("--reconnect-max-delay must be greater than or equal to the initial delay")
    if args.lock_retry_interval <= 0:
        parser.error("--lock-retry-interval must be positive")
    try:
        args.url = validate_websocket_url(args.url)
        args.name = validate_worker_agent_name(args.name)
        args.workdir = _normalize_dir(args.workdir, "--workdir")
        args.pass_env = validate_child_env_names(args.pass_env)
        args.profile_config = load_profile_config(
            args.profiles, global_workdir=args.workdir, global_pass_env=args.pass_env
        )
    except (ProfileConfigError, ValueError) as exc:
        parser.error(str(exc))
    if args.default_profile:
        if args.default_profile not in args.profile_config["profiles"]:
            parser.error(f"--default-profile {args.default_profile!r} is not defined")
        args.profile_config["default_profile"] = args.default_profile
    if not args.lock_file:
        args.lock_file = default_worker_lock_file("profile", args.url, args.name)
    return args


def build_agent_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "synapse_url": args.url,
        "agent_name": args.name,
        "api_key": args.key,
        "extra": {
            "profile_config": args.profile_config,
        },
    }


class ProfileDispatcherAgent(BaseAgent):
    """Dispatch tasks to local allowlisted command profiles."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        extra = config.get("extra", config)
        self.profile_config = extra.get("profile_config", {})
        self.profiles = self.profile_config.get("profiles", {})
        self.default_profile = self.profile_config.get("default_profile", "")
        self.synapse_url = config.get("synapse_url", DEFAULT_WS_URL)
        self.agent_name = config.get("agent_name", DEFAULT_AGENT_NAME)
        self.api_key = config.get("api_key", "")
        self._ws: Any | None = None
        self._current_proc: asyncio.subprocess.Process | None = None
        self._current_pgid: int | None = None

    async def connect(self) -> bool:
        try:
            import websockets
        except ImportError:
            print("websockets is required. Install with `pip install -r requirements.txt`.", file=sys.stderr)
            return False

        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            self._ws = await websockets.connect(
                self.synapse_url,
                max_size=WORKER_WS_MAX_MESSAGE_BYTES,
                max_queue=WORKER_WS_MAX_QUEUE,
                **websocket_headers_kwargs(websockets.connect, headers),
            )
            await self._ws.send(
                json.dumps(
                    {
                        "type": "register",
                        "payload": worker_registration_payload(
                            self.agent_name,
                            worker_kind="profile_worker",
                            capabilities=(
                                "task",
                                "accept",
                                "chunk",
                                "return",
                                "cancel",
                                "probe",
                                "profile_dispatch",
                                "keepalive",
                            ),
                            extra_client_fields={
                                "profiles": sorted(self.profiles),
                                "profile_capabilities": describe_profile_capabilities(self.profile_config),
                                "default_profile": self.default_profile,
                                "default_timeout": self.profile_config.get("timeout", DEFAULT_TIMEOUT_SECONDS),
                                "default_max_output_bytes": self.profile_config.get(
                                    "max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES
                                ),
                            },
                        ),
                    }
                )
            )
            resp = await receive_registration_ack(self._ws)
            if resp.get("type") == "error":
                print(f"Register failed: {resp}", file=sys.stderr)
                return False
            self._connected = True
            synapse_logger.info(
                "profile worker registered",
                extra={
                    "event": "profile_registered",
                    "agent": self.agent_name,
                    "profile_count": len(self.profiles),
                },
            )
            print_registration_banner(resp)
            return True
        except Exception as exc:
            synapse_logger.warning(
                "profile worker connection failed",
                extra={
                    "event": "profile_connection_failed",
                    "agent": self.agent_name,
                    "error_type": type(exc).__name__,
                },
            )
            print(f"Connection failed: {exc}", file=sys.stderr)
            return False

    async def disconnect(self) -> None:
        await self._kill_process()
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False

    async def _kill_process(self) -> None:
        """Terminate the current child and its process group."""
        proc = self._current_proc
        process_group_id = self._current_pgid
        self._current_proc = None
        self._current_pgid = None
        await terminate_child_process(proc, process_group_id)

    async def _send_chunk(self, cid: str, text: str) -> bool:
        text = sanitize_process_text(text)
        if not self._ws or not text:
            return True
        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "chunk",
                        "payload": {"text": text},
                        "correlation_id": cid,
                    }
                )
            )
        except Exception:
            return False
        return True

    async def _run_profile(self, payload: Mapping[str, Any], task_id: str, cid: str) -> dict[str, Any]:
        try:
            request = build_profile_request(payload, self.default_profile)
            profile = self.profiles.get(request["profile"])
            if not profile:
                return {
                    "exit_code": -1,
                    "output": "",
                    "error": f"Unknown profile/tool: {request['profile']}",
                    "profile": request["profile"],
                    "session_alias": request["session_alias"],
                }
            argv, session_alias = build_profile_command(profile, request)
        except ProfileConfigError as exc:
            return {"exit_code": -1, "output": "", "error": str(exc)}

        try:
            env = build_child_env(pass_env=profile.get("pass_env", []))
            for raw_name, raw_value in (profile.get("env", {}) or {}).items():
                name = validate_child_env_names([str(raw_name)])[0]
                if raw_value is not None:
                    env[name] = str(raw_value)
        except ValueError as exc:
            return {
                "exit_code": -1,
                "output": "",
                "error": str(exc),
                "profile": request["profile"],
                "session_alias": session_alias,
            }

        timeout = bounded_task_timeout(
            payload.get("timeout"),
            int(profile.get("timeout") or DEFAULT_TIMEOUT_SECONDS),
        )
        max_output_bytes = int(profile.get("max_output_bytes") or DEFAULT_MAX_OUTPUT_BYTES)
        output_buffer = LimitedByteBuffer(max_output_bytes)
        streamed_bytes = 0
        stream_enabled = True
        truncation_notice_sent = False

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE,
                cwd=profile.get("workdir") or None,
                env=env,
                **child_process_group_kwargs(),
            )
        except FileNotFoundError:
            error = "Configured profile command was not found"
        except PermissionError:
            error = "Configured profile command is not executable"
        except OSError:
            synapse_logger.exception(
                "profile worker failed to start configured command",
                extra={"event": "profile_start_failed", "profile": request["profile"]},
            )
            error = "Failed to start configured profile command"
        else:
            error = ""
        if error:
            return {
                "exit_code": -1,
                "output": "",
                "error": error,
                "profile": request["profile"],
                "session_alias": session_alias,
            }

        self._current_proc = proc
        self._current_pgid = child_process_group_id(proc)

        if proc.stdin:
            with suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                proc.stdin.close()

        async def _read_and_stream() -> None:
            nonlocal streamed_bytes, stream_enabled, truncation_notice_sent
            if proc.stdout is None:
                return
            while True:
                try:
                    chunk = await proc.stdout.read(READ_CHUNK_BYTES)
                except ValueError:
                    break
                if not chunk:
                    break
                output_buffer.append(chunk)
                remaining_stream_bytes = max(0, max_output_bytes - streamed_bytes)
                if stream_enabled and remaining_stream_bytes:
                    stream_chunk = chunk[:remaining_stream_bytes]
                    streamed_bytes += len(stream_chunk)
                    stream_enabled = await self._send_chunk(cid, stream_chunk.decode(errors="replace"))
                if stream_enabled and output_buffer.truncated and not truncation_notice_sent:
                    truncation_notice_sent = True
                    stream_enabled = await self._send_chunk(
                        cid, f"\n[output truncated after {max_output_bytes} bytes]\n"
                    )

        try:
            await asyncio.wait_for(asyncio.gather(_read_and_stream(), proc.wait()), timeout=timeout)
        except TimeoutError:
            await self._kill_process()
            return {
                "exit_code": -1,
                "output": output_buffer.text(),
                "output_truncated": output_buffer.truncated,
                "error": f"Process timed out after {timeout}s",
                "profile": request["profile"],
                "session_alias": session_alias,
            }
        except asyncio.CancelledError:
            await terminate_child_process(proc, self._current_pgid)
            raise
        except Exception:
            await terminate_child_process(proc, self._current_pgid)
            raise
        finally:
            self._current_proc = None
            self._current_pgid = None

        return {
            "exit_code": proc.returncode if proc.returncode is not None else -1,
            "output": output_buffer.text(),
            "output_truncated": output_buffer.truncated,
            "error": "",
            "profile": request["profile"],
            "session_alias": session_alias,
        }

    async def _handle_task_message(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload", {})
        task_id = payload.get("task_id", "")
        cid = msg.get("correlation_id", "")
        await self._ws.send(json.dumps({"type": "accept", "correlation_id": cid}))
        synapse_logger.info(
            "profile worker task started",
            extra={
                "event": "profile_task_started",
                "agent": self.agent_name,
                "task_id": task_id,
                "profile": payload.get("profile") or payload.get("tool") or self.default_profile,
                "timeout": payload.get("timeout") or self.profile_config.get("timeout"),
            },
        )

        result = await self._run_profile(payload, task_id, cid)
        synapse_logger.info(
            "profile worker task completed",
            extra={
                "event": "profile_task_completed",
                "agent": self.agent_name,
                "task_id": task_id,
                "profile": result.get("profile"),
                "exit_code": result.get("exit_code"),
                "output_truncated": bool(result.get("output_truncated")),
            },
        )
        return_payload = {
            "task_id": task_id,
            "exit_code": result["exit_code"],
            "output": result["output"],
        }
        for name in ("error", "output_truncated", "profile", "session_alias"):
            value = result.get(name)
            if value:
                return_payload[name] = value

        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "return",
                        "payload": return_payload,
                        "correlation_id": cid,
                    }
                )
            )
        except Exception:
            synapse_logger.exception(
                "profile worker task result delivery failed",
                extra={
                    "event": "profile_task_return_failed",
                    "agent": self.agent_name,
                    "task_id": task_id,
                    "profile": result.get("profile"),
                },
            )
            raise
        synapse_logger.info(
            "profile worker task result delivered",
            extra={
                "event": "profile_task_returned",
                "agent": self.agent_name,
                "task_id": task_id,
                "profile": result.get("profile"),
                "exit_code": result.get("exit_code"),
            },
        )

    async def run_loop(self) -> None:
        if self._ws:
            await run_worker_message_loop(self._ws, self._handle_task_message, self._kill_process)


async def _run_forever(args: argparse.Namespace) -> int:
    delay = args.reconnect_initial_delay
    while True:
        agent = ProfileDispatcherAgent(build_agent_config(args))
        connected = await agent.connect()
        connected_at = time.monotonic() if connected else 0.0
        if connected:
            profiles = ", ".join(sorted(args.profile_config["profiles"]))
            print(f"Connected profile worker '{args.name}' to {args.url}; profiles={profiles}")
            try:
                await agent.run_loop()
            except Exception as exc:
                synapse_logger.warning(
                    "profile worker connection loop ended",
                    extra={
                        "event": "profile_connection_ended",
                        "agent": args.name,
                        "error_type": type(exc).__name__,
                    },
                )
                print(f"Profile worker connection loop ended: {exc}", file=sys.stderr)
            finally:
                await agent.disconnect()
        else:
            await agent.disconnect()

        if not args.reconnect:
            return 0 if connected else 1
        if connected_at and time.monotonic() - connected_at >= 60:
            delay = args.reconnect_initial_delay
        print(f"Profile worker disconnected; reconnecting in {delay:g}s.", file=sys.stderr)
        await asyncio.sleep(delay)
        delay = min(delay * 2, args.reconnect_max_delay)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    lock = None
    if not args.allow_duplicate:
        lock = await wait_for_instance_lock(args.lock_file, f"Profile worker {args.name!r}", args.lock_retry_interval)
    try:
        return await _run_forever(args)
    finally:
        if lock is not None:
            lock.release()


def cli() -> None:
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    cli()
