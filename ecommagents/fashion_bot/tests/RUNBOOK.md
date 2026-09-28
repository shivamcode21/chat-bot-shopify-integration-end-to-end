# Agent Testing Runbook

> Quick-reference guide for running, debugging, and maintaining the agent test suite.
> All commands assume you are in the project root: `cd /Users/shivammehrotra/git-bot/ecommagents/fashion_bot`

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [Discovery Commands](#2-discovery-commands)
3. [Running Tests](#3-running-tests)
4. [Real Chat Replay](#4-real-chat-replay)
5. [Prompt Change Workflow](#5-prompt-change-workflow)
6. [Regression & Pre-Deploy Gate](#6-regression--pre-deploy-gate)
7. [Adding a New Test Scenario](#7-adding-a-new-test-scenario)
8. [Adding a New Tool Mock](#8-adding-a-new-tool-mock)
9. [Adding a New Client](#9-adding-a-new-client)
10. [Adding a New State Fixture](#10-adding-a-new-state-fixture)
11. [File Reference](#11-file-reference)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Quick Start

```bash
# Run all tests for Casence (mocked tools — no real API calls)
python3 -m tests.run_agent_tests --client casence
```

That's it. Results print to console + saved to `tests/agent_test_reports/`.

---

## 2. Discovery Commands

### List all agents in the tool registry

```bash
python3 -m tests.run_agent_tests --list-agents
```

Shows every agent name, its topic, prompt name, and tool factory.

### List all clients, fixtures, and test scenarios

```bash
python3 -m tests.run_agent_tests --list
```

### List recent real conversations (for replay)

```bash
python3 -m tests.run_agent_tests --replay-list --client casence

# Filter by phone number
python3 -m tests.run_agent_tests --replay-list --client casence --phone 9876543210
```

---

## 3. Running Tests

### Run all scenarios for a client

```bash
# Sequential (default)
python3 -m tests.run_agent_tests --client casence

# Parallel execution (faster for many tests)
python3 -m tests.run_agent_tests --client casence --parallel 4
```

- Tools are **mocked** by default (no Shopify/Shiprocket/Gupshup calls)
- Every tool call is **logged** with name, args, result, timing
- **Tool expectations** are verified (expected tools called, forbidden tools blocked)
- LLM-as-Judge scores each response (base + context criteria)
- **Parallel mode**: Use `--parallel N` to run N tests concurrently (recommended: 4-8 workers)

### Run a single scenario

```bash
python3 -m tests.run_agent_tests --test-id ctx-1-cancellation-cold-start --client casence
```

Available test IDs (run `--list` to see all):

| ID | Tests |
|----|-------|
| `ctx-1-cancellation-cold-start` | Full cancel flow: phone → orders → reason |
| `ctx-2-cancellation-mid-flow-reason` | Reason given mid-flow → escalate, don't cancel |
| `ctx-3-product-page-context-inquiry` | Product question with page_context pre-set |
| `ctx-4-identified-user-no-repeat-phone` | User known → don't re-ask phone |
| `ctx-5-cross-topic-product-to-cancel` | Topic switch from product → cancellation |
| `ctx-6-cancellation-size-issue-should-offer-fix` | Size issue → offer fix FIRST, not cancel |
| `ctx-7-address-update-no-ask-name` | Address update → auto-fill name/phone |
| `ctx-8-multi-order-selection` | Multiple orders → ask which one |

### Run with real APIs (no mocking)

```bash
python3 -m tests.run_agent_tests --client casence --use-real-apis
```

Tools still get **intercepted and logged**, but they hit real APIs. Use this for end-to-end integration testing.

### Run auto-generated scenarios (from contract extraction)

```bash
# Run scenarios generated from a single agent's prompt
python3 -m tests.run_agent_tests --scenarios-file tests/agent_test_reports/cancellation_handler_generated_scenarios.json --client casence

# Run ALL generated scenarios (from --agent all extraction)
python3 -m tests.run_agent_tests --scenarios-file tests/agent_test_reports/all_agents_generated_scenarios.json --client casence
```

### Custom scenarios file

```bash
python3 -m tests.run_agent_tests --scenarios-file path/to/your_scenarios.json --client casence
```

### Save report to a custom directory

```bash
python3 -m tests.run_agent_tests --client casence --output-dir tests/my_reports
```

---

## 4. Real Chat Replay

Replay actual customer conversations from the Postgres database to verify tool calls.

### List recent conversations

```bash
python3 -m tests.run_agent_tests --replay-list --client casence
```

### Replay a conversation (with mocked tools)

```bash
python3 -m tests.run_agent_tests --replay <conversation_id_uuid> --client casence
```

Output shows for each turn:
- Customer message
- Actual production bot response (from DB)
- Replay bot response (from current code)
- Tools called during replay

### Replay with real APIs

```bash
python3 -m tests.run_agent_tests --replay <conversation_id_uuid> --client casence --use-real-apis
```

### Export a conversation as a reusable test fixture

```bash
python3 -m tests.run_agent_tests --replay-export <conversation_id_uuid> --client casence
```

Creates two files:
- `tests/fixtures/states/replay_<short_id>.json` — initial state fixture
- `tests/fixtures/replays/replay_<short_id>.json` — scenario with all turns

### Direct CLI (alternative)

```bash
# List conversations
python3 -m tests.chat_replay --list --client-id 81e80e20-fe91-470a-ab3d-e9dfc2eebf4a

# View a conversation
python3 -m tests.chat_replay --conversation-id <uuid>

# Replay with tool verification
python3 -m tests.chat_replay --conversation-id <uuid> --verify-tools

# Export as fixture
python3 -m tests.chat_replay --conversation-id <uuid> --export-fixture
```

---

## 5. Prompt Change Workflow

When an agent prompt changes, follow this sequence:

### Step 1 — Extract contracts from the new prompt

```bash
# Single agent — from the agents_config DB table
python3 -m tests.run_agent_tests --extract-contracts --agent cancellation_handler --client casence

# Single agent — from a local text file
python3 -m tests.run_agent_tests --extract-contracts --agent cancellation_handler --client casence --prompt-file /path/to/prompt.txt

# ALL agents at once — pulls every prompt from agents_config table for this client
python3 -m tests.run_agent_tests --extract-contracts --agent all --client casence
```

> **Note:** If `DATABASE_URL` is not in your `.env`, pass it inline:
> ```bash
> DATABASE_URL="postgresql://user:pass@host/db?sslmode=require" python3 -m tests.run_agent_tests --extract-contracts --agent all --client casence
> ```

Outputs (per agent):
- `tests/agent_test_reports/<agent>_contracts.json` — extracted rules
- `tests/agent_test_reports/<agent>_generated_scenarios.json` — auto-generated test scenarios

When `--agent all` is used, an additional combined file is created:
- `tests/agent_test_reports/all_agents_generated_scenarios.json` — all scenarios from every agent in one file

### Step 2 — Review generated scenarios

Open the generated scenarios file. Check:
- Are `tool_expectations` correct?
- Are `expected_keywords` and `expected_behavior` sensible?
- Does the `initial_state_fixture` match the test intent?

### Step 3 — Merge into main scenarios file

Copy the good scenarios into `tests/context_test_scenarios.json`.
Also update any **existing scenarios** affected by the prompt change.

### Step 4 — Run the full suite

```bash
python3 -m tests.run_agent_tests --client casence
```

### Step 5 — Save new regression baseline

```bash
python3 -m tests.run_agent_tests --client casence --save-baseline
```

### Prompt → Agent name mapping

| Prompt Name (in agents_config DB) | Agent Name (in tool registry) |
|---|---|
| `cancellation_handler` | `cancel_or_update_order` |
| `product_details_handler` | `product_details` |
| `size_inquiry_handler` | `size_inquiry` |
| `order_status_handler` | `order_status` |
| `return_exchange_handler` | `return_exchange` |
| `delivery_timeline_handler` | `delivery_timeline` |
| `place_order_handler` | `place_order` |
| `discount_handler` | `discount` |
| `category_handler` | `category_details` |
| `recommendations_handler` | `recommendations` |
| `escalation_handler` | `escalation` |
| `feedback_handler` | `feedback` |
| `policy_handler` | `delivery_policy` / `payment_policy` / `return_exchange_policy` / `vendor_inquiry` |
| `unknown_handler` | `unknown_handler` |
| `continuity_check_agent` | *(meta-node — no tool registry entry)* |
| `intent_detection_handler` | *(meta-node — no tool registry entry)* |
| `final_answer_handler` | *(meta-node — no tool registry entry)* |

> **Tip:** You can pass either name to `--agent`. The CLI resolves it automatically via the tool registry.

---

## 6. Regression & Pre-Deploy Gate

### Save a baseline (do this after a known-good run)

```bash
python3 -m tests.run_agent_tests --client casence --save-baseline
```

Baselines saved to `tests/regression_snapshots/<scenario_id>.json`.

### Compare against baseline

```bash
python3 -m tests.run_agent_tests --client casence --regression
```

Detects:
- Score drops > 0.5 points
- Pass → Fail flips
- Response content changes

### Pre-deployment gate (CI/CD friendly)

```bash
python3 -m tests.run_agent_tests --client casence --regression --gate
```

- Exit code `0` = safe to deploy
- Exit code `1` = DO NOT deploy (tests failed or regressions detected)

---

## 7. Adding a New Test Scenario

Edit `tests/context_test_scenarios.json` and add to the `test_scenarios` array:

```json
{
    "id": "ctx-9-your-new-test",
    "description": "Describe what this tests",
    "client_name": "casence",
    "initial_state_fixture": "cold_start",
    "target_agent": "cancel_or_update_order",
    "initial_state_overrides": {
        "phone_number": "9876543210",
        "known_orders": [{"order_id": "GV12345", "status": "New", "product": "Sunfire Denim"}],
        "selected_order_id": "GV12345"
    },
    "context_assertions": {
        "expected_behaviors": ["Should do X", "Should NOT do Y"],
        "should_not_ask_for": ["phone_number"],
        "should_ask_for": ["cancellation reason"]
    },
    "tool_expectations": {
        "expected_tools": ["trigger_agent_escalation"],
        "forbidden_tools": ["cancel_order_in_shopify_tool", "cancel_order_in_shiprocket_tool"],
        "expected_tool_sequence": ["add_order_note_shopify_tool", "trigger_agent_escalation"],
        "min_tool_calls": 1,
        "max_tool_calls": 10
    },
    "mock_overrides": {
        "get_order_status_details": "access_denied"
    },
    "conversation": [
        {"role": "customer", "message": "Customer says this"},
        {
            "role": "bot",
            "expected_behavior": "Bot should respond by doing this",
            "expected_keywords": ["keyword1", "keyword2"],
            "context_check": "Must NOT ask for X since it is already in state"
        },
        {"role": "customer", "message": "Customer follow-up"},
        {
            "role": "bot",
            "expected_behavior": "Bot should do Y next",
            "expected_keywords": ["support", "contact"]
        }
    ]
}
```

### Field reference

| Field | Required | Purpose |
|---|---|---|
| `id` | Yes | Unique test identifier |
| `description` | Yes | What this test verifies |
| `client_name` | Yes | Client profile to use (`casence`) |
| `initial_state_fixture` | No | State fixture (`cold_start`, `mid_cancellation_flow`, etc.) |
| `target_agent` | No | Expected agent to handle this |
| `initial_state_overrides` | No | Override specific state fields inline |
| `context_assertions` | No | What the LLM judge checks for context usage |
| `tool_expectations` | No | Which tools should/shouldn't be called |
| `mock_overrides` | No | Use specific mock variants (e.g., `"access_denied"`) |
| `conversation` | Yes | The test turns (customer messages + expected bot behavior) |

### Available state fixtures

| Fixture | Pre-seeded state |
|---|---|
| `cold_start` | Empty state, nothing known |
| `mid_cancellation_flow` | Phone known, order GV12345 confirmed, waiting for cancellation reason |
| `identified_user_with_orders` | Phone 9876543210 known, 2 orders fetched (GV12345 + GV12346) |
| `product_page_context` | On Sunfire Denim product page, page_context set |
| `cross_topic_switch` | Was asking about a product, now switching topics |

---

## 8. Adding a New Tool Mock

Edit `tests/tool_mocker.py`, add to `MockToolDataProvider.MOCK_RESPONSES`:

```python
"your_new_tool_name": {
    "default": {
        "success": True,
        "message": "Tool executed successfully.",
        "data": {"key": "value"},
    },
    "error_variant": {
        "success": False,
        "error": "Something went wrong",
    },
},
```

### Rules

- The `"default"` variant is returned unless a scenario specifies `mock_overrides`
- Response format should match the real tool's return format
- Use `{arg_name}` placeholders — they get replaced with actual args
- List available mocks: `python3 -c "from tests.tool_mocker import list_available_mocks; print(list_available_mocks())"`

### Currently mocked tools (27)

`extract_phone_from_message`, `get_recent_orders_tool`, `get_order_status_details`,
`get_order_details_from_shopify_tool`, `get_order_details_from_shiprocket_tool`,
`store_cancellation_reason`, `increment_reason_attempts`,
`update_order_address`, `update_order_size_tool`, `update_order_phone_number_tool`,
`update_order_email_tool`, `add_order_note_shopify_tool`, `add_order_tags_shopify_tool`,
`cancel_order_in_shopify_tool`, `cancel_order_in_shiprocket_tool`,
`send_agent_message_tool`, `get_agent_phone_tool`, `trigger_agent_escalation`,
`change_order_product_tool`, `search_products_by_name`,
`check_grace_period_eligibility`, `get_return_reason`,
`get_final_return_exchange_message`, `suggest_exchange_instead_of_return`,
`search_product_by_name`, `get_product_details`, `update_shipping_order`

---

## 9. Adding a New Client

Create `tests/fixtures/clients/<client_name>.json`:

```json
{
    "client_name": "newclient",
    "client_id": "<uuid-from-database>",
    "gupshup_source_number": "15551234567",
    "agents": [
        "cancel_or_update_order",
        "order_status",
        "product_details"
    ]
}
```

Then update `context_test_scenarios.json` — either add new scenarios with `"client_name": "newclient"` or duplicate existing ones.

---

## 10. Adding a New State Fixture

Create `tests/fixtures/states/<fixture_name>.json`:

```json
{
    "fixture_id": "your_fixture_name",
    "description": "Describe the pre-seeded state",
    "state": {
        "messages": [
            {"role": "customer", "content": "I want to cancel my order"},
            {"role": "bot", "content": "Sure, which order?"}
        ],
        "phone_number": "9876543210",
        "client_id": "{{client_id}}",
        "selected_order_id": null,
        "known_orders": null,
        "page_context": null,
        "conversation_context": null,
        "is_frustrated": false,
        "needs_escalation": false,
        "waiting_for_cancellation_reason": false,
        "waiting_for_order_confirmation": false
    }
}
```

> `{{client_id}}` is automatically replaced at load time with the client's real UUID.

---

## 11. File Reference

```
tests/
├── run_agent_tests.py              # CLI entry point — run this
├── agent_test_runner.py            # Core test engine (graph invocation, LLM judge, tool patching)
├── tool_mocker.py                  # Mock data + tool interceptor + tool call verifier
├── chat_replay.py                  # Real conversation replay from Postgres
├── fixture_loader.py               # Loads client profiles + state fixtures
├── context_test_scenarios.json     # ★ YOUR TEST SCENARIOS (edit this)
├── context_test_config.py          # Scoring thresholds and weights
├── test_config.py                  # Base evaluation criteria
├── env_utils.py                    # Environment setup (.env loading)
├── fixtures/
│   ├── clients/
│   │   ├── casence.json            # Casence client profile
│   │   └── groovee.json            # Groovee client profile
│   └── states/
│       ├── cold_start.json
│       ├── mid_cancellation_flow.json
│       ├── identified_user_with_orders.json
│       ├── product_page_context.json
│       └── cross_topic_switch.json
├── regression_snapshots/           # Saved baselines for regression detection
├── agent_test_reports/             # Generated reports (txt, json, summary)
└── RUNBOOK.md                      # ★ THIS FILE
```

---

## 12. Troubleshooting

### "OPENAI_API_KEY not found"
```bash
# Make sure your .env file exists in fashion_bot/ and contains:
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...   # Used by LLM judge (Gemini)
```

### "Agent 'X' not found in tool registry"
The `target_agent` in your scenario doesn't match any key in `TOOL_REGISTRY`. Run `--list-agents` to see valid names.

### "State fixture not found"
The `initial_state_fixture` in your scenario doesn't match any file in `tests/fixtures/states/`. Run `--list` to see available fixtures.

### "Client profile not found"
The `client_name` in your scenario doesn't match any file in `tests/fixtures/clients/`. Run `--list` to see available clients.

### Tests pass with mocks but fail with `--use-real-apis`
This means the tools are being called correctly (good!) but the real API returned something unexpected. Check the tool call log in `agent_test_reports/agent_test_results.json` for the actual API response.

### "DATABASE_URL not set" (for replay features)
Replay features need Postgres access. Set `DATABASE_URL` in your environment or `.env` file.

### Tool not being intercepted
If a tool doesn't appear in the tool call log, it might not be registered in `tool_mocker.py`. The interceptor still wraps it but returns a generic `{"success": true}` mock. Add proper mock data for accurate testing.

---

## Quick Command Cheat Sheet

```bash
# === DISCOVERY ===
python3 -m tests.run_agent_tests --list-agents          # Show all agents
python3 -m tests.run_agent_tests --list                  # Show scenarios/fixtures/clients

# === RUN TESTS ===
python3 -m tests.run_agent_tests --client casence                              # All tests, mocked, sequential
python3 -m tests.run_agent_tests --client casence --parallel 4                 # All tests, parallel (4 workers)
python3 -m tests.run_agent_tests --client casence --use-real-apis              # All tests, real APIs
python3 -m tests.run_agent_tests --test-id ctx-1-cancellation-cold-start       # Single test

# === RUN GENERATED SCENARIOS ===
python3 -m tests.run_agent_tests --scenarios-file tests/agent_test_reports/cancellation_handler_generated_scenarios.json --client casence
python3 -m tests.run_agent_tests --scenarios-file tests/agent_test_reports/all_agents_generated_scenarios.json --client casence

# === REPLAY REAL CHATS ===
python3 -m tests.run_agent_tests --replay-list --client casence                # List conversations
python3 -m tests.run_agent_tests --replay <uuid> --client casence              # Replay conversation
python3 -m tests.run_agent_tests --replay-export <uuid> --client casence       # Export as fixture

# === PROMPT CHANGES ===
python3 -m tests.run_agent_tests --extract-contracts --agent cancellation_handler --client casence   # Single agent
python3 -m tests.run_agent_tests --extract-contracts --agent all --client casence                    # ALL agents

# With inline DATABASE_URL (if not in .env):
DATABASE_URL="postgresql://user:pass@host/db?sslmode=require" python3 -m tests.run_agent_tests --extract-contracts --agent all --client casence

# === REGRESSION / DEPLOY ===
python3 -m tests.run_agent_tests --client casence --save-baseline              # Save baseline
python3 -m tests.run_agent_tests --client casence --regression                 # Compare vs baseline
python3 -m tests.run_agent_tests --client casence --regression --gate          # CI gate (exit 0/1)
```

