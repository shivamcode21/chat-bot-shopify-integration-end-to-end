"""Dramatiq-based background task pipeline.

See ``fashion_bot/design_docs/WEBHOOK_QUEUE_DRAMATIQ.md`` for the full design.

Safety contract: importing this package must stay cheap and must NOT import
``dramatiq`` at module load on the producer (web) side. The producer reaches the
queue only through :func:`fashion_bot.workers.enqueue.submit_or_inline`, which
imports the broker/actors lazily and only when ``WEBHOOK_QUEUE_ENABLED`` is set.
With the flag off (default) the webhook request path is byte-for-byte the legacy
inline behaviour.
"""
