"""Small broker-inspection CLI (ops aid).

    python -m fashion_bot.workers.dlq stats          # pending depth per lane
    python -m fashion_bot.workers.dlq peek <key> [n] # LRANGE a raw broker list

Best-effort and read-only by default. Dramatiq's Redis broker keeps dead
messages internally with a TTL rather than in a single replayable list, so a
generic "replay" command would be broker-version specific; use ``peek`` to
inspect, and re-enqueue via the producer if needed. Keys here follow the
``{namespace}:{queue}`` layout (§17.1.2).
"""
from __future__ import annotations

import sys

from fashion_bot.env_loader import bootstrap_environment, get_env
from fashion_bot.workers import config

bootstrap_environment()

NAMESPACE = get_env("WEBHOOK_QUEUE_NAMESPACE", "dramatiq") or "dramatiq"


def _client():
    from fashion_bot.workers.broker import get_broker_sync_client
    c = get_broker_sync_client()
    if c is None:
        print("DRAMATIQ_BROKER_URL not set", file=sys.stderr)
        raise SystemExit(2)
    return c


def _stats() -> None:
    c = _client()
    print(f"broker namespace: {NAMESPACE}")
    for q in config.ALL_QUEUES:
        key = f"{NAMESPACE}:{q}"
        try:
            print(f"  {q:32s} depth={c.llen(key)}")
        except Exception as ex:
            print(f"  {q:32s} (llen failed: {ex!r})")


def _peek(key: str, n: int = 10) -> None:
    c = _client()
    for i, raw in enumerate(c.lrange(key, 0, n - 1)):
        print(f"[{i}] {raw}")


def main(argv=None) -> None:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] == "stats":
        _stats()
    elif argv[0] == "peek" and len(argv) >= 2:
        _peek(argv[1], int(argv[2]) if len(argv) > 2 else 10)
    else:
        print(__doc__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
