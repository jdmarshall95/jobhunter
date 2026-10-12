"""Structured logging with secret masking.

Any registered secret value (bot token, API key) is replaced by ``***`` in the
rendered message, so a stray ``repr()`` can never leak a credential to stdout,
Docker logs or a commit.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_SECRETS: set[str] = set()
_MASK = "***"


def register_secret(value: str | None) -> None:
    """Register a raw secret value so it is masked in every log record."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


class SecretMaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.msg)
        if _SECRETS:
            for secret in _SECRETS:
                if secret in msg:
                    msg = msg.replace(secret, _MASK)
            record.msg = msg
            if record.args:
                record.args = tuple(
                    _MASK if isinstance(a, str) and a in _SECRETS else a for a in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SecretMaskFilter())
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s")
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "aiogram.dispatcher", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
