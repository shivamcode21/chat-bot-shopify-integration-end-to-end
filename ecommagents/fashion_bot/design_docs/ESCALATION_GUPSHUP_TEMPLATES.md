# Escalation WhatsApp Notifications via Gupshup Templates

> **Status:** Draft v2 (revised after review — see §13 changelog)
> **Owner:** CX platform
> **Scope:** Deliver staff escalation WhatsApp notifications through pre-approved
> Gupshup templates for clients that opt in, keeping the existing free-text
> `send_message` path as the default for everyone else.

---

## 1. Problem

Escalations (frustrated customer, return/exchange, config gap, cancellation
threat, delivery-partner failure, store-visit lead, …) are delivered to **staff**
by `asend_escalation_notification()`
(`fashion_bot/utils/escalation_helper.py:87`). For every resolved staff phone it
calls `send_message(num, notification, …)` (`fashion_bot/gupshup_webhook.py:310`),
which sends a **free-text WhatsApp "session" message**.

**The gap:** WhatsApp only delivers a free-text session message inside an open
**24-hour customer-service window**. A staff recipient who has not messaged the
business number recently has a *closed* window, so the escalation WhatsApp is
**silently dropped by WhatsApp** — the gateway still returns HTTP 200/`success`
(acceptance ≠ delivery; `gupshup_webhook.py:421-423`). Today such escalations
reach staff only by the parallel email channel. Business-initiated messaging
outside the 24h window requires a **pre-approved template (HSM)** — which is what
this design adds for opted-in clients' staff escalation alerts.

---

## 2. Goal / Non-goals

**Goals**
- For clients that configure an escalation template, deliver the staff
  escalation WhatsApp via that template (works outside the 24h window).
- Keep free-text `send_message` as the **default** — zero behavior change for
  non-opted-in clients.
- Reuse each client's existing template credentials; add **no** new secrets.
- Fail safe: never make an escalation *worse* than today. Email always fires in
  parallel and remains the only guaranteed channel.
- **Accurate telemetry** — never report a silently-dropped message as delivered.

**Non-goals**
- Changing customer-facing template paths (Shopify / logistics webhooks).
- Changing escalation routing, contact resolution, DB logging, or email.
- Template authoring/approval UI (templates are created & approved in Gupshup
  out of band).
- Interactive / button / carousel / flow template variants.
- **Enterprise transport in phase 1** (see §5 — legacy-first, enterprise gated).

---

## 3. Background: existing building blocks

| Concern | Existing code | Reuse |
|---|---|---|
| Per-client transport selection | `aget_whatsapp_api_version()` (`utils/whatsapp_api_version.py:62`) | Choose enterprise vs legacy; **phase 1 = legacy only** |
| Legacy template creds | `aget_gupshup_config()` → `gupshup_template_details` (`shopify/webhook/gupshup_template_sender.py:43`) | Creds for the escalation template POST |
| Legacy template POST shape | `asend_shopify_gupshup_template_generic()` (`gupshup_template_sender.py:184-240`) | **Reference only — NOT called directly (see §6.2)** |
| Enterprise template send | `asend_enterprise_template()` (media) + `asend_enterprise_text_template()` (text, new) (`utils/whatsapp_enterprise_client.py`) | Enterprise transport (§5, §6.3) |
| Param ordering | `resolve_template_params()` (`utils/template_param_resolver.py:92`) | Map fields → `var1..varN` |
| Config reads | `aget_config(key, client_id=…)` three-tier cache (AGENTS.md §3) | Load the new opt-in config |
| Contact resolution | `aget_escalation_contacts()` (`agent_config.py:350`) | Unchanged — same staff recipients |

---

## 4. Configuration (opt-in)

Client-config key **`escalation_template`** provides the shared `param_order`
for all templates. Read via the three-tier cache. Stored value may be a JSON
string or a dict (parse both, mirroring `aget_gupshup_config`).

```jsonc
// client_configs.config_key = 'escalation_template'
{
  "param_order": [                     // canonical field names, in template var order
    "priority",                        // "" normally, "URGENT" when immediate_attention
    "category",
    "customer_contact",
    "order_id",
    "summary"
  ]
}
```

