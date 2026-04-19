"""Artifact and topic history management."""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def load_history(path: str) -> list[dict]:
    """履歴JSONを読み込む。存在しなければ空リスト"""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"History load failed ({path}): {e}")
        return []


def save_history(path: str, entries: list[dict]):
    """履歴JSONを書き出す"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def filter_recent(entries: list[dict], days: int, date_key: str = "date") -> list[dict]:
    """直近N日のエントリのみ抽出"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for e in entries:
        try:
            dt = datetime.fromisoformat(e[date_key])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                out.append(e)
        except Exception:
            continue
    return out


def trim_history(entries: list[dict], retention_days: int, date_key: str = "date") -> list[dict]:
    """保持期間を超えるエントリを削除"""
    return filter_recent(entries, retention_days, date_key)


def save_artifact(path: str, content: str):
    """任意のテキストファイルを保存"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"Saved artifact: {path}")


def save_json_artifact(path: str, data: dict):
    """JSON形式のartifact保存"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved JSON artifact: {path}")
