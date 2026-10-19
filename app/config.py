"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(ValueError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True)
class ChannelConfig:
    """A public Telegram channel to monitor."""

    name: str
    username: str
    public_url: str

    @property
    def key(self) -> str:
        return self.username.lower()


@dataclass(frozen=True)
class ProxyConfig:
    """HTTP proxy configuration used only for Telegram API requests."""

    enabled: bool
    url: str | None


@dataclass(frozen=True)
class MonitoringConfig:
    """Failure alerting and per-channel retry backoff settings."""

    failure_alert_threshold: int
    backoff_initial_seconds: int
    backoff_max_seconds: int


@dataclass(frozen=True)
class AppConfig:
    """Validated application configuration."""

    version: str
    timezone: str
    timezone_info: ZoneInfo
    state_file: Path
    channels: tuple[ChannelConfig, ...]
    proxy: ProxyConfig
    monitoring: MonitoringConfig
    feishu_webhook_url: str
    feishu_signing_secret: str | None
    poll_interval_seconds: int
    request_timeout_seconds: float
    log_level: str


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.startswith("请替换"):
        raise ConfigError(f"环境变量 {name} 未配置，请在 docker-compose.yml 中填写")
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def normalize_username(value: str) -> str:
    """Normalize @name, t.me/name, and https://t.me/name to name."""

    raw = value.strip()
    if raw.startswith("@"):
        raw = raw[1:]

    if raw.startswith(("https://", "http://")):
        parsed = urlparse(raw)
        if parsed.netloc.lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
            raise ConfigError(f"频道地址必须是 t.me 链接：{value}")
        raw = parsed.path.strip("/").split("/")[0]

    raw = raw.strip().lstrip("@").strip()
    if not raw or not _USERNAME_RE.fullmatch(raw):
        raise ConfigError(f"无效的 Telegram 频道 username：{value}")
    return raw


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"找不到配置文件：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 格式错误：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是对象")
    return data


def _parse_channels(raw_channels: Any) -> tuple[ChannelConfig, ...]:
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ConfigError("channels 必须是至少包含一个频道的列表")

    channels: list[ChannelConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_channels, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"channels[{index}] 必须是对象")
        name = str(item.get("name", "")).strip()
        raw_username = str(item.get("username", "")).strip()
        if not name:
            raise ConfigError(f"channels[{index}].name 不能为空")
        if not raw_username:
            raise ConfigError(f"channels[{index}].username 不能为空")

        username = normalize_username(raw_username)
        key = username.lower()
        if key in seen:
            raise ConfigError(f"频道重复配置：{username}")
        seen.add(key)
        channels.append(
            ChannelConfig(
                name=name,
                username=username,
                public_url=f"https://t.me/{username}",
            )
        )
    return tuple(channels)


def load_config(path: str | Path = "/app/config/config.yaml") -> AppConfig:
    """Load YAML configuration and secrets supplied by docker-compose."""

    config_path = Path(path)
    data = _load_yaml(config_path)

    version = str(data.get("version", "1.0.0")).strip()
    if version != "1.0.0":
        raise ConfigError(f"当前程序只支持配置版本 1.0.0，收到：{version}")

    timezone = str(data.get("timezone", "Asia/Shanghai")).strip()
    try:
        timezone_info = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"无效的时区：{timezone}") from exc

    state_file = Path(str(data.get("state_file", "/app/data/state.json")))
    raw_proxy = data.get("proxy", {})
    if raw_proxy is None:
        raw_proxy = {}
    if not isinstance(raw_proxy, dict):
        raise ConfigError("proxy 必须是对象")
    proxy_url = str(raw_proxy.get("url", "")).strip() or None
    proxy_enabled = bool(raw_proxy.get("enabled", bool(proxy_url)))
    if proxy_enabled and not proxy_url:
        raise ConfigError("proxy.enabled 为 true 时必须填写 proxy.url")
    if proxy_url and not proxy_url.lower().startswith(("http://", "https://")):
        raise ConfigError("当前仅支持 HTTP/HTTPS 代理，例如 http://127.0.0.1:7890")

    try:
        poll_interval_seconds = int(data.get("poll_interval_seconds", 30))
    except (TypeError, ValueError) as exc:
        raise ConfigError("poll_interval_seconds 必须是整数") from exc
    if poll_interval_seconds < 10:
        raise ConfigError("poll_interval_seconds 不能小于 10 秒")

    try:
        request_timeout_seconds = float(data.get("request_timeout_seconds", 20))
    except (TypeError, ValueError) as exc:
        raise ConfigError("request_timeout_seconds 必须是数字") from exc
    if request_timeout_seconds <= 0:
        raise ConfigError("request_timeout_seconds 必须大于 0")

    raw_monitoring = data.get("monitoring", {})
    if raw_monitoring is None:
        raw_monitoring = {}
    if not isinstance(raw_monitoring, dict):
        raise ConfigError("monitoring 必须是对象")

    def _monitoring_int(name: str, default: int) -> int:
        try:
            value = int(raw_monitoring.get(name, default))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"monitoring.{name} 必须是整数") from exc
        if value < 1:
            raise ConfigError(f"monitoring.{name} 必须大于 0")
        return value

    failure_alert_threshold = _monitoring_int("failure_alert_threshold", 3)
    backoff_initial_seconds = _monitoring_int("backoff_initial_seconds", 30)
    backoff_max_seconds = _monitoring_int("backoff_max_seconds", 900)
    if backoff_max_seconds < backoff_initial_seconds:
        raise ConfigError(
            "monitoring.backoff_max_seconds 不能小于 monitoring.backoff_initial_seconds"
        )

    return AppConfig(
        version=version,
        timezone=timezone,
        timezone_info=timezone_info,
        state_file=state_file,
        channels=_parse_channels(data.get("channels")),
        proxy=ProxyConfig(enabled=proxy_enabled, url=proxy_url if proxy_enabled else None),
        monitoring=MonitoringConfig(
            failure_alert_threshold=failure_alert_threshold,
            backoff_initial_seconds=backoff_initial_seconds,
            backoff_max_seconds=backoff_max_seconds,
        ),
        feishu_webhook_url=_required_env("FEISHU_WEBHOOK_URL"),
        feishu_signing_secret=_optional_env("FEISHU_SIGNING_SECRET"),
        poll_interval_seconds=poll_interval_seconds,
        request_timeout_seconds=request_timeout_seconds,
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
