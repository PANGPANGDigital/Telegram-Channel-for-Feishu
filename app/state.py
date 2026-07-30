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
        self._state: dict[str, Any] = {"initialized": False, "last_message_ids": {}}
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
            self._state = {
                "initialized": bool(loaded.get("initialized", False)),
                "last_message_ids": {
                    str(key): int(value) for key, value in last_message_ids.items()
                },
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
