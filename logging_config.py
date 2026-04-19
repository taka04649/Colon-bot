"""Common logging configuration."""

import logging
import sys


def setup_logging(name: str = "bot", level: int = logging.INFO) -> logging.Logger:
    """標準的なloggerセットアップ"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    return logging.getLogger(name)
