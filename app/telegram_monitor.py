"""Public Telegram channel page polling and message formatting."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
from bs4 import BeautifulSoup, Tag

from .config import AppConfig, ChannelConfig
from .feishu import FeishuError, FeishuWebhook
from .state import StateStore

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 3500
TELEGRAM_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "Chrome/124.0 Safari/537.36"
)


@dataclass(frozen=True)
class PublicChannelMessage:
    channel: ChannelConfig
    message_id: int
    text: str
    published_at: datetime

    @property
    def public_url(self) -> str:
        return f"{self.channel.public_url}/{self.message_id}"


class PublicChannelMonitor:
    """Poll t.me/s pages, so no Telegram account or Bot membership is needed."""

    def __init__(self, config: AppConfig, state: StateStore, feishu: FeishuWebhook) -> None:
        self.config = config
        self.state = state
        self.feishu = feishu

    async def _fetch_channel_messages(
        self,
        client: httpx.AsyncClient,
        channel: ChannelConfig,
    ) -> list[PublicChannelMessage] | None:
        page_url = f"https://t.me/s/{channel.username}"
        health = await self.state.fetch_health(channel.key)
        retry_after = self._parse_timestamp(health.get("retry_after"))
        now = datetime.now(timezone.utc)
        if retry_after is not None and retry_after > now:
            logger.debug(
                "频道仍处于退避期，跳过本轮抓取：%s，下次尝试时间：%s",
                channel.username,
                retry_after.isoformat(),
            )
            return None

        try:
            response = await client.get(page_url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            retry_after_seconds = self._retry_after_seconds(
                exc.response.headers.get("Retry-After")
            )
            error = (
                f"HTTP {exc.response.status_code} {exc.response.reason_phrase}; "
                f"url={exc.request.url}"
            )
            await self._record_fetch_failure(channel, error, retry_after_seconds)
            return None
        except httpx.RequestError as exc:
            request = getattr(exc, "request", None)
            request_url = str(request.url) if request is not None else page_url
            error = f"{type(exc).__name__}: {exc!r}; url={request_url}"
            await self._record_fetch_failure(channel, error)
            return None
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc!r}; url={page_url}"
            await self._record_fetch_failure(channel, error)
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        messages: list[PublicChannelMessage] = []
        for node in soup.select(".tgme_widget_message[data-post]"):
            message = self._parse_message(node, channel)
            if message is not None:
                messages.append(message)

        messages.sort(key=lambda item: item.message_id)
        fetched_at = datetime.now(timezone.utc)
        health = await self.state.record_fetch_success(channel.key, fetched_at.isoformat())
        logger.info(
            "读取公开频道成功：%s，成功时间：%s，解析到 %s 条文字消息",
            channel.username,
            fetched_at.astimezone(self.config.timezone_info).strftime("%Y-%m-%d %H:%M:%S"),
            len(messages),
        )
        if health["recovery_pending"]:
            await self._notify_fetch_recovery(channel, health)

        if not messages:
            logger.info("频道当前页面没有可推送的文字消息：%s", channel.username)
        return messages

    async def _record_fetch_failure(
        self,
        channel: ChannelConfig,
        error: str,
        server_retry_after_seconds: float | None = None,
    ) -> None:
        previous = await self.state.fetch_health(channel.key)
        failure_number = int(previous["consecutive_failures"]) + 1
        exponent = min(failure_number - 1, 30)
        backoff_seconds = min(
            self.config.monitoring.backoff_max_seconds,
            self.config.monitoring.backoff_initial_seconds * (2**exponent),
        )
        if server_retry_after_seconds is not None:
            backoff_seconds = max(backoff_seconds, server_retry_after_seconds)

        occurred_at = datetime.now(timezone.utc)
        retry_at = occurred_at + timedelta(seconds=backoff_seconds)
        health = await self.state.record_fetch_failure(
            channel.key,
            occurred_at.isoformat(),
            error,
            retry_at.isoformat(),
        )
        logger.warning(
            "读取公开频道失败：%s；连续失败 %s 次；错误：%s；下次尝试不早于 %s",
            channel.username,
            health["consecutive_failures"],
            error,
            retry_at.astimezone(self.config.timezone_info).strftime("%Y-%m-%d %H:%M:%S"),
        )

        if (
            health["consecutive_failures"] >= self.config.monitoring.failure_alert_threshold
            and not health["alerted"]
        ):
            title = f"Telegram 频道抓取异常 · {channel.name}"
            elements = [
                self.feishu.text(f"频道：{channel.name}（@{channel.username}）\n"),
                self.feishu.text(f"连续失败：{health['consecutive_failures']} 次\n"),
                self.feishu.text(
                    f"最近成功抓取：{self._format_timestamp(health['last_success_at'])}\n"
                ),
                self.feishu.text(f"最近错误：{health['last_error']}\n"),
                self.feishu.text(
                    "下次尝试不早于："
                    f"{retry_at.astimezone(self.config.timezone_info):%Y-%m-%d %H:%M:%S}"
                    f"（{self.config.timezone}）"
                ),
            ]
            if await self._send_with_retries(title, elements):
                await self.state.mark_fetch_alerted(channel.key)

    async def _notify_fetch_recovery(
        self,
        channel: ChannelConfig,
        health: dict[str, object],
    ) -> None:
        title = f"Telegram 频道抓取已恢复 · {channel.name}"
        elements = [
            self.feishu.text(f"频道：{channel.name}（@{channel.username}）\n"),
            self.feishu.text(
                f"恢复时间：{self._format_timestamp(health.get('last_success_at'))}\n"
            ),
            self.feishu.text("页面抓取已成功，后续轮询已恢复。"),
        ]
        if await self._send_with_retries(title, elements):
            await self.state.mark_fetch_recovery_notified(channel.key)

    def _format_timestamp(self, value: object) -> str:
        parsed = self._parse_timestamp(value)
        if parsed is None:
            return "暂无成功记录"
        return (
            parsed.astimezone(self.config.timezone_info).strftime("%Y-%m-%d %H:%M:%S")
            + f"（{self.config.timezone}）"
        )

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    @staticmethod
    def _parse_message(node: Tag, channel: ChannelConfig) -> PublicChannelMessage | None:
        post_ref = str(node.get("data-post", ""))
        try:
            message_id = int(post_ref.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            return None

        text_node = node.select_one(".tgme_widget_message_text")
        if text_node is None:
            # The project only forwards text/caption content, not media-only posts.
            return None
        text = text_node.get_text("\n", strip=True)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return None

        published_at = PublicChannelMonitor._parse_date(str(node.get("data-date", "")))
        return PublicChannelMessage(
            channel=channel,
            message_id=message_id,
            text=text,
            published_at=published_at,
        )

    @staticmethod
    def _parse_date(value: str) -> datetime:
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    async def _post_init(self, client: httpx.AsyncClient) -> None:
        first_start = not self.state.initialized
        title = "Telegram 公开频道监控首次启动" if first_start else "Telegram 公开频道监控已重启"
        elements: list[dict[str, str]] = [
            self.feishu.text("项目版本：1.0.0\n"),
            self.feishu.text("运行模式：公开频道网页轮询（无需 Telegram 登录或加入机器人）\n"),
            self.feishu.text(
                "首次启动将尝试推送每个频道当前最新的一条文字消息。\n"
                if first_start
                else "本次为容器重启，已恢复持久化状态。\n"
            ),
            self.feishu.text(f"正在监控的频道（{len(self.config.channels)} 个）：\n"),
        ]
        for channel in self.config.channels:
            elements.extend(
                [
                    self.feishu.text(f"• {channel.name}："),
                    self.feishu.link(channel.public_url, channel.public_url),
                    self.feishu.text("\n"),
                ]
            )

        if not await self._send_with_retries(title, elements):
            return

        # On the first deployment, or when a new channel is added, initialize it
        # with the currently latest text post. Existing channels on restart are
        # intentionally not replayed here.
        initialization_complete = True
        for channel in self.config.channels:
            last_id = await self.state.last_message_id(channel.key)
            if not first_start and last_id > 0:
                continue
            messages = await self._fetch_channel_messages(client, channel)
            if messages is None:
                initialization_complete = False
                continue
            if messages:
                latest = messages[-1]
                if not await self._forward_message(latest):
                    initialization_complete = False

        if initialization_complete:
            await self.state.mark_initialized()

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        for channel in self.config.channels:
            messages = await self._fetch_channel_messages(client, channel)
            if messages is None:
                continue

            last_id = await self.state.last_message_id(channel.key)
            if last_id == 0:
                # A new channel, or a channel whose first initialization failed,
                # starts from the current latest post instead of replaying the
                # page's historical batch.
                if messages:
                    await self._forward_message(messages[-1])
                continue

            for message in messages:
                if message.message_id <= last_id:
                    continue
                if not await self._forward_message(message):
                    # Preserve order and retry this message on the next poll.
                    break
                last_id = message.message_id

    async def _forward_message(self, message: PublicChannelMessage) -> bool:
        if not await self.state.should_forward(message.channel.key, message.message_id):
            return True

        text = message.text
        if len(text) > MAX_MESSAGE_LENGTH:
            text = text[:MAX_MESSAGE_LENGTH].rstrip() + "\n…（内容过长，已截断）"

        local_time = message.published_at.astimezone(self.config.timezone_info)
        elements = [
            self.feishu.text(f"频道：{message.channel.name}\n"),
            self.feishu.text(
                f"时间：{local_time:%Y-%m-%d %H:%M:%S}（{self.config.timezone}）\n\n"
            ),
            self.feishu.text(text + "\n\n"),
            self.feishu.link("查看 Telegram 原消息", message.public_url),
        ]
        title = f"Telegram 新消息 · {message.channel.name}"
        if not await self._send_with_retries(title, elements):
            return False

        await self.state.mark_forwarded(message.channel.key, message.message_id)
        logger.info("已推送频道消息：%s/%s", message.channel.username, message.message_id)
        return True

    async def _send_with_retries(self, title: str, elements: list[dict[str, str]]) -> bool:
        for attempt in range(1, 4):
            try:
                await self.feishu.send_post(title, elements)
                return True
            except FeishuError as exc:
                if attempt == 3:
                    logger.error("飞书消息连续发送失败，放弃本次发送：%s", exc)
                    return False
                delay = 2**attempt
                logger.warning("飞书发送失败，将在 %s 秒后重试（%s/3）：%s", delay, attempt, exc)
                await asyncio.sleep(delay)
        return False

    async def _run(self) -> None:
        proxy = self.config.proxy.url if self.config.proxy.enabled else None
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=self.config.request_timeout_seconds,
            headers={"User-Agent": TELEGRAM_USER_AGENT},
            follow_redirects=True,
            trust_env=False,
        ) as client:
            await self._post_init(client)
            while True:
                try:
                    await self._poll_once(client)
                except Exception:
                    logger.exception("本轮公开频道轮询异常")
                await asyncio.sleep(self.config.poll_interval_seconds)

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            logger.info("服务已停止")