All approved templates for a client share the same variable structure (same
number and order of `{{n}}` placeholders), so `param_order` is configured once
here and inherited by every per-route template.

`template_id`, `enabled`, and `image_url` are configured per-route inside
the `escalation_contact` routing nodes (see §4.3). They are **not** part of
this config key.

**Credentials are NOT duplicated here.** The send reuses the client's existing
`gupshup_template_details` creds (legacy). Secrets stay in one place (AGENTS.md §7).

### 4.1 Canonical fields — "never None, never empty, always sanitized"

`param_order` may reference these canonical keys. The field builder
(`build_escalation_template_fields`, §6.1) emits every key as a **non-None,
non-empty**, sanitized (§6.5) string:

- never `None` — a `None` for a non-dynamic param key makes
  `resolve_template_params` raise a `ValueError` and abort the send; and
- never `""` — **WhatsApp/Meta rejects a template send whose parameter value is
  blank**, so optional fields fall back to a safe token. This lets a template
  reference *any* field (a dedicated Order ID or Urgency line) without a
  blank-parameter rejection on web-chat / non-urgent / no-order escalations.

| Canonical key | Source | Fallback when absent |
|---|---|---|
| `priority` | `"URGENT"` when `immediate_attention` else `"Normal"` | `"Normal"` |
| `category` | escalation category | `"Escalation"` |
| `customer_contact` | user contact → stripped customer phone → `"web chat"` | `"web chat"` |
| `customer_name` | resolved name (often absent — most call sites don't pass it) | `"N/A"` |
| `order_id` | order id | `"N/A"` |
| `escalation_group` | pre_sales / post_sales / offline_leads | `"Pre Sales"` |
| `summary` | the `details` block, whitespace-collapsed + length-capped (§6.5) | `"No additional details"` |
| `conversation` | last ≤3 customer messages (from `state`), joined ` / `, capped ~400 | `"N/A"` |
| `trace_id` | trace id | `"N/A"` |
| `timestamp_ist` | IST timestamp | `"N/A"` |

> **Param-count caveat.** WhatsApp rejects a send whose param count ≠ the
> approved template's `{{n}}` count. We cannot know the approved count from our
> side, so a client mis-editing `param_order` produces a gateway rejection (which
> fail-opens per §7). Rollout (§11) requires verifying `param_order` length
> against the approved template before enabling.

### 4.2 Template body shape (client responsibility)

The approved template carries the static scaffold (emojis, labels, newlines are
allowed in the *body*, not in *parameters*). The recommended **Utility** template
(9 variables, matching the `param_order` below) that reproduces the legacy
free-text notification layout:

```
🔔 [{{1}}] Escalation: {{2}}

👤 {{3}}
📞 {{4}}
🔢 Order ID: {{5}}
🏷️ Team: {{6}}
🕒 Raised at: {{7}} IST

📋 Details:
{{8}}

💬 Recent messages:
{{9}}

— Automated alert from Bloomerce
```

with
`param_order = ["priority","category","customer_name","customer_contact","order_id","escalation_group","timestamp_ist","summary","conversation"]`.

Rules honored: static text precedes the first variable and every pair of
consecutive variables is separated by a label (Meta rejects a body that
starts/ends with a variable or has two adjacent — a newline alone does **not**
separate them). Avoid trailing spaces on variable lines. Urgency is conveyed by
the `priority` param (`{{1}}` → "URGENT"/"Normal"), so a single approved template
serves both normal and immediate-attention escalations — no separate urgent
template required (addresses the lost banner in template mode, review m2).

### 4.3 Per-route template configuration

Template config can be embedded **inside** `escalation_contact` routing nodes
(the same nodes that carry `contacts`), giving per-category, per-agent, or
per-escalation-group control over which template is used. The separate
`escalation_template` config key is retained as the **last fallback** in the
cascade for backward compatibility.

Each `default` / `routes[key]` / `buckets[group]` node gains an optional
`template` block:

```jsonc
// inside client_configs.config_key = 'escalation_contact'
{
  "ESCALATION_ROUTING": {
    "default": {
      "contacts": { "phone": ["9111111111"] },
      "template": {
        "enabled": true,
        "template_id": "default_tmpl_abc",
        "image_url": "https://cdn.example.com/logo.png"  // optional
      }
    },
    "buckets": {
      "pre_sales": {
        "template": {
          "enabled": true,
          "template_id": "presales_tmpl_def"
        }
      }
    },
    "routes": {
      "Order Status Query": {
        "contacts": { "phone": ["9111111113"] },
        "template": {
          "enabled": true,
          "template_id": "order_status_tmpl_jkl"
        }
      },
      "Offline Store Suggestion": {
        "template": { "enabled": false }   // explicit disable — free text only
      }
    }
  }
}
```

> **`param_order` lives in the global `escalation_template` config key**, not
> per-route. All approved templates for a client share the same variable
> structure (same number and order of `{{n}}` placeholders), so `param_order`
> is configured once and inherited by every route-level template.

**Resolution order** — same candidate walk as contact resolution, first match
wins:

1. `routes[category].template`
2. `routes[agent].template`
3. `routes[CATEGORY_TO_AGENT[category]].template`
4. `routes[PARENT_INTENT_TO_AGENT[parent_intent]].template`
5. `buckets[escalation_group].template`
6. `default.template`

If no routing node carries a template block, templates are not used (free-text
only). There is no global fallback for `template_id` — it must come from a
routing node.

Semantics:

- **No `template` block** = not configured at this level; falls through.
- **`template.enabled = false`** = explicit override to disable templates for
  this route. Stops the cascade — no template will be used even if a parent node
  has one.
- **`template.enabled = true`** with a `template_id` = use this template.
- **`param_order` is global, not per-route.** It lives in the
  `escalation_template` config key (§4) and applies to all templates for the
  client. All approved templates share the same variable structure.
- **Contacts and template resolve independently.** A `post_sales` bucket with
  only a `template` block and no `contacts` still uses the template — phones
  come from wherever the contact cascade resolves them.

**Backward compatibility:**

- Existing `escalation_contact` configs without `template` blocks are unchanged
  (free-text only).
- `aget_escalation_contacts` return shape gains an optional `"template"` key;
  existing callers that only access `["phone"]` and `["email"]` are unaffected.

---

## 5. Transports (both supported)

Selected per-client by `whatsapp_api_version` (`utils/whatsapp_api_version.py`):

- **legacy** → raw POST to `api.gupshup.io/wa/api/v1/template/msg` (§6.2).
- **enterprise** (`mediaapi.smsgupshup.com` GatewayAPI) →
  - **media header present** (`image_url` set) → the **verified** media path
    `asend_enterprise_template` (`whatsapp_enterprise_client.py`), which the
    codebase already uses in production for customer templates.
  - **text-only** (no `image_url`) → `asend_enterprise_text_template` (§6.3), a
    body-only HSM that mirrors the media path minus the media header.

**Verification note (enterprise text-only).** The media path is production-proven.
The *text-only* HSM wire values (`method=SendMessage`, `msg_type=HSM`,
`isHSM=true`, `whatsAppTemplateId` + `var1..varN`, no `media_url`) follow Gupshup
enterprise docs and mirror the proven media shape, but this exact body has not
yet been round-tripped through the live gateway in-repo. They are **overridable
per-tenant** (`gupshup_enterprise_details.text_hsm_method` /
`.text_hsm_msg_type`) and should be confirmed via the existing `gant` live
integration test (`tests/test_whatsapp_api_versioning.py::test_live_send_to_gant_with_confirmation`,
extended for a text HSM) before an enterprise client relies on it. The send is
fail-open, so a wrong value degrades to email (today's behavior), never a
regression. **Enterprise clients whose escalation template carries an image
header get fully-verified delivery immediately.**

---

## 6. Implementation

### 6.1 Thread structured fields into the notifier

> **Implementation note.** Rather than have each of the ~10 call sites build and
> pass a `template_fields` dict (several sites share a byte-identical
> `asend_escalation_notification(...)` block, making per-site dict-threading
> error-prone), the shipped code threads the **scalar** fields
> (`details`/`order_id`/`escalation_group`/`customer_contact`/`timestamp_ist`) and
> assembles the dict **once inside** `asend_escalation_notification` via
> `build_escalation_template_fields`. `details` is the opt-in signal (only
> escalations carrying a summary are template-eligible). This keeps
> `build_escalation_template_fields` a single, unit-tested pure function and
> minimizes call-site churn. The original dict-passing sketch below is retained
> for context.

Add an **optional** `template_fields` kwarg to `asend_escalation_notification()`
(`escalation_helper.py:87`) — additive, all existing callers (positional
`notification` + kwargs) keep working:

```python
async def asend_escalation_notification(
    notification, *, client_id, state=None, agent=None, category=None,
    immediate_attention=False, contacts_override=None, email_html=None,
    customer_contact_provided=False,
    template_fields: Optional[Dict[str, Any]] = None,   # NEW
) -> Dict[str, Any]:
```

New sibling helper next to `build_escalation_notification`:

```python
def build_escalation_template_fields(
    *, category, order_id, phone_number, customer_contact, customer_name,
    details, escalation_group, immediate_attention, trace_id, timestamp_ist,
) -> Dict[str, str]:
    """Every canonical key (§4.1) as a sanitized, non-None string."""
```

**No Mode B passthrough** (review M5/M2): template mode is used **only** when
`template_fields` is supplied. Call sites not yet threaded stay on free text.
Full coverage therefore requires threading all ~8 orchestrator sites + the
tool_factory store-visit site (§6.4); they already have the fields in scope.

### 6.2 Dedicated low-level legacy template send (do NOT reuse the Shopify sender)

**Blocker fix (review #2 / m4).** `asend_shopify_gupshup_template_generic`
unconditionally, whenever `client_id` is set:
- writes a `template_delivery` analytics row (`gupshup_template_sender.py:242-266`)
  — polluting *customer* template reporting with staff alerts; and
- on legacy success creates a `template_initiated` **customer conversation keyed
  on the staff phone number** via `aadd_template_to_conversation_safe`
  (`gupshup_template_sender.py:270-281`).

`event_key=None` does **not** suppress either. So we do **not** call it. Instead
add a focused low-level sender that does the raw POST only:

```python
# utils/escalation_template_sender.py
async def asend_legacy_gupshup_template_raw(
    *, to, template_id, params, image_url=None, client_id, trace_id=None,
) -> Optional[dict]:
    """POST api.gupshup.io/wa/api/v1/template/msg with gupshup_template_details
    creds. No template_delivery logging, no conversation creation. Returns the
    gateway JSON on HTTP 200/202, else None."""
```

Body/encoding mirror `gupshup_template_sender.py:201-240` exactly (`template =
{"id": …, "params": […]}`, optional `message` image block, `_encode_form_preserving_percent`).
Staff-alert observability lives in the escalation logs (§9), not the customer
`template_delivery` table.

### 6.3 Enterprise text-template extension (implemented)

Body-only HSM builder/sender siblings to the existing media ones
(`whatsapp_enterprise_client.py`), consistent with the module's "extend with
typed per-variant helpers" model:

```python
def build_text_template_params(*, userid, password, send_to, template_id,
                               params=None, param_order=None,
                               method="SendMessage", msg_type="HSM", msg_id=None):
    # whatsAppTemplateId + var1..varN + isHSM=true, isTemplate=false; NO media_url.

async def asend_enterprise_text_template(destination_phone, template_id, params=None,
        client_id=None, *, param_order=None, msg_id=None, trace_id=None,
        log_tag="ENTERPRISE_WA"):
    # Loads gupshup_enterprise_details (template creds — an HSM authenticates
    # against the template app, not the free-text session app). POSTs (not GETs)
    # so var1..varN customer values stay out of the request URL. method/msg_type
    # overridable via config keys text_hsm_method / text_hsm_msg_type.
```

Routing lives in `_asend_enterprise` (in `escalation_template_sender.py`):
media path when `image_url` is set (verified), else the text HSM path.
Missing/incomplete enterprise creds → `FAILED_PRESEND` (free-text fallback); a
gateway rejection → `GATEWAY_FAILED` (email is the guaranteed channel). Also adds
`var*` masking in the enterprise GET debug log (PII: params carry customer
contact + summary).

### 6.4 Per-recipient send: three-state, no double-send

**Double-alert fix (review M2).** The resolver returns a **three-state** result so
we only fall back to free text when the gateway was **never hit**:

- `NOT_CONFIGURED` — no/disabled config, or enterprise in phase 1 → use free text
  (normal default).
- `SENT` — gateway accepted the template → **done, never also send free text**.
- `FAILED_PRESEND` — deterministic failure *before* any gateway POST (config
  parse, missing creds, param resolution error) → fall back to free text.

A failure *at/after* the gateway POST returns `SENT=False`→ treated as
`GATEWAY_FAILED`: **do not** send free text (avoids the double-alert when the
POST was actually accepted but a later step raised); count as `degraded` and rely
on email.

```python
async def _send_one(num):
    outcome = await amaybe_send_escalation_template(
        to=num, client_id=client_id, template_fields=template_fields,
        immediate_attention=immediate_attention, trace_id=trace_id)
    if outcome == SENT:            return ("sent", num, "template")
    if outcome == GATEWAY_FAILED:  return ("degraded", num, "template")   # email covers it
    # NOT_CONFIGURED or FAILED_PRESEND → free-text default/fallback
    try:
        await send_message(num, notification, trace_id=trace_id, client_id=client_id)
        return ("sent", num, "text")
    except Exception as e:
        logger.error("[ESCALATION_NOTIFY] WhatsApp send failed to %s: %s", num, e)
        return ("failed", num, "text")
```

`amaybe_send_escalation_template` wraps only the **pre-gateway** work in the
try/except that yields `FAILED_PRESEND`; the gateway call's own result maps to
`SENT`/`GATEWAY_FAILED`. Nothing after a successful POST can trigger a fallback.

### 6.5 Parameter sanitization (review M4)

Each template param is sanitized before send:
1. Collapse **all** whitespace runs — newlines, tabs, and 2+ spaces — to a single
   `·` (summary) / space (others). (Not just `\n`.)
2. Strip control chars.
3. Hard-cap length per param (`summary` ≤ ~600 chars, others ≤ ~120), ellipsizing.
4. Never emit `None` (→ `""`).

Adversarial unit tests: `details` containing tabs, 4+ spaces, emoji, `%`, `{`/`}`,
very long text, and a `None` order_id.

### 6.6 Result / telemetry (review M1)

Do **not** count template-configured-but-dropped sends as delivered. The result
dict gains additive, honest status buckets:

```python
{ "sent": [...], "failed": [...],            # sent = actually accepted (template or in-window text)
  "degraded": [...],                          # NEW: template gateway-failed; email is the real channel
  "email_sent": [...], "email_failed": [...], "email_cc": [...],
  "whatsapp_channel": {"<num>": "template"|"text"} }  # NEW
```

Log line: `via_template=<n> via_text=<n> degraded=<n> email_to=<n>`. Reporting
treats **email as the only guaranteed channel**; a `degraded` WhatsApp is a gap,
not a success.

> **`_fanout_whatsapp` unpacking must change** (review m1): it currently unpacks
> 2-tuples (`escalation_helper.py:183`); `_send_one` now returns 3-tuples with a
> `degraded` status. Update the aggregation loop accordingly.

---

## 7. Control flow (per escalation)

```
asend_escalation_notification(notification, client_id, template_fields?, …)
 ├─ resolve staff contacts (unchanged)
 ├─ WhatsApp fan-out (per number, isolated):
 │    _send_one(num) → amaybe_send_escalation_template(...)
 │      ├─ config missing/disabled / enterprise-phase1 ─► NOT_CONFIGURED ─┐
 │      ├─ parse cfg, build+sanitize params (structured only)             │
 │      │    parse/creds/param error (pre-gateway) ─────► FAILED_PRESEND ─┤
 │      ├─ legacy POST /template/msg (raw, no logging/convo)              │
 │      │    accepted ─► SENT (done, NO free text)                        │
 │      │    rejected ─► GATEWAY_FAILED (degraded, NO free text; email)   │
 │      └───────────────────────────────────────────────────────────────┘
 │      NOT_CONFIGURED | FAILED_PRESEND → send_message(num, notification)  ◄ default & fallback
 └─ email fan-out (unchanged, parallel — the guaranteed channel)
```

---

## 8. Backward compatibility & safety

- **Default off** — no `escalation_template` ⇒ byte-for-byte current behavior.
- **Fail-open** (AGENTS.md §11): pre-gateway errors fall back to free text; email
  always fires.
- **Per-recipient isolation** preserved — one number's failure never suppresses
  others.
- **No double-send** — free text is sent only when the gateway was never hit.
- **No new secrets**; additive signature; existing callers unchanged.

---

## 9. Observability & PII (review M6)

- Escalation template sends are logged to the **escalation** log/event stream
  (`publish_escalation_event` metadata), **not** the customer `template_delivery`
  table (that separation is a direct consequence of §6.2).
- **PII hardening.** Template params carry `customer_contact`, `customer_name`,
  `summary`. Today `_aget_gateway` prints/logs the full GET URL with only
  `password` masked (`whatsapp_enterprise_client.py:288-292`), and the legacy path
  logs `response.text` / rendered `template_message`. As part of this work:
  - Mask/omit `var*` values in enterprise debug URL + INFO logs.
  - Use **POST** (not GET) for enterprise template sends so params aren't in the
    URL/proxy access logs (phase 2).
  - The legacy raw sender (§6.2) logs status only, never param values.

---

## 10. Testing

Unit tests (mirroring `tests/test_escalation_routing.py`,
`tests/test_whatsapp_api_versioning.py`):
1. No config / `enabled:false` → `send_message`; template sender not called.
2. Enabled + legacy → raw template POST with correct `template_id` + ordered,
   sanitized params; `send_message` NOT called; **no** `template_delivery` row,
   **no** conversation created.
3. Enabled + **enterprise, no image** → routes to `asend_enterprise_text_template`
   with ordered params; enabled + **enterprise, image_url** → routes to the media
   `asend_enterprise_template`; enterprise **missing creds** → `FAILED_PRESEND`;
   enterprise **gateway reject** → `GATEWAY_FAILED`.
4. Gateway accepts → status `sent`/`template`, **no** free-text second send.
5. Gateway rejects → status `degraded`, **no** free-text second send, email still
   fires.
6. Pre-send error (bad param_order / missing creds) → `FAILED_PRESEND` → free-text
   fallback.
7. Param sanitization: tabs / 4+ spaces / newlines / emoji / `%` / long `summary`
   / `None` order_id — never raises, never emits `None`, respects caps.
8. `immediate_attention` → `priority` param = "URGENT".
9. Multi-recipient isolation: one template failure doesn't block others.
10. `build_text_template_params` wire shape: `SendMessage` / `msg_type=HSM` /
    `isHSM=true` / no `media_url` / `var1..varN`.

---

## 11. Rollout

1. Land code (default off) — no client sees a change.
2. Create + approve a **utility** (not marketing — review m3) escalation HSM in
   Gupshup; note its exact `{{n}}` count. For **enterprise**, either give it an
   **image header** (fully-verified media path) or, for a text-only template,
   run the extended `gant` live test first to confirm `text_hsm_method` /
   `text_hsm_msg_type`.
3. Set `escalation_template` with `param_order` length == the approved count.
4. **Verify with a closed 24h window** (the whole point): staff receives it.
5. Expand per client (legacy or enterprise).

---

## 12. Decisions taken from review (was "open questions")

1. **Mode B passthrough — dropped.** Full-notification-in-one-param risks
   WhatsApp length/format rejection and gives false telemetry; structured-only.
2. **Enterprise — supported** (both transports). Media header ⇒ verified path;
   text-only ⇒ mirrored HSM shape, tenant-overridable, live-test before reliance.
3. **Delivery logging — escalation stream only**, never `template_delivery`.
4. **Fallback — only pre-gateway**, never after a gateway POST (no double-send,
   no false "sent"). Email is the guaranteed channel.
5. **Urgency — via `priority` param**, single template; no separate urgent HSM.

---

## 13. Changelog

**v2 (post-review):** dropped Mode B; deferred enterprise to a gated phase 2;
replaced the Shopify-sender reuse with a dedicated raw legacy sender (no
`template_delivery` / no staff-phone conversations); three-state fallback to
prevent double-alerts and false-success telemetry; added `degraded` status;
mandated full whitespace sanitization + length caps + never-None param contract;
added `priority` param for urgency; added PII log-masking; noted param-count and
utility-category rollout requirements.

**v3 (enterprise support):** implemented the enterprise transport (majority of
clients). Media-header escalation templates use the production-verified
`asend_enterprise_template`; text-only templates use a new
`asend_enterprise_text_template` / `build_text_template_params` (whatsAppTemplateId
+ `var1..varN`, `method=SendMessage` / `msg_type=HSM`, no media), POSTed so
customer values stay out of the URL, with `text_hsm_method` / `text_hsm_msg_type`
per-tenant overrides and `var*` masking in enterprise GET debug logs. Transport
routing (`_asend_enterprise` / `_asend_legacy`) preserves the pre-gateway vs
post-gateway failure distinction so the no-double-send guarantee holds on both
transports. The text-only wire values still need a one-time live-gateway
confirmation (fail-open until then).

**v4 (never-empty params):** every canonical field now renders a non-empty value
(`priority` → "Normal"/"URGENT"; `order_id`/`customer_name`/`trace_id` → "N/A";
`summary` → "No additional details"). WhatsApp/Meta rejects a send with a blank
parameter, so this lets an approved template safely include optional fields
(dedicated Order ID / Urgency lines) without blank-parameter rejections on
web-chat / non-urgent / no-order escalations.

**v5 (name + conversation parity):** added `customer_name` and a `conversation`
field (last ≤3 customer messages, derived from `state` so no call site is
re-threaded, joined ` / ` and capped ~400 chars, → `"N/A"` when none) so a
template can reproduce the old free-text notification's Name line and 💬 Customer
Conversation block. Recommended template grows to 9 variables.

**v6 (per-route templates):** template config is now embedded inside
`escalation_contact` routing nodes (`default`, `routes[key]`, `buckets[group]`)
— each route specifies `enabled`, `template_id`, and optionally `image_url`.
Resolution follows the same cascade as contact resolution (category → agent →
mapped agent → intent agent → bucket → default), with `enabled: false` as an
explicit disable that stops the cascade. The `escalation_template` config key
is simplified to carry only `param_order` (shared across all templates for a
client — all approved templates use the same variable structure); it no longer
carries `enabled`, `template_id`, or `image_url`. If no routing node has a
template block, templates are not used (no global fallback).
`aget_escalation_contacts` return shape gains a `"template"` key;
`amaybe_send_escalation_template` accepts a `route_template` parameter. See §4.3.

**v7 (store-visit template config):** store-visit (offline store recommendation)
escalation notifications now support template delivery via the `store_locations`
config key. The config uses a dict shape with top-level `template_enabled_for_notification`
and `template_id` fields (plus optional `image_url`):

```json
{
  "template_enabled_for_notification": true,
  "template_id": "1665491144557994",
  "image_url": "https://cdn.example.com/logo.png",
  "locations": [ ... ]
}
```

`aget_store_notification_template(client_id)` returns a `{"template_id": ..., "image_url": ...}`
dict when both the flag is truthy and a `template_id` is present; `None` otherwise.
`aget_store_visit_contacts` sources the template from this config (not from `escalation_contact`
routing nodes), so the store notification template is self-contained alongside the store data.
`param_order` still comes from the global `escalation_template` config key. Legacy bare-list
`store_locations` configs continue to work (no template, free-text delivery).
