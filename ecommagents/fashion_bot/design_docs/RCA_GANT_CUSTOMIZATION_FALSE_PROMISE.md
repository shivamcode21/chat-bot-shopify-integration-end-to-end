# RCA — GANT bot promised a trouser alteration service that GANT does not offer

**Severity:** High (customer-facing false commercial promise, multi-tenant blast radius)
**Date of incident:** 2026-08-06 05:57:34 UTC
**Channel:** web-chat (Shopify preview store, widget "Ask GANT AI")
**Client:** GANT — `f5a737a7-c274-48d3-a6d2-d067f14b755b`
**Conversation:** `9aa64ec6-17d9-4262-a711-77ef9e86efad`
**LangSmith trace:** `019fd5a5-f886-7071-b1bb-67e03d941f5b` (project `fashion-bot-general-prod`)
**Internal trace id:** `0a4004ad` · **Release:** `dee8b27`

---

## 1. What happened

Customer asked: *"What is MF length is too big"*. The bot replied:

> If the length is too big, don't worry! We offer an alteration service to shorten your trousers to the perfect length for you.
> * **Shortening:** If the trousers are too long, we can professionally shorten them to your desired measurement. This requires the order to be **prepaid**.
> * **Lengthening:** Unfortunately, we cannot lengthen trousers if they are too short.

GANT does not offer this service. The authoritative config says so:

```sql
-- client_configs, id=1015, client_id=f5a737a7-… (GANT), config_key='customization_policy'
{"Do you support customization?": "No we do not support personal customization",
 "Do you support size alteration?": "No"}
```

The reply is also not a generic hallucination — it is a near-verbatim restatement of a **different tenant's** policy. Concept Groove (`c3ffcb1b-…`, the Groovee reference client) is the only client in `client_configs` whose policy is "shortening only, prepaid required, no lengthening" (`client_configs` id=32).

---

## 2. Root cause

**GANT's `product_details_handler` system prompt hard-codes Concept Groove's alteration policy as a business rule.** The DB config was never consulted, because the prompt already contained an answer.

`agents_config` where `client_id='f5a737a7-…'` and `agent_name='product_details_handler'`, in the `[WORKFLOW] → Customization/alteration` block:

```
Customization/alteration:
  Fetch customization/alteration policy.
  Determine logic:
    SHORTENING (final length smaller than current) → usually possible; mention prepaid requirement.
    LENGTHENING (final length larger than current) → not possible.
    If unclear, ask for desired final length.
```

Those two branches *are* Concept Groove's policy, frozen into prose. The model followed them exactly — including the "prepaid" clause, which appears nowhere except Concept Groove's config. `client_configs` was correct and was simply outranked by the prompt.

**A second, worse copy sat in the same prompt.** The `[EXAMPLES]` section carried a few-shot demonstration of the exact false answer:

```
Customization (shortening):
Customer: "Can I shorten this from 42 inches to 39 inches?"
Response: "Yes, we can shorten the length by 3 inches! This would require a prepaid order.
           Our team will handle the alteration before shipping. 😊"
Customization (lengthening):
Customer: "Can you make it longer?"
Response: "We can only shorten products, not lengthen them. However, we can help with
           other alterations. What would you like to adjust? 😊"
```

This was found during remediation, not initial analysis. It is more dangerous than the workflow rule: it *shows* the model the wrong answer rather than describing a policy, and the shipped reply mirrors its structure (shortening yes / prepaid / lengthening no). It was present in 19 of the 20 affected clients.

### Origin: contaminated onboarding clone, never corrected

`agents_config_history` for GANT / `product_details_handler` shows the defect was present from birth:

| version | updated_at | created_by | has "SHORTENING" rule |
|---|---|---|---|
| 2026-06-13-v1 | 2026-06-19 | `onboarding_system` | ✅ yes |
| 2026-06-19-v1 → 2026-07-30 (5 revisions) | … | `f4bb36a3-…` (human) | ✅ yes |
| live (2026-08-01 04:44:55) | — | — | ✅ yes |

