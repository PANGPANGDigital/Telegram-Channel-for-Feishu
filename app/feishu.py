"""Feishu custom bot rich-text webhook client."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Iterable
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class FeishuError(RuntimeError):
    """Raised when Feishu rejects a webhook request."""


class FeishuWebhook:
    def __init__(self, url: str, signing_secret: str | None) -> None:
        self.url = url
        self.signing_secret = signing_secret

    @staticmethod
    def text(text: str) -> dict[str, str]:
        return {"tag": "text", "text": text}

    @staticmethod
    def link(text: str, href: str) -> dict[str, str]:
        return {"tag": "a", "text": text, "href": href}

    def _signature(self, timestamp: str) -> str:
        if not self.signing_secret:
            return ""
        string_to_sign = f"{timestamp}\n{self.signing_secret}"
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            msg=b"",
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    async def send_post(self, title: str, elements: Iterable[dict[str, str]]) -> None:
        """Send one zh_cn rich-text post to the Feishu custom bot."""

        payload: dict[str, Any] = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [list(elements)],
                    }
                }
            },
        }
        if self.signing_secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = self._signature(timestamp)

        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            try:
                response = await client.post(self.url, json=payload)
                response.raise_for_status()
                result = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise FeishuError(f"飞书 Webhook 请求失败：{exc}") from exc

        code = result.get("code", result.get("StatusCode", 0))
        if code not in (0, "0", None):
            message = result.get("msg", result.get("StatusMessage", "未知错误"))
            raise FeishuError(f"飞书 Webhook 返回错误 {code}：{message}")

        logger.debug("飞书消息发送成功")
