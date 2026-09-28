# Agent Testing Architecture

## Overview

A 5-layer testing framework for LLM-based e-commerce agents that handles non-deterministic outputs, prompt changes, tool call verification, regression detection, and pre-deployment gating.

**Client tested:** Casence (`81e80e20-fe91-470a-ab3d-e9dfc2eebf4a`)  
**Agent tested:** `cancellation_handler` (cancel_or_update_order)  
**First run results:** 5/8 passed (62.5%) — **3 real issues caught**

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                     run_agent_tests.py (CLI)                        │
│  --extract-contracts | --regression | --gate | --save-baseline      │
└────────────┬────────────────────┬───────────────────┬───────────────┘
             │                    │                   │
    ┌────────▼────────┐  ┌───────▼────────┐  ┌───────▼───────────┐
    │  Layer 1:        │  │  Layer 3:       │  │  Layer 5:          │
    │  Prompt Contract │  │  Semantic       │  │  Pre-Deployment    │
    │  Extractor       │  │  Scenario Tests │  │  Gate              │
    │                  │  │  (LLM-as-Judge) │  │                    │
    │  • Auto-extract  │  │                 │  │  • Pass/fail       │
    │    65 contracts   │  │  • Base eval    │  │    thresholds      │
    │  • Generate 15   │  │    (8 criteria) │  │  • Regression      │
    │    test scenarios │  │  • Context eval │  │    check           │
    └──────────────────┘  │    (5 criteria) │  │  • Exit code 0/1   │
                          └────────┬────────┘  └───────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     ┌────────▼────────┐  ┌───────▼────────┐  ┌───────▼───────────┐
     │  Layer 0:        │  │  Layer 2:       │  │  Layer 4:          │
     │  Fixtures &      │  │  Tool Call      │  │  Regression        │
     │  State Seeding   │  │  Verifier       │  │  Snapshots         │
     │                  │  │                 │  │                    │
     │  • Client profs  │  │  • Expected     │  │  • Save baselines  │
     │  • State fixtures│  │    tools        │  │  • Compare scores  │
     │  • Template vars │  │  • Forbidden    │  │  • Detect drops    │
     │  • Message       │  │    tools        │  │  • Response diffs  │
     │    reconstruction│  │  • Arg matching │  │                    │
     └─────────────────┘  └────────────────┘  └───────────────────┘
```

---

## Layer 0: Fixtures & State Seeding

### What it does
Seeds the conversation graph with a **pre-built state** before the customer message is sent. This lets us test mid-conversation scenarios (not just cold starts) and verify the agent uses existing context correctly.

### Files

| File | Purpose |
|------|---------|
| `tests/fixture_loader.py` | Loads client profiles, resolves `{{client_id}}` templates, reconstructs LangChain `HumanMessage`/`AIMessage` objects from JSON |
| `tests/fixtures/clients/casence.json` | Casence client profile — UUID, expected behaviors, 9 agent names |
| `tests/fixtures/clients/groovee.json` | Groovee client profile (same structure) |
| `tests/fixtures/states/cold_start.json` | Empty state — tests agent from scratch |
| `tests/fixtures/states/mid_cancellation_flow.json` | 4 messages in history, order GV12345 confirmed, `waiting_for_cancellation_reason=True` |
| `tests/fixtures/states/identified_user_with_orders.json` | Phone `9876543210` known, 2 orders fetched (GV12345 shipped, GV12346 new) |
| `tests/fixtures/states/product_page_context.json` | User on product page — `page_context` set to Sunfire Denim |
| `tests/fixtures/states/cross_topic_switch.json` | Was in product inquiry, conversation_context has `topic=product_inquiry` |

### How client_id resolves
```
1. TEST_CASENCE_CLIENT_ID env var (if set)
2. client_id_fallback from fixture JSON → "81e80e20-fe91-470a-ab3d-e9dfc2eebf4a"
3. Live DB lookup via gupshup_source (if env var + fallback missing)
4. test-casence-default (last resort)
```

### Template resolution
State fixtures use `{{client_id}}` which gets replaced at load time:
```json
{ "client_id": "{{client_id}}" }  →  { "client_id": "81e80e20-fe91-470a-ab3d-e9dfc2eebf4a" }
```

### Message reconstruction
JSON messages are converted to LangChain objects:
```json
{"role": "human", "content": "I want to cancel"}  →  HumanMessage(content="I want to cancel")
{"role": "ai", "content": "Which order?"}          →  AIMessage(content="Which order?")
```

### Key design decisions
- **State fixtures are reusable across agents** — the same `identified_user_with_orders` fixture works for cancellation, order status, return/exchange tests
- **Overrides are composable** — a scenario can use a fixture + inline `initial_state_overrides` to customize (e.g., add `selected_order_id` on top of `cold_start`)
- **Client profiles separate config from test data** — adding a new client = one JSON file

---

## Layer 1: Prompt Contract Extraction

### What it does
Uses an LLM to automatically read an agent prompt and extract **testable behavioral contracts** — rules the agent MUST follow or MUST NOT violate. Then generates test scenarios from those contracts.

### Class: `PromptContractExtractor` (in `agent_test_runner.py`)

### How it works
```
Agent Prompt (13,135 chars)
       │
       ▼
  LLM Analysis (gemini-2.5-flash-lite)
       │
       ▼
  65 Contracts extracted:
    ├── 21 must_do (critical/high)
    ├── 13 must_not_do
    ├── 18 conditional_behavior
    ├── 5 escalation_rules
    └── 8 data_handling
       │
       ▼
  Top 15 critical/high contracts
       │
       ▼
  LLM generates 15 test scenarios
       │
       ▼
  Saved to: cancellation_handler_generated_scenarios.json