GANT's prompts were cloned from the Groovee/Concept Groove reference template at onboarding (per `AGENTS.md`: *"The reference/template client (`c3ffcb1b-…`, Groovee) is copied from, never written to, during onboarding"*). Brand-surface strings were rebranded (`GANT`, `instagram.com/gant/`) but **business logic in the prompt body was not reviewed against GANT's own config.** Six subsequent edits, five by a human, never caught it.

Independent proof the clone was only partially rebranded — GANT's `recommendations_handler` prompt still ships Concept Groove's assets:

- `https://www.instagram.com/cncptgroove?igsh=…` presented as GANT's Instagram
- Worked examples using `https://groovee.in/products/groovee-hoodie`, "The Groovee Hoodie — ₹2,499"

---

## 3. Why the correct config never got a chance

The tool exists and is correctly wired. `get_customization_config` is registered for the product-details agent (`tool_factory.py:5007`) and reads the right row per tenant (`orchestrator.py:6395` → `config_manager.aget_config`). `aget_config` is also *not* the culprit — it explicitly refuses cross-tenant fallback:

```python
# config_manager.py:104
if not effective_client_id:
    logger.warning("aget_config called without client_id for key=%s; skipping cross-tenant fallback", config_key)
    return None
```

**Tenant resolution was correct.** Every span in the trace carries `client_id=f5a737a7-…` (GANT). This was not a client-mixup bug.

**The tool was simply never called.** The full LangSmith trace for the turn contains exactly one LLM call and zero tool runs:

```
general-streaming-conversation (root, 2.94s)
└─ LangGraph
   ├─ conversation_limit_gate
   ├─ detect_intent → ChatOpenAI → {"p":"SD","i":"product_details"}
   └─ product_details_intent
      ├─ generic_skill.fetch_prompt (prompt_name=product_details_handler)
      ├─ generic_skill.load_tools
      └─ generic_skill.create_agent → LangGraph → model → ChatOpenAI  ← answered here, no tool call
```

Grafana Loki confirms from the runtime side:

```
[TRACE_ID=0a4004ad] 🧮 runtime_metrics redis_calls=0 db_calls=1 elapsed_ms=2926
```

One DB call (the transcript write). No config read, no cache read.

### Contributing factors

1. **Soft instruction vs. hard instruction.** The prompt says "Fetch customization/alteration policy" in plain text, while genuinely enforced fetches in the same prompt use emphatic markers — e.g. *"🔴 EXCEPTION 2 (MANDATORY TOOL FETCH) … you MUST call `find_product_by_id` … NEVER answer 'not specified' without first attempting a tool call"* for fabric and size charts. Customization got no such treatment. With a complete answer already in-prompt, calling the tool had no marginal value to the model.
2. **Weak model + very large prompt.** `google/gemini-3.1-flash-lite-preview` at `temperature=0.7`, 28,286 input tokens (33,491-char system prompt), only 12,231 tokens served from cache. A lite-tier model is the worst case for "ignore the convenient inline answer and call a tool anyway".
3. **Self-reinforcing context.** One turn earlier (05:56:25, *"What is inseam"*) the bot had already volunteered *"If you find they are too long for your height, you can easily have them shortened to fit you perfectly!"* — also with no tool call. That claim sat in conversation history and corroborated the final answer.
4. **No authority ordering.** Nothing in the runtime treats `client_configs` as outranking prompt text. Even if the tool *had* returned `"No"`, the prompt's explicit `SHORTENING → usually possible` branch would have directly contradicted it, and conflict resolution would have been left to a lite model's judgment.
5. **Weak failure signal in the tool itself.** `orchestrator.py:6404-6410` returns `{"success": True, "policy": {"message": "Customization policy not found"}}` — a missing policy is reported as success, so a caller cannot distinguish "no policy" from "policy retrieved".

---

