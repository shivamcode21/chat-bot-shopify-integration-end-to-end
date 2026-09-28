#!/usr/bin/env python3
"""
Generate JSON for client_configs.widget_api_key_sha256 (hash only; store in DB).

Usage:
  export WIDGET_API_KEY_PEPPER=your-pepper   # must match server env
  python scripts/generate_widget_api_key_hash.py <client_uuid> <plain_api_key>

Prints a JSON object to store in config_value for config_key widget_api_key_sha256.
"""

from __future__ import annotations

import json
import os
import sys

# Project root on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fashion_bot.security.widget_api_key import compute_widget_api_key_hash  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: generate_widget_api_key_hash.py <client_id_uuid> <plain_api_key>", file=sys.stderr)
        sys.exit(1)
    client_id, plain = sys.argv[1], sys.argv[2]
    h = compute_widget_api_key_hash(client_id, plain)
    print(json.dumps({"hash": h}, indent=2))


if __name__ == "__main__":
    main()