```

### Contract format
```json
{
    "category": "must_not_do",
    "rule": "If the customer persists in cancelling, you MUST NOT cancel the order in Shopify or Shiprocket.",
    "priority": "critical",
    "test_hint": "Send persistent cancellation request and verify escalation instead of direct cancel"
}
```

### Contract categories
| Category | Count | Description |
|----------|-------|-------------|
| `must_do` | 21 | Actions the agent MUST always perform |
| `must_not_do` | 13 | Actions the agent must NEVER perform |
| `conditional_behavior` | 18 | If X then Y rules |
| `escalation_rules` | 5 | When to escalate to human |
| `data_handling` | 8 | What data to collect/auto-fill |

### Why this matters
- **Prompt changes → auto-updated tests**: Change the prompt, re-run `--extract-contracts`, get new contracts + scenarios
- **No hardcoding**: The LLM reads the prompt and extracts rules — you don't manually write assertions
- **Priority ordering**: Critical contracts are tested first

### CLI usage
```bash
# Extract contracts from the DB-stored prompt
python -m tests.run_agent_tests --extract-contracts --agent cancellation_handler --client casence

# Extract from a file
python -m tests.run_agent_tests --extract-contracts --agent cancellation_handler --prompt-file my_prompt.txt
```

---

## Layer 2: Tool Call Verification

### What it does
Verifies that the agent called the **correct tools** with **correct arguments** and did NOT call **forbidden tools**.

### Class: `ToolCallVerifier` (in `agent_test_runner.py`)

### Expectation format
```json
{
    "expected_tools": ["fetch_orders_by_phone", "store_cancellation_reason"],
    "forbidden_tools": ["cancel_order_in_shopify_tool", "cancel_order_in_shiprocket_tool"],
    "expected_tool_args": {
        "fetch_orders_by_phone": {"phone": "9876543210"}
    }
}
```

### What the first run caught
In `ctx-2-cancellation-mid-flow-reason`, the agent called:
1. `store_cancellation_reason` with `{reason_category: "wrong_size"}` ✅
2. `cancel_order_in_shopify_tool` with `{order_id: "GV12345"}` 🔴 **VIOLATION**
3. `cancel_order_in_shiprocket_tool` with `{order_id: "GV12345"}` 🔴 **VIOLATION**

The prompt explicitly says: *"If the customer persists in cancelling, you MUST NOT cancel the order in Shopify or Shiprocket."*

### Current state
Tool call verification currently inspects the `conversation_context.recent_actions` from the final state. Full tool-call-level interception (capturing every tool invocation with arguments) would require wrapping the tool registry — this is a planned enhancement.

---

## Layer 3: Semantic Scenario Tests (LLM-as-Judge)

### What it does
Runs multi-turn conversations through the **real graph** (not mocked) and evaluates responses using **two LLM judges**: a base quality judge and a context-awareness judge.

### Class: `ContextAwareTestRunner` (in `agent_test_runner.py`)

### Dual evaluation

**Base Evaluation (8 criteria):**
| Criterion | Weight | What it checks |
|-----------|--------|---------------|
| Logical Correctness | – | Responses accurately address questions |
| Conciseness | – | Brief and to the point |
| Helpfulness | – | Actionable info or clear next steps |
| Tone | – | Professional, empathetic, warm |
| Completeness | – | All aspects addressed |
| Context Awareness | – | Appropriate for conversation flow |
| Conversation Flow | – | Natural, builds on previous exchanges |
| Memory Retention | – | Remembers info from previous messages |

**Context Evaluation (5 criteria):**
| Criterion | Weight | What it checks |
|-----------|--------|---------------|
| Context Utilization | 20% | Did the bot USE pre-seeded context? |
| No Redundant Questions | 20% | Did it avoid asking for known info? |
| Flow Continuity | 20% | Did it treat message as continuation when `waiting_for_*` was set? |
| Correct Routing | 20% | Was it handled by the right agent? |
| Prompt Compliance | 20% | Did it follow specific behavioral rules? |

### Scoring formula
```
combined_score = (base_score × 0.5) + (context_score × 0.5)
pass = combined_score >= 3.0
```

### Test scenario format
```json
{
    "id": "ctx-2-cancellation-mid-flow-reason",
    "description": "User provides cancellation reason when agent is waiting for it",
    "client_name": "casence",
    "initial_state_fixture": "mid_cancellation_flow",
    "target_agent": "cancel_or_update_order",
    "context_assertions": {
        "should_not_ask_for": ["phone_number", "order_id"],
        "should_use_context": ["GV12345"],
        "expected_behaviors": [
            "Should treat 'wrong size' as cancellation REASON not size change request",
            "Should NOT cancel directly - must escalate"
        ]
    },
    "conversation": [
        {"role": "customer", "message": "wrong size ordered"},
        {"role": "bot", "expected_behavior": "Should escalate, NOT cancel directly", "expected_keywords": ["support"]}
    ]
}
```

### Full suite results (Casence, 8 scenarios)
| Test | Fixture | Combined | Status |
|------|---------|----------|--------|
| ctx-1 Cold start cancellation | cold_start | 4.2/5 | ✅ PASS |
| ctx-2 Mid-flow reason | mid_cancellation_flow | 1.4/5 | ❌ FAIL |
| ctx-3 Product page context | product_page_context | 1.9/5 | ❌ FAIL |
| ctx-4 No repeat phone | identified_user_with_orders | 4.4/5 | ✅ PASS |
| ctx-5 Cross-topic switch | cross_topic_switch | 4.2/5 | ✅ PASS |
| ctx-6 Size → offer fix first | cold_start + overrides | 3.0/5 | ✅ PASS |
| ctx-7 Address no ask name | cold_start + overrides | 2.5/5 | ❌ FAIL |
| ctx-8 Multi-order selection | identified_user_with_orders | 3.05/5 | ✅ PASS |

### Issues caught
1. **ctx-2 FAIL**: Bot called `cancel_order_in_shopify_tool` directly instead of escalating. Ignored `waiting_for_cancellation_reason=True` flag.
2. **ctx-3 FAIL**: Bot asked "which product?" despite `page_context` having Sunfire Denim. Graph doesn't pass page_context from fixture into the skill node — state seeding gap.
3. **ctx-7 FAIL**: Bot couldn't find mock order GV12345 in real Shopify/Shiprocket — fixture orders don't exist in live systems. Expected behavior: tools hit real APIs.

---

## Layer 4: Regression Snapshots

### What it does
Saves a snapshot of each test run (scores, bot responses, state mutations) as a baseline. On subsequent runs, compares with the baseline to detect **regressions**.

### Class: `RegressionSnapshotManager` (in `agent_test_runner.py`)

### Snapshot contents
```json
{
    "scenario_id": "ctx-4-identified-user-no-repeat-phone",
    "timestamp": "2026-02-16T10:26:14",
    "conversation_length": 1,
    "passed": true,
    "combined_score": 4.4,
    "bot_responses": ["I understand you want to cancel order GV12346..."],
    "state_mutations": [...],
    "tool_calls_log": [],
    "final_state_keys": ["messages", "phone_number", "client_id", ...]
}
```

### Regression detection
```
score_change = current_score - baseline_score

