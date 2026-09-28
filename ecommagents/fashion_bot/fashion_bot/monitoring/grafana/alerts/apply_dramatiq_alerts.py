#!/usr/bin/env python3
"""Apply dramatiq_webhook_alerts.yaml to Grafana Cloud via the provisioning API.

Grafana Cloud can't read on-disk file provisioning, so this script creates the
folder and upserts each alert rule through the HTTP provisioning API. Rules are
created with X-Disable-Provenance so they stay editable in the UI afterwards.

Usage:
    export GRAFANA_URL="https://<your-stack>.grafana.net"
    export GRAFANA_TOKEN="<service-account-token-with-Editor/Alerting-write>"
    python apply_dramatiq_alerts.py            # apply
    python apply_dramatiq_alerts.py --dry-run  # print payloads only

Notes:
* The contact point "webhook-processing-errors" must already exist (it does).
* Re-running is idempotent (PUT by rule uid; POST if absent).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

YAML_PATH = Path(__file__).with_name("dramatiq_webhook_alerts.yaml")
DRY = "--dry-run" in sys.argv


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = os.environ["GRAFANA_URL"].rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + os.environ["GRAFANA_TOKEN"])
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Disable-Provenance", "true")  # keep rules UI-editable
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "{}")


def ensure_folder(title: str) -> str:
    status, folders = _req("GET", "/api/folders")
    for f in folders if isinstance(folders, list) else []:
        if f.get("title") == title:
            return f["uid"]
    status, created = _req("POST", "/api/folders", {"title": title})
    if status >= 300:
        sys.exit(f"folder create failed ({status}): {created}")
    return created["uid"]


def upsert_rule(rule: dict, group: str, folder_uid: str) -> None:
    payload = {
        "uid": rule["uid"],
        "title": rule["title"],
        "condition": rule["condition"],
        "data": rule["data"],
        "noDataState": rule.get("noDataState", "OK"),
        "execErrState": rule.get("execErrState", "Error"),
        "for": rule.get("for", "5m"),
        "orgID": 1,
        "ruleGroup": group,
        "folderUID": folder_uid,
        "labels": rule.get("labels", {}),
        "annotations": rule.get("annotations", {}),
        "notification_settings": rule.get("notification_settings"),
        "isPaused": False,
    }
    if DRY:
        print(json.dumps(payload, indent=2))
        return
    status, _ = _req("PUT", f"/api/v1/provisioning/alert-rules/{rule['uid']}", payload)
    if status == 404:
        status, resp = _req("POST", "/api/v1/provisioning/alert-rules", payload)
        if status >= 300:
            sys.exit(f"create {rule['uid']} failed ({status}): {resp}")
        print(f"created  {rule['uid']}")
    elif status >= 300:
        sys.exit(f"update {rule['uid']} failed ({status})")
    else:
        print(f"updated  {rule['uid']}")


def main() -> None:
    if not DRY and not (os.environ.get("GRAFANA_URL") and os.environ.get("GRAFANA_TOKEN")):
        sys.exit("Set GRAFANA_URL and GRAFANA_TOKEN (or pass --dry-run).")
    doc = yaml.safe_load(YAML_PATH.read_text())
    for group in doc["groups"]:
        folder_uid = "DRY" if DRY else ensure_folder(group["folder"])
        for rule in group["rules"]:
            upsert_rule(rule, group["name"], folder_uid)
    print("done." if not DRY else "dry-run complete.")


if __name__ == "__main__":
    main()
