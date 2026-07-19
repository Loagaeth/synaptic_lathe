import ipaddress
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

_DNS_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_BODY_BYTES = 16 * 1024 * 1024


class _StrictConfigModel(BaseModel):
    """Reject misspelled configuration keys instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


def _valid_bind_host(value: str) -> bool:
    if value == "localhost":
        return True
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        labels = value.split(".")
        return bool(value) and len(value) <= 253 and all(_DNS_LABEL_RE.fullmatch(label) for label in labels)


class AgentConfig(_StrictConfigModel):
    """Server-side adapters. Local CLI tools connect through WebSocket workers."""

    type: Literal["http_api"]
    base_url: str = ""
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    stream: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_required_fields(self) -> "AgentConfig":
        if not self.base_url:
            raise ValueError("Agent type 'http_api' requires base_url")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Agent base_url must use http:// or https://")
        if parsed.username or parsed.password:
            raise ValueError("Agent base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Agent base_url must not contain a query or fragment")
        return self


class RouteRule(_StrictConfigModel):
    prefix: str = Field(default="", max_length=256)
    keyword: str = Field(default="", max_length=256)
    target: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")

    @model_validator(mode="after")
    def check_at_least_one_match(self) -> "RouteRule":
        if not self.prefix and not self.keyword:
            raise ValueError("RouteRule must specify at least one of prefix or keyword")
        return self


class RouterConfig(_StrictConfigModel):
    default_agent: str = Field(default="", max_length=64, pattern=r"^$|^[a-zA-Z0-9_-]+$")
    rules: list[RouteRule] = Field(default_factory=list, max_length=256)


class ServerConfig(_StrictConfigModel):
    host: str = "127.0.0.1"
    port: int = Field(default=9112, ge=1, le=65535)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    worker_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    cors_origins: list[str] = Field(default_factory=list, max_length=64)  # 空列表 → 自动生成
    ws_receive_timeout: int = Field(default=60, ge=5, le=3600)
    ws_ping_interval: int = Field(default=30, ge=5, le=300)
    behind_proxy: bool = False
    trusted_proxy_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    public_read_context: bool = False  # 公开读取 /context* 全量上下文；公网部署通常保持 false
    outbound_trust_env: bool = False  # 是否让服务端 HTTP adapter 继承 HTTP(S)_PROXY 等环境变量

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not _valid_bind_host(value):
            raise ValueError("host must be an IP address, localhost, or a valid DNS name")
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, origins: list[str]) -> list[str]:
        normalized: list[str] = []
        for origin in origins:
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"Invalid CORS origin: {origin!r}") from exc
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError(f"Invalid CORS origin: {origin!r}")
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname.lower()
            clean_origin = f"{parsed.scheme.lower()}://{host}"
            if port is not None:
                clean_origin += f":{port}"
            if clean_origin not in normalized:
                normalized.append(clean_origin)
        return normalized

    @field_validator("trusted_proxy_hosts")
    @classmethod
    def validate_trusted_proxies(cls, hosts: list[str]) -> list[str]:
        for host in hosts:
            try:
                ipaddress.ip_network(host, strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid trusted proxy IP/CIDR: {host!r}") from exc
        return hosts

    @model_validator(mode="after")
    def validate_websocket_timing(self) -> "ServerConfig":
        if self.ws_receive_timeout <= self.ws_ping_interval:
            raise ValueError("ws_receive_timeout must be greater than ws_ping_interval")
        return self

    def get_cors_origins(self) -> list[str]:
        """返回实际的 CORS 来源列表。空列表或默认值自动从 host:port 生成。"""
        if self.cors_origins:
            return self.cors_origins
        display_host = f"[{self.host}]" if ":" in self.host else self.host
        return [f"http://{display_host}:{self.port}"]

    rate_limit_max: int = Field(default=60, ge=1, le=10_000)
    rate_limit_window: int = Field(default=60, ge=1, le=3600)
    ws_rate_limit_max: int = Field(default=240, ge=10, le=100_000)
    ws_rate_limit_window: int = Field(default=60, ge=1, le=3600)
    max_body_bytes: int = Field(default=1_048_576, ge=1024, le=_MAX_BODY_BYTES)
    auto_memory_threshold: int = Field(default=0, ge=0, le=1000)
    auto_memory_max_chars: int = Field(default=4000, ge=256, le=100_000)
    pending_message_ttl_hours: int = Field(default=24, ge=1, le=720)
    task_history_hours: int = Field(default=168, ge=24, le=8760)

    def get_worker_api_key(self) -> str:
        """Use a least-privilege worker key, falling back for old configs."""
        return self.worker_api_key.get_secret_value() or self.api_key.get_secret_value()


class MemoryConfig(_StrictConfigModel):
    scope: Literal["shared", "persona"] = "shared"
    embedding_model: str = Field(default="", max_length=512)
    embedding_provider: Literal["local", "openai", "gemini", "nvidia", "ollama"] = "local"
    embedding_api_url: str = Field(default="", max_length=2048)
    embedding_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    embedding_dimensions: int = Field(default=0, ge=0, le=4096)
    embedding_timeout: int = Field(default=20, ge=1, le=300)
    embedding_trust_env: bool = False

    @field_validator("embedding_api_url")
    @classmethod
    def validate_embedding_url(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("embedding_api_url must use http:// or https://")
        if parsed.username or parsed.password:
            raise ValueError("embedding_api_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("embedding_api_url must not contain a query or fragment")
        return value


class GlobalConfig(_StrictConfigModel):
    router: RouterConfig = Field(default_factory=RouterConfig)
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    db_path: str = "data/synaptic_lathe.db"

    @field_validator("agents")
    @classmethod
    def validate_agent_names(cls, agents: dict[str, AgentConfig]) -> dict[str, AgentConfig]:
        invalid = [name for name in agents if not _AGENT_NAME_RE.fullmatch(name)]
        if invalid:
            raise ValueError(f"Invalid configured Agent name: {invalid[0]!r}")
        return agents

    @classmethod
    def load(cls, path: str | Path) -> "GlobalConfig":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Config file must be a YAML mapping, got {type(data).__name__}")
        if data.get("agents") is None:
            data["agents"] = {}
        config = cls.model_validate(data)
        # 相对路径 DB 基于 config 文件所在目录解析，不依赖 CWD
        dbp = Path(config.db_path)
        if not dbp.is_absolute():
            config.db_path = str((Path(path).resolve().parent / dbp).resolve())
        return config
