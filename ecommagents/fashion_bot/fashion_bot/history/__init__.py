"""
History package exports.

Keep imports lazy so loading `fashion_bot.history.*` does not eagerly pull
BigQuery dependencies during worker bootstrap.
"""

from typing import Any

__all__ = ["log_conversation_to_bigquery"]


def __getattr__(name: str) -> Any:
    if name == "log_conversation_to_bigquery":
        try:
            from .bigquery_logger import log_conversation_to_bigquery
        except Exception as exc:
            raise AttributeError(
                "log_conversation_to_bigquery is unavailable (BigQuery dependency/config issue)"
            ) from exc
        return log_conversation_to_bigquery
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