🔴 REGRESSION if:
  - score_change < -0.5 (significant drop)
  - baseline passed=True → current passed=False

✅ NO REGRESSION if:
  - score_change >= -0.5
  - pass/fail didn't change
```

### Saved baselines
```
tests/regression_snapshots/
├── ctx-1-cancellation-cold-start.json
├── ctx-2-cancellation-mid-flow-reason.json
├── ctx-3-product-page-context-inquiry.json
├── ctx-4-identified-user-no-repeat-phone.json
├── ctx-5-cross-topic-product-to-cancel.json
├── ctx-6-cancellation-size-issue-should-offer-fix.json
├── ctx-7-address-update-no-ask-name.json
├── ctx-8-multi-order-selection.json
└── cancellation_handler-must_not_do-3.json
```

### CLI usage
```bash
# Run with regression comparison
python -m tests.run_agent_tests --regression

# Save current results as new baselines
python -m tests.run_agent_tests --save-baseline

# Both: compare + save new baselines where none exist
python -m tests.run_agent_tests --regression --save-baseline
```

---

## Layer 5: Pre-Deployment Gate

### What it does
Binary pass/fail verdict for CI/CD. Returns exit code 0 (deploy) or 1 (don't deploy).

### Checks
1. **Pass rate** ≥ 60% (configurable in `context_test_config.py`)
2. **No regressions** detected (if `--regression` flag used)
3. **No critical failures** (tests with errors)

### CLI usage
```bash
# Gate check (exits 0 or 1)
python -m tests.run_agent_tests --gate