## 4. Blast radius

This is not GANT-only. **20 clients** currently run a `product_details_handler` containing the `SHORTENING … LENGTHENING` rule, 18 of them stamped with an identical `updated_at` of `2026-08-01T04:44:55.452Z` — a bulk template propagation:

| Client | Prompt says shortening OK | `client_configs` actually says | Status |
|---|---|---|---|
| **GANT** | ✅ | "No we do not support personal customization" / "No" | ❌ **contradiction** |
| **IconicIndia** | ✅ | "No" / "No" | ❌ **contradiction** |
| **Vahro** | ✅ | "No" / "No" | ❌ **contradiction** |
| **Little Igloo** | ✅ | "No we do not support customization…" / "We do not support personal alteration…" | ❌ **contradiction** |
| Concept Groove | ✅ | shortening-only, prepaid | ✅ correct (source of truth) |
| Acchao, Amydus, BAS Motorcycles, Casence, Krvvy, Levis, Lifelong, Mufti, Nalli, Posh Affair, Pothys, Prathaa, Rare Rabbit, True Religion, "Do not delete" | ✅ | **empty string** (unset) | ⚠️ unverified — bot will promise alterations by default |

Separately, **8 non-Groovee clients** had Concept Groove brand assets leaking in their prompts — Casence, GANT, Krvvy, Lifelong, Little Igloo, Mufti, True Religion, "Do not delete":

- `https://www.instagram.com/cncptgroove?igsh=…` presented as the client's own Instagram (7 clients × 2 occurrences: one workflow instruction, one worked example)
- `groovee.in` product URLs in `[EXAMPLES]` (GANT, Mufti, "Do not delete" — 9 occurrences each)
- Groovee product names ("The Groovee Hoodie", "The Groovee Denim")

Sending a GANT customer to a different brand's Instagram or storefront is a second, independent cross-tenant leak. Note: `+918607845846` and `admin@groovee.in` were checked and did **not** leak — those appear only in Concept Groove's own rows.

---

## 5. Remediation applied (2026-08-06)

Steps 1–5 below are **done**. DB changes are replayable from
`scripts/rca_gant_customization_fix_20260806.sql`; the pre-change snapshot is
`agents_config_backup_rca_20260806` (602 rows) and every touched row is stamped
`created_by='rca_gant_customization_20260806'`.

