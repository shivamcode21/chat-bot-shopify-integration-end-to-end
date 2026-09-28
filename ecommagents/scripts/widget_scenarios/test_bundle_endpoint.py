"""Backend checks for /widget/bundle-config/{client_ref}.json.

Run from the fashion_bot directory:  python3 ../scripts/widget_scenarios/test_bundle_endpoint.py

Covers the cases the browser suite cannot: it stubs the bundle response, so it
never exercises the real endpoint's client resolution or its cache headers.
"""
import sys, json; sys.path.insert(0,'.')
from fastapi import FastAPI
from fastapi.testclient import TestClient
import fashion_bot.widget_config as wc
import fashion_bot.config_manager as cm

CLIENTS = {"concept groove": "cid-42"}
STORE = {("cid-42","widget_launcher_theme"): {"background":"#0f3d2e"}}
FAIL = {"on": False}

async def fake_get_json(key, client_id=None):
    if FAIL["on"]:
        st = cm.config_read_state.get()
        if st is not None: st["failed"] = True; return None
    return STORE.get((client_id, key))
async def fake_get(key, client_id=None):
    v = await fake_get_json(key, client_id)
    return json.dumps(v) if v else None
async def fake_resolve_name(name):
    return CLIENTS.get((name or "").strip().lower())

wc.aget_json_config = fake_get_json; wc.aget_config = fake_get
wc._aresolve_client_id = fake_resolve_name
wc.ENABLE_WIDGET_LAUNCHER_THEME_CONFIG = True
app = FastAPI(); app.include_router(wc.widget_router); c = TestClient(app)

print("=== client NAME in path (the regression) ===")
r = c.get("/widget/bundle-config/Concept%20Groove.json").json()
print("  resolved client_id :", r["client_id"], "(expect cid-42)")
print("  launcher_theme     :", r["sections"]["launcher-theme"].get("launcher_theme"), "(expect the green theme)")

print("\n=== plain client_id in path still works ===")
r2 = c.get("/widget/bundle-config/cid-42.json").json()
print("  resolved client_id :", r2["client_id"], "| theme:", r2["sections"]["launcher-theme"].get("launcher_theme"))

print("\n=== unknown reference falls through unchanged ===")
r3 = c.get("/widget/bundle-config/no-such-client.json").json()
print("  resolved client_id :", r3["client_id"], "(expect no-such-client)")

print("\n=== nothing configured for a real client ===")
STORE.clear()
resp0 = c.get("/widget/bundle-config/cid-42.json")
print("  Cache-Control      :", resp0.headers.get("cache-control"), "(expect no-store)")
STORE[("cid-42","widget_launcher_theme")] = {"background":"#0f3d2e"}
resp1 = c.get("/widget/bundle-config/cid-42.json")
print("  with config again  :", resp1.headers.get("cache-control"), "(expect public, max-age=60)")

print("\n=== unresolvable client ===")
resp2 = c.get("/widget/bundle-config/ghost-client.json")
print("  Cache-Control      :", resp2.headers.get("cache-control"), "(expect no-store)")

print("\n=== config store degraded ===")
FAIL["on"] = True
resp = c.get("/widget/bundle-config/cid-42.json")
print("  Cache-Control      :", resp.headers.get("cache-control"), "(expect no-store)")
print("  incomplete_sections:", resp.json()["incomplete_sections"])