# Combine with regression
python -m tests.run_agent_tests --gate --regression
```

### CI/CD integration example
```yaml
# GitHub Actions
- name: Run Agent Tests
  run: python -m tests.run_agent_tests --gate --regression
  # Step fails if gate doesn't pass
```

---

## CLI Reference

```bash
# List everything available
python -m tests.run_agent_tests --list

# Run all 8 context-aware tests
python -m tests.run_agent_tests

# Run a specific test
python -m tests.run_agent_tests --test-id ctx-2-cancellation-mid-flow-reason

# Run for a specific client only
python -m tests.run_agent_tests --client casence

# Extract prompt contracts + generate scenarios
python -m tests.run_agent_tests --extract-contracts --agent cancellation_handler

# Run auto-generated contract tests
python -m tests.run_agent_tests --scenarios-file tests/agent_test_reports/cancellation_handler_generated_scenarios.json

# Run with regression + save baselines
python -m tests.run_agent_tests --regression --save-baseline

# Pre-deployment gate
python -m tests.run_agent_tests --gate

# Custom output directory
python -m tests.run_agent_tests --output-dir my_reports/
```

---

## File Structure

```
tests/
├── AGENT_TESTING_ARCHITECTURE.md     ← This file
│
├── fixtures/
│   ├── __init__.py
│   ├── clients/
│   │   ├── casence.json              ← Client profile (UUID, behaviors, agents)
│   │   └── groovee.json
│   └── states/
│       ├── cold_start.json           ← Empty state
│       ├── mid_cancellation_flow.json ← 4 messages, waiting for reason
│       ├── identified_user_with_orders.json  ← Phone + 2 orders
│       ├── product_page_context.json  ← On product page
│       └── cross_topic_switch.json    ← Topic switching
│
├── fixture_loader.py                  ← Loads fixtures, resolves templates
├── context_test_config.py             ← Context evaluation criteria + thresholds
├── context_test_scenarios.json        ← 8 hand-crafted test scenarios
├── agent_test_runner.py               ← Core engine (all layers)
├── run_agent_tests.py                 ← CLI entry point
│
├── regression_snapshots/              ← Baselines (auto-generated)
│   ├── ctx-1-cancellation-cold-start.json
│   ├── ctx-2-cancellation-mid-flow-reason.json
│   └── ... (9 baselines saved)
│
├── agent_test_reports/                ← Reports (auto-generated)
│   ├── agent_test_report.txt          ← Human-readable report
│   ├── agent_test_results.json        ← Full results JSON
│   ├── summary.json                   ← Pass/fail summary
│   ├── cancellation_handler_contracts.json      ← 65 extracted contracts
│   └── cancellation_handler_generated_scenarios.json  ← 15 auto-generated tests
│
├── test_config.py                     ← Base eval criteria (existing)
├── test_fashion_bot.py                ← Original test runner (existing)
├── smoke_tests_data.json              ← Original smoke tests (existing)
└── env_utils.py                       ← Environment setup (existing)
```

---

## How the Layers Connect

```
Prompt changes  ──→  Layer 1 extracts new contracts  ──→  Generates new test scenarios
                                                              │