| # | Change | Result |
|---|---|---|
| 1 | Hard-coded `SHORTENING/LENGTHENING` verdicts replaced with config-driven instruction | 20/20 rows, 0 remaining |
| 1b | False `[EXAMPLES]` few-shot replaced with a policy-driven example | 19/19 rows, 0 remaining |
| 2 | Cross-tenant Groovee assets stripped (domains remapped to each client's own `clients.domain`; GANT repointed to `instagram.com/gant/`; handles with no known replacement removed rather than guessed) | 0 leaks outside Concept Groove |
| 4 | Customization fetch marked `🔴 MANDATORY TOOL FETCH`, matching the convention already used for fabric and size chart | folded into step 1 |
| 5 | `get_customization_config` now returns `success=False` / `policy_found=False` on a missing **or blank** policy | `orchestrator.py`, `tool_factory.py` |
| 3 | 15 blank `customization_policy` rows set to a conservative "No" | every client that handles customization now has a populated policy |

Applied to all 20 clients carrying the defect, not just the 4 with an actively
contradicted policy — including Concept Groove itself, so the next onboarding
clone inherits the safe version. Concept Groove's behaviour is unchanged: its
`client_configs` row is populated, so the tool now supplies at answer time what
the prompt used to assert.

Step 5 also closes the blank-policy hole at the code level: `{"Do you support
customization?": "", "Do you support size alteration?": ""}` is a truthy dict
but says nothing, and used to be reported as a successful fetch. It now reports
`policy_found=False`, which the new prompt routes to "promise nothing, escalate".

Step 3 then populated those rows. The 15 clients whose `product_details_handler`
actually handles customization (Acchao, Amydus, BAS Motorcycles, Casence, Krvvy,
Levis, Lifelong, Mufti, Nalli, Posh Affair, Pothys, Prathaa, Rare Rabbit, True
Religion, "Do not delete") were set to a conservative `"No"` / `"No"`.

> **Open risk, accepted deliberately.** These values were not sourced from the
> brands — the real per-client policy was unavailable, and a conservative "No"
> was chosen over leaving the rows blank. Any of these 15 that *does* offer
> alterations will now wrongly deny them. That is a wrong answer rather than a
> deferred one, and nothing will surface it automatically. **Client success
> should confirm all 15 and correct any that are wrong.**

The other 8 blank clients — Fitflop, Forest Essentials, Kerala Ayurvedic, Love
Beauty Planet, Manyavar, Mochi Shoes, Tattvalogy, Underneat — were left blank on
purpose: their prompts do not handle customization at all, so there is nothing
to answer and no false-promise surface.

**Cache:** prompts live only in Redis under `agents_config:{client_id}` with a
600s TTL and no in-memory tier, so the prompt rewrite propagates within 10
minutes with no action needed. Config values are cached under
`cfg:{client_id}:{key}` at a 3600s Redis TTL plus a 600s in-memory tier, so the
step-3 policy writes take up to an hour to land; until then those clients serve
the cached blank value and escalate, which is safe. Redis was not reachable from
the remediation environment, so no explicit bust was issued.

### Remaining

**Short term:**
6. **Confirm the 15 conservative "No" values with client success** and correct any brand that actually offers alterations (see the accepted-risk note above).
7. **Verify the fix on the original conversation** by replaying *"What is MF length is too big"* for GANT once the 600s prompt cache expires, and confirm the trace now contains a `get_customization_config` tool run.

**Systemic:**
8. **Onboarding lint:** block/flag any cloned prompt whose body contains another tenant's domain, handle, phone, email, or brand name, and any prompt asserting a policy verdict that a `client_configs` key is supposed to own. The Aug-1 bulk push shipped this to 18 clients at once with no such gate. Note that the `[EXAMPLES]` section needs linting as hard as the `[WORKFLOW]` section — the worse copy of this defect lived in a few-shot.
9. **Policy-conflict test:** add a per-client regression asserting the customization answer matches `client_configs.customization_policy` — the same shape as the existing `test_product_details_tools.py::TestGetCustomizationConfig` suite, but asserting on the *final agent reply*, not just the tool return. The existing suite passed throughout this incident because it only ever asserted on the tool, which was never called.
10. **Model tier:** re-evaluate `gemini-3.1-flash-lite-preview` for the product-details agent. A 33k-char prompt with conditional tool-calling obligations is beyond what a lite model reliably honors.
11. Consider injecting the resolved customization policy into the prompt at render time (as data), so there is exactly one place the policy can come from and no opportunity for prompt text to disagree with config.

---

## 6. Evidence index

| Claim | Source |
|---|---|
| GANT policy is "No" | `client_configs` id=1015 |
| Reply matches Concept Groove policy | `client_configs` id=32 |
| Prompt hard-codes the shortening rule | `agents_config` (GANT, `product_details_handler`), offset ~3480 |
| Defect present at onboarding | `agents_config_history`, version `2026-06-13-v1`, `created_by='onboarding_system'` |
| Zero tool calls in the turn | LangSmith trace `019fd5a5-f886-7071-b1bb-67e03d941f5b` |
| One DB call total | Loki: `[TRACE_ID=0a4004ad] 🧮 runtime_metrics redis_calls=0 db_calls=1` |
| Correct tenant throughout | `client_id=f5a737a7-…` on every span |
| No cross-tenant config fallback in code | `fashion_bot/config_manager.py:104` |
| Bulk propagation | 18 rows sharing `updated_at='2026-08-01T04:44:55.452Z'` |
