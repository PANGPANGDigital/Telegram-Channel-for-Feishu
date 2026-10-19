"""Small JSON state store used to make restarts idempotent."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StateStore:
    """Persist startup status and the last forwarded message per channel."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            "initialized": False,
            "last_message_ids": {},
            "fetch_health": {},
        }
        self._load()

    @property
    def initialized(self) -> bool:
        return bool(self._state.get("initialized", False))

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("state 根节点不是对象")
            last_message_ids = loaded.get("last_message_ids", {})
            if not isinstance(last_message_ids, dict):
                raise ValueError("last_message_ids 不是对象")
            raw_fetch_health = loaded.get("fetch_health", {})
            if not isinstance(raw_fetch_health, dict):
                logger.warning("状态文件中的 fetch_health 不是对象，将重置频道抓取状态")
                raw_fetch_health = {}
            fetch_health: dict[str, dict[str, Any]] = {}
            for key, value in raw_fetch_health.items():
                if not isinstance(value, dict):
                    continue
                try:
                    consecutive_failures = max(0, int(value.get("consecutive_failures", 0)))
                except (TypeError, ValueError):
                    consecutive_failures = 0
                fetch_health[str(key)] = {
                    "last_success_at": value.get("last_success_at")
                    if isinstance(value.get("last_success_at"), str)
                    else None,
                    "last_failure_at": value.get("last_failure_at")
                    if isinstance(value.get("last_failure_at"), str)
                    else None,
                    "consecutive_failures": consecutive_failures,
                    "last_error": value.get("last_error")
                    if isinstance(value.get("last_error"), str)
                    else None,
                    "retry_after": value.get("retry_after")
                    if isinstance(value.get("retry_after"), str)
                    else None,
                    "alerted": bool(value.get("alerted", False)),
                    "recovery_pending": bool(value.get("recovery_pending", False)),
                }
            self._state = {
                "initialized": bool(loaded.get("initialized", False)),
                "last_message_ids": {
                    str(key): int(value) for key, value in last_message_ids.items()
                },
                "fetch_health": fetch_health,
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("无法读取状态文件 %s，将从空状态开始：%s", self.path, exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(self._state, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    async def mark_initialized(self) -> None:
        async with self._lock:
            self._state["initialized"] = True
            self._save()

    async def last_message_id(self, channel_key: str) -> int:
        async with self._lock:
            return int(self._state["last_message_ids"].get(channel_key, 0))

    async def should_forward(self, channel_key: str, message_id: int) -> bool:
        """Return whether a message is newer than the persisted forwarded message."""

        async with self._lock:
            last_id = int(self._state["last_message_ids"].get(channel_key, 0))
            return message_id > last_id

    async def mark_forwarded(self, channel_key: str, message_id: int) -> None:
        async with self._lock:
            self._state["last_message_ids"][channel_key] = message_id
            self._save()

    @staticmethod
    def _empty_fetch_health() -> dict[str, Any]:
        return {
            "last_success_at": None,
            "last_failure_at": None,
            "consecutive_failures": 0,
            "last_error": None,
            "retry_after": None,
            "alerted": False,
            "recovery_pending": False,
        }

    async def fetch_health(self, channel_key: str) -> dict[str, Any]:
        async with self._lock:
            health = self._state["fetch_health"].get(channel_key)
            if not isinstance(health, dict):
                return self._empty_fetch_health()
            return {**self._empty_fetch_health(), **health}

    async def record_fetch_success(self, channel_key: str, occurred_at: str) -> dict[str, Any]:
        async with self._lock:
            health = self._state["fetch_health"].get(channel_key)
            if not isinstance(health, dict):
                health = self._empty_fetch_health()
            else:
                health = {**self._empty_fetch_health(), **health}

            if health["alerted"] and health["consecutive_failures"] > 0:
                health["recovery_pending"] = True
            health["last_success_at"] = occurred_at
            health["consecutive_failures"] = 0
            health["last_error"] = None
            health["retry_after"] = None
            self._state["fetch_health"][channel_key] = health
            self._save()
            return dict(health)

    async def record_fetch_failure(
        self,
        channel_key: str,
        occurred_at: str,
        error: str,
        retry_after: str,
    ) -> dict[str, Any]:
        async with self._lock:
            health = self._state["fetch_health"].get(channel_key)
            if not isinstance(health, dict):
                health = self._empty_fetch_health()
            else:
                health = {**self._empty_fetch_health(), **health}

            health["consecutive_failures"] += 1
            health["last_failure_at"] = occurred_at
            health["last_error"] = error[:500]
            health["retry_after"] = retry_after
            # A renewed failure before the recovery alert is delivered keeps the
            # original incident open instead of emitting a false recovery.
            health["recovery_pending"] = False
            self._state["fetch_health"][channel_key] = health
            self._save()
            return dict(health)

    async def mark_fetch_alerted(self, channel_key: str) -> None:
        async with self._lock:
            health = self._state["fetch_health"].setdefault(
                channel_key, self._empty_fetch_health()
            )
            health["alerted"] = True
            self._save()

    async def mark_fetch_recovery_notified(self, channel_key: str) -> None:
        async with self._lock:
            health = self._state["fetch_health"].get(channel_key)
            if not isinstance(health, dict):
                return
            health["alerted"] = False
            health["recovery_pending"] = False
            self._save()