Code/tool changes  ──→  Layer 3 runs scenarios through real graph
                                                              │
                         Layer 2 checks tool calls were correct
                                                              │
                         Layer 4 compares with saved baselines
                                                              │
                         Layer 5 gives pass/fail gate verdict
```

### Workflow for prompt changes
1. Change the prompt in DB
2. Run `--extract-contracts --agent cancellation_handler` → new contracts + scenarios
3. Run the generated scenarios → see if the prompt change broke or improved behavior
4. If passing, `--save-baseline` to update baselines

### Workflow for code/tool changes
1. Make code changes
2. Run `--regression` → compare with saved baselines
3. If regression detected → fix before deploying
4. If passing, `--save-baseline` to update

### Workflow for pre-deployment
1. Run `--gate --regression` in CI
2. Exit code 0 → safe to deploy
3. Exit code 1 → block deployment

---

## Known Limitations & Discussion Points

### 1. Mock orders hit real APIs
State fixtures contain mock order IDs (GV12345) but the graph invokes real Shopify/Shiprocket APIs. These orders don't exist, so tool calls return "not found". This affects tests that depend on order operations succeeding.

**Options to discuss:**
- A) Use real test order IDs from Casence's Shopify
- B) Add a tool-mocking layer that intercepts tool calls in test mode
- C) Accept "not found" as valid — the test evaluates behavioral compliance, not data accuracy

### 2. State seeding vs. graph checkpointing
Fixtures seed the initial state dict, but the LangGraph checkpointer may not see this state as "real" history. For example, `page_context` in the fixture may not survive into the skill node if the graph reconstructs state from checkpoints.

**Options to discuss:**
- A) Bypass checkpointing in test mode
- B) Write fixture state into Redis before test
- C) Feed fixture state through graph's state merge mechanism

### 3. LLM-as-Judge variability
The judge itself is an LLM (gemini-2.5-flash-lite) and its scores can vary ±0.5 between runs for the same conversation. The 3.0/5 pass threshold accounts for this.

**Options to discuss:**
- A) Run each test 3 times and average scores
- B) Use a more deterministic judge model (temperature=0 already set)
- C) Accept variance and only flag >1.0 drops as regressions

### 4. Contract extraction completeness
The extractor found 65 contracts from the cancellation prompt, but some are duplicates or near-duplicates (e.g., 5 separate rules about "inform customer when escalating"). Deduplication would improve signal.

### 5. Multi-agent coverage
Currently only `cancellation_handler` is tested. The framework supports all 9 agents in the Casence profile. Each agent needs:
- Its own test scenarios in `context_test_scenarios.json`
- Or auto-generated scenarios via `--extract-contracts --agent <name>`

---

## Next Steps to Discuss

1. **Mock order IDs or real test orders?** — Biggest impact on test accuracy
2. **Tool-level interception** — Capture every tool call with args for Layer 2
3. **Scale to all 9 agents** — Run `--extract-contracts` for each agent
4. **CI/CD integration** — Add to GitHub Actions / deployment pipeline
5. **Contract deduplication** — Post-process extracted contracts to remove near-duplicates
6. **Multi-run averaging** — Run tests 3x to reduce LLM judge variance
7. **State seeding depth** — Ensure fixture state reaches skill nodes correctly

