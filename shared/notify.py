"""Discord webhook posting utilities."""

from __future__ import annotations

import os
import time
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

DISCORD_EMBED_DESC_LIMIT = 4096
DISCORD_FIELD_LIMIT = 1024
DISCORD_TOTAL_EMBED_LIMIT = 6000


def post(
    content: str = "",
    embeds: Optional[list[dict]] = None,
    webhook_url: Optional[str] = None,
    pause: float = 1.0,
) -> bool:
    """Discord Webhookに投稿"""
    url = webhook_url or os.environ.get("DISCORD_WEBHOOK")
    if not url:
        logger.error("DISCORD_WEBHOOK not configured")
        return False

    if not content and not embeds:
        logger.warning("post() called with no content and no embeds; skipping")
        return False

    payload = {}
    if content:
        payload["content"] = content[:2000]  # Discord content limit
    if embeds:
        payload["embeds"] = embeds[:10]  # Discord max 10 embeds per message

    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code >= 300:
            logger.error(f"Discord post failed: {r.status_code} {r.text[:200]}")
            return False
        time.sleep(pause)  # rate limit courtesy
        return True
    except Exception as e:
        logger.error(f"Discord post exception: {e}")
        return False


def chunk_text(text: str, limit: int = DISCORD_EMBED_DESC_LIMIT) -> list[str]:
    """改行を尊重しつつ文字数制限で分割。空チャンクは作らない"""
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        # 単一行がlimitを超える場合の強制分割
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        if len(current) + len(line) + 1 > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        chunks.append(current)
    return chunks


def post_embed(
    title: str,
    description: str,
    color: int = 0x2E86AB,
    webhook_url: Optional[str] = None,
    footer: Optional[str] = None,
) -> bool:
    """単一embedを投稿(長文は自動分割)"""
    chunks = chunk_text(description, DISCORD_EMBED_DESC_LIMIT)
    ok = True
    for i, chunk in enumerate(chunks):
        embed = {
            "title": title if i == 0 else f"{title} (cont.)",
            "description": chunk,
            "color": color,
        }
        if footer and i == len(chunks) - 1:
            embed["footer"] = {"text": footer}
        if not post(embeds=[embed], webhook_url=webhook_url):
            ok = False
    return ok


def post_error(error: Exception, bot_name: str, webhook_url: Optional[str] = None):
    """エラー発生時の簡易通知"""
    msg = f"🚨 **{bot_name}** crashed: `{type(error).__name__}: {str(error)[:500]}`"
    try:
        post(content=msg, webhook_url=webhook_url)
    except Exception:
        pass
