"""Application entrypoint."""

from __future__ import annotations

import logging
import sys

from .config import ConfigError, load_config
from .feishu import FeishuWebhook
from .state import StateStore
from .telegram_monitor import PublicChannelMonitor


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger(__name__)
    logger.info("启动 Telegram 频道消息推送 for Feishu v%s", config.version)
    logger.info(
        "已加载 %s 个公开频道，网页轮询间隔：%s 秒，Telegram 代理：%s",
        len(config.channels),
        config.poll_interval_seconds,
        config.proxy.url or "未启用",
    )

    state = StateStore(config.state_file)
    feishu = FeishuWebhook(config.feishu_webhook_url, config.feishu_signing_secret)
    PublicChannelMonitor(config, state, feishu).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
