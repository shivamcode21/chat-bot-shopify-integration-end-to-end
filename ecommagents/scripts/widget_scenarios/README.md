# Widget launcher scenario suite

End-to-end scenarios for the chat widget launcher (v78), covering the two
first-paint bugs and the launcher-config failure policy:

* the launcher must never show the built-in purple pill or a `Loading…`
  placeholder — the first frame a shopper sees must be the final one;
* launcher config gets one 5s attempt plus one 10s retry, and if it still
  does not arrive the widget is not rendered at all and a critical Faro
  event is pushed.

## Running

```bash
npm install playwright          # once; Chromium is already on the image
python3 harness.py &            # local stand-in for the config API, port 8795
node run.mjs                    # against the harness
```

Against a live service (no harness needed):

```bash
node run.mjs --base-url http://127.0.0.1:4631 --page /test
```

To exercise a bundle the host has not deployed yet, serve a local file in its
place — the config API, page and network stay the live ones:

```bash
node run.mjs --base-url http://127.0.0.1:4631 --page /test \
  --inject-bundle ../../fashion_bot/static/chat-widget.v78.js --inject-as v78
```

The panel lives in its own document, so frame-side changes need it injected too:

```bash
node run.mjs --base-url http://127.0.0.1:4631 --page /test \
  --inject-bundle ../../fashion_bot/static/chat-widget.v78.js \
  --inject-frame ../../fashion_bot/static/chat-widget-frame.html
```

If the test browser cannot egress but CLI tools can (some sandboxes), put
`reverse_proxy.py` in front so Chromium only talks to localhost:

```bash
python3 reverse_proxy.py --target https://your-host --port 8796 &
node run.mjs --base-url http://127.0.0.1:8796 --page /test
```

Other flags: `--version v77` (baseline comparison), `--only C4` (single
scenario), `--headed` (watch it run).

Exit status is non-zero if any check fails.

## Scenarios

| id | what it exercises |
|----|-------------------|
| C1 | healthy config, fast network |
| C2 | every endpoint `200 {enabled:false}` — the real default flag state |
| C3 | one endpoint returns HTTP 500 |
| C4 | one endpoint hangs — the 5s + 10s retry and the ~15s give-up |
| C5 | first attempt fails, retry succeeds |
| C6 | every config endpoint dead |
| C7 | config slow but inside budget (3s) |
| N1 | Slow 3G, healthy config |
| N2 | offline from the start |
| M1 | mobile viewport 390x844 |
| P1 | panel prewarm on hover |
| P2 | prewarm on Slow 3G, watching for ready-watchdog teardown |

Faults are injected with Playwright route interception rather than by
changing the server, so every scenario runs unmodified against any host.

## Notes

* C4 and C6 deliberately leave the widget hidden for ~15s. That is the
  intended behaviour, not a test failure.
* Fault injection must not touch `/widget/config.json`. Aborting it makes the
  loader fall back to an older bundle, so the scenario silently stops testing
  the build you meant to test.
* `run.mjs` stubs `window.GrafanaFaroWebSdk` so `pushEvent` calls are
  captured instead of shipped. Remove the `addInitScript` block if you want
  a run to land real events in Grafana — C3, C4 and C6 will each fire a
  genuine critical alert if you do.
