"""Claude API client wrapper.

Provides a simple interface to Claude with retry logic and JSON extraction helpers.
"""

from __future__ import annotations

import os
import json
import time
import logging
from typing import Optional

from anthropic import Anthropic, APIError, APIStatusError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("MODEL", "claude-sonnet-4-6")

_client: Optional[Anthropic] = None


def get_client() -> Anthropic:
    """シングルトンクライアント"""
    global _client
    if _client is None:
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


def call(
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
    max_retries: int = 3,
) -> str:
    """Claudeを呼び出しテキストを返す。overloaded時に指数バックオフでリトライ"""
    client = get_client()
    model = model or DEFAULT_MODEL

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text.strip()
        except APIStatusError as e:
            last_error = e
            if e.status_code in (429, 529, 500, 502, 503, 504):
                wait = 2 ** attempt * 5
                logger.warning(f"API {e.status_code}, retrying in {wait}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            raise
        except APIError as e:
            last_error = e
            wait = 2 ** attempt * 3
            logger.warning(f"API error, retrying in {wait}s: {e}")
            time.sleep(wait)

    raise RuntimeError(f"Claude API failed after {max_retries} retries: {last_error}")


def call_json(
    system: str,
    user: str,
    model: Optional[str] = None,
    max_tokens: int = 2000,
    temperature: float = 0.3,
    max_retries: int = 3,
) -> dict:
    """JSONレスポンスを期待するClaude呼び出し。フェンス除去+パース"""
    text = call(system, user, model, max_tokens, temperature, max_retries)
    return parse_json(text)


def parse_json(text: str) -> dict:
    """Claudeの出力からJSONを抽出してパース"""
    text = text.strip()
    # コードフェンス除去
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    # 先頭が{でなければ最初の{から最後の}までを抽出
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed. Raw text (first 500 chars):\n{text[:500]}")
        raise ValueError(f"Failed to parse JSON: {e}") from e
