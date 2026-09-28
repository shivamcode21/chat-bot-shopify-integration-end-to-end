#!/usr/bin/env python3
"""Run queued summary worker as a standalone process."""

import argparse
import asyncio

from fashion_bot.summary_worker import run_summary_worker_forever


def main() -> None:
    parser = argparse.ArgumentParser(description="Run fashion-bot summary worker")
    parser.add_argument("--client-id", default=None, help="Optional client UUID to process")
    parser.add_argument("--poll-seconds", type=float, default=None, help="Polling interval")
    args = parser.parse_args()

    asyncio.run(
        run_summary_worker_forever(
            poll_seconds=args.poll_seconds,
            client_id=args.client_id,
        )
    )


if __name__ == "__main__":
    main()
