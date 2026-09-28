# GOAL

CONVERT THE COMPLETE GRAPH IMPL INTO TRUE STREAMING.

## CURRENT_STATE

- We have a Lnaggraph implemented behind a stream_graph_response function from steraming service
- It provides streaming interface but internally the graph it uses dont provide streaming interface.
  
## PROCESS

- To achieve this first i want to cleanup generic skill node
- Then make the generic skill node true streaming
- Then the entire graph
- IN this every step should be commited so that we can see a diff.

## STEP 1 - cleanup generic skill node

- In this we have a primary function generic_skill_node
- In the cleanup first our goal is make that function small / neat and clean
- all internal utility code in that , convert them to a idempotent function and any state update or variable assignment to be done within this function in as minimal as possible.
   - Eg: we have a section in  CONDITIONAL PHONE EXTRACTION  , in that actually we are just fetching a phone basis a logic and update state phone number that entire code should actually be in a function and called from this function which return the phone number
   - EG: if a prompt is defined in the main function that hinders the readability , create it outside as a global variable
   - Make Sure no variable change in the functions we create thos should be pure idempotent function.
- To do this you first need to identify all such cases in the generic skill node , 
- Then launch a Principal Enginner which creates a change for all and then a adversial agent to review the changes so that no logic is broken and cleanup goal is actually achieved.
- Launch a workflow for the above and complete this step

AFter Step 1 is completed pause and wait for human input to privude Step 2 in this file

Step 1 is COMPLETE (commit 4d584b2 on branch streaming_generic).

---

# STEP 2 - make generic_skill_node true streaming

## 2.0 Definitions / current contract (the thing we are replacing)

`stream_graph_response()` in `core/streaming_service.py` is the public streaming
entrypoint. Today it is a FAKE stream:

1. `yield {"type":"start"}`
2. `result = await graph.ainvoke(state)`   <-- whole graph runs to completion, blocking
3. extract final text from `result["messages"][-1]` / `result["customer_message"]`
4. slice that finished string into 5-char chunks and `yield {"type":"token"}` per slice
5. `yield {"type":"end","full_response":...,"result":result,"langsmith_trace_id":...}`
6. on error: `yield {"type":"error","message":...}`

The WIRE CONTRACT the adapters already understand (websocket_chat.py,
conversation_runtime.run_turn_stream) is the set of event `type`s:
`start | token | tool_start | tool_end | end | error | queued` plus state-only
events (`state_snapshot`, `reply_text`). We KEEP this wire contract and make the
tokens real. Anything new is additive.

## 2.1 What generic_skill_node emits

The terminal LLM message (`_invoke_native_tool_loop`) is ONE string with three
concatenated regions IN THIS ORDER:

    <customer prose>                     -> stream to user
    ###SHOW_PRODUCTS:[...]###            -> machine block, buffer (do NOT stream)
    ```summary_update { ...json... }```  -> machine block, buffer (do NOT stream)

After the full string exists the node parses the two trailing blocks
(`_parse_summary_update_from_response`, `_parse_show_products_from_response`),
strips them from the prose, mutates `conversation_context` (topic summary/status/
awaiting, focal entity, entity refs), and returns the state delta
(`customer_message`, `conversation_context`, `messages`, `recent_products`,
`show_product_handles`, passthrough fields). That return delta is the real
LangGraph contract; the tokens today are cosmetic.

## 2.2 Native mechanism

Use LangGraph custom streaming:
- inside the node: `writer = get_stream_writer()` (langgraph.config) and push events.
- outside: `graph.astream(state, stream_mode=["custom","updates"])`.
The node STILL returns its normal state delta, so `graph.ainvoke` (WhatsApp /
gupshup, non-streaming) keeps working unchanged. Only the web/streaming caller
switches to `astream`. Gate on the existing `state["_streaming_enabled"]` flag so
we only pay the astream/token cost when someone is listening.

## 2.3 The three components to build

### (a) StreamGuard - delimiter boundary splitter  [pure, idempotent]
A small stateful-but-isolated helper (class or closure) that receives raw token
deltas and splits prose (emit) from machine blocks (buffer). Rules:
- scan the running buffer for the EARLIEST delimiter anchor:
  `###SHOW_PRODUCTS`, ```` ```summary_update ````, ```` ```context_update ````,
  ```` ```json_summary_update ````  (reuse the exact anchors the parsers accept).
- before a delimiter is seen: emit everything EXCEPT a hold-back tail of
  `H = len(longest_anchor)-1` chars, so an anchor split across two chunks is never
  leaked. Flush the held tail only once it is proven not to start an anchor.
- once any delimiter is seen: stop emitting, append everything (incl. the rest of
  the stream) to `machine_tail`. No further user tokens for this turn.
- expose `.flush()` (end of stream) and `.machine_tail` (the buffered blocks).
- emits NOTHING for the machine region -> the existing leak-guard regexes stay as
  defense-in-depth but should never fire on the streamed path.

### (b) stream EVERY turn; the node is a transparent emitter  [DECISION LOCKED]
DECISION: stream everything; the CALLER of the streaming service chooses what to
show the end user. This removes any "is this the terminal turn?" guessing from the
node. The node never suppresses; it only TAGS.

`_invoke_native_tool_loop` today calls `_ainvoke_llm` (ainvoke) every iteration.
Change: when `_streaming_enabled` and a `writer` exists, EVERY iteration is produced
with `llm.astream(messages)`. Each `AIMessageChunk` is fed to the StreamGuard so we
can tag its `region` (prose vs machine-block), and pushed via the writer WITH
metadata identifying its source:
  - `turn` (int, 1-based iteration index)
  - `region` ("prose" | "machine")
  - tool-call chunks surface as `tool_start`/`tool_end` events around tool exec.
At each iteration boundary emit `turn_end {turn, had_tool_calls}` so the caller
knows, after the fact, which turn was the answer (the one with had_tool_calls=false
and final=true). The caller can therefore choose any policy: forward only the final
prose turn, show intermediate "thinking", show tool activity, etc. The node ships
the full firehose; policy is downstream.

StreamGuard responsibility narrows accordingly: it CLASSIFIES (prose vs machine
boundary, with the split-delimiter hold-back) so each delta is correctly tagged. It
no longer DROPS anything — dropping is the caller's choice.

### (c) post-stream parse + structured emit  ("further streaming response")
After the stream completes the node has `guard.machine_tail`. Feed it through the
SAME two parsers (no behavior change), update `conversation_context` /
`show_product_handles` exactly as today, then emit structured custom events:
- `writer({"type":"products","handles":[...],"recent_products":[...]})`
- optionally `writer({"type":"state","conversation_context_delta":{...}})`
and finally RETURN the same state delta dict as today. Streamed prose == returned
`customer_message` (clean_response) so streaming and non-streaming channels stay
byte-consistent.

## 2.4 Final event contract (verbose, additive to the wire contract)

THREE LAYERS, each with a clear responsibility:

LAYER 1 - node/loop -> writer (RAW firehose, no policy, fully tagged):
- `{"type":"turn_start","turn":int}`
- `{"type":"token","content":str,"turn":int,"region":"prose"|"machine"}`
- `{"type":"tool_start","turn":int,"tool":str,"input":obj}`
- `{"type":"tool_end","turn":int,"tool":str,"output":str}`
- `{"type":"turn_end","turn":int,"had_tool_calls":bool,"final":bool}`
- `{"type":"products","handles":[str],"recent_products":[obj]}`  (post-parse)
- `{"type":"state","conversation_context_delta":obj}`            (optional)
- node `return` (state delta) -> arrives on `astream` `updates` channel.

LAYER 2 - stream_graph_response (DEFAULT POLICY mapper, caller-overridable):
Consumes the raw firehose and applies the default end-user policy, producing the
UNCHANGED wire contract so existing adapters keep working with zero change:
- forward `region=="prose"` deltas of the FINAL turn as `{"type":"token","content"}`
- DROP `region=="machine"` deltas (delimited blocks never shown)
- forward `tool_start`/`tool_end` as-is
- forward `products`/`state` as additive events (adapters ignore unknown types)
- final node value -> `{"type":"end","full_response":<final-turn prose>,
  "result":<state delta>,"langsmith_trace_id":...}`
- errors -> `{"type":"error","message":...}` (keep langsmith trace wrapper)
Expose a hook/param (e.g. `event_policy="final_prose"|"raw"|callable`) so a caller
can instead receive the raw firehose and pick its own policy (intermediate
thinking, tool activity, etc.). DEFAULT preserves today's UX exactly.

LAYER 3 - channel adapter (websocket_chat / gupshup): unchanged for the default
policy; may opt into `raw` to render richer UI later.

`full_response` at `end` MUST equal the concatenation of forwarded `token`s (the
final-turn clean prose) so runtime `reply_text` accounting and persistence stay
correct.

## 2.5 Decisions (LOCKED)

1. [LOCKED] Stream EVERY turn; node is a transparent tagged emitter; the streaming
   service / caller owns the show-to-user policy. Default policy = final-turn prose
   only (preserves today's UX). No terminal-turn guessing in the node.
2. [LOCKED] Carousel timing: emit `products` AFTER prose finishes (matches current
   ordering and the "collect delimited data then continue" requirement).
3. [LOCKED] Non-streaming channels (WhatsApp/gupshup) untouched: writer is a no-op
   under `ainvoke`; guard/astream engage only under `_streaming_enabled`.
4. [OPEN-impl] Provider differences: OpenAI vs Gemini chunk shapes
   (`tool_call_chunks`, list-content). Normalize via existing
   `_normalize_llm_content`; handle both in the loop.
5. [LOCKED] astream stream_mode must include `updates` (state delta for `end` +
   merge) and `custom` (the firehose).

## 2.6 Build plan (each step its own commit)

1. StreamGuard helper + unit tests (pure, no graph) .................. commit
2. `_invoke_native_tool_loop` streaming path behind `_streaming_enabled` . commit
3. node: writer wiring + post-parse structured emit (return unchanged) .. commit
4. `stream_graph_response`: switch to `graph.astream(stream_mode=...)`,
   map events, keep wire contract + langsmith wrapper .................. commit
5. adapter/runtime: accept additive `products`/`state` events ......... commit
6. verify: web (real tokens) + whatsapp (unchanged) parity ............ commit

Launch a Principal Engineer to implement step-by-step and an adversarial
reviewer per step (same pattern as Step 1). Do NOT start until 2.5 decisions are
locked by human.

---

# STEP 2 — FINAL DESIGN (post-migration to langchain 1.x `create_agent`)

Supersedes 2.2-2.6 above. The codebase now uses `langchain.agents.create_agent`
(`utils/agent_utils.py: run_agent_graph`). Two execution paths currently exist in
`generic_skill_node`:
  - `USE_NATIVE_TOOL_LOOP=True`  -> `_invoke_native_tool_loop` (HARDCODED custom loop)
  - else / fallback              -> `run_agent_graph` (native create_agent)

## 2.7 Decision: ONE native path

DELETE `_invoke_native_tool_loop` and its `USE_NATIVE_TOOL_LOOP` branch. The native
`create_agent` graph (`run_agent_graph`) becomes the SINGLE execution path, with a
streaming variant. Rationale: one provider-agnostic loop, native token streaming,
no bespoke dedup/forced-tool/format code to maintain in two places. The behaviors
the hardcoded loop had (forced-first-tool grounding, product-observation
formatting) move to create_agent equivalents (middleware / formatted-tool-return)
or are accepted as follow-ups — tracked explicitly, not silently dropped.

Helpers to remove with the loop: `_invoke_native_tool_loop`, `_astream_llm_turn`
(if added), and `_ainvoke_llm`/`_ainvoke_tool` IF unused elsewhere (grep first).

## 2.8 Two clearly separated stages (the core of this step)

STAGE A — NATIVE STREAM (transport only; no parsing, no state writes):
  `run_agent_graph(..., writer=writer)` streams the create_agent graph with
  `stream_mode=["messages","values"]`. A `StreamGuard` forwards PROSE tokens via
  `writer({"type":"token"})` and stops at the first delimiter anchor (the rest of
  the model output buffers into `guard.machine_tail`, unsent). When the inner
  astream exhausts, it returns the SAME shape as today:
  `{"output": <full final text incl. blocks>, "intermediate_steps": [...]}`.
  Note: `output` is the COMPLETE text (prose + delimited blocks). The guard only
  governed what STREAMED; it does not pre-strip the return value.

STAGE B — PARSE (pure, separate, runs AFTER the native call returns):
  A single pure function turns the full `output` into the customer-facing prose +
  the extracted machine artifacts. NO streaming, NO state mutation here.

      def parse_agent_output(response_content: str):
          # 1) ```summary_update {...}```  -> dict + text with block stripped
          summary_update, text = _parse_summary_update_from_response(response_content)
          # 2) defense-in-depth: strip any mislabelled internal fence
          text = _strip_internal_tracking_leak(text)
          # 3) ###SHOW_PRODUCTS:[...]###  -> handles + text with block stripped
          show_product_handles, text = _parse_show_products_from_response(text)
          return SimpleNamespace(
              clean_prose=text,
              summary_update=summary_update,
              show_product_handles=show_product_handles or [],
          )

  Invariant: `parse_agent_output(output).clean_prose` == the prose the StreamGuard
  streamed live (both are "everything before the first delimiter", post leak-guard).
  So the streamed text and the persisted `customer_message` are byte-consistent.
  parse_agent_output is the SINGLE SOURCE OF TRUTH for the persisted reply; the
  guard is a transport optimization, not a second parser.

STAGE C — UPDATE CONTEXT (unchanged logic, now fed by parsed artifacts):
  existing entity/topic/focal/return-dict code consumes `summary_update`,
  `show_product_handles`, `intermediate_steps`. Then one manual yield:
  `writer({"type":"products","handles":..., "recent_products":...})`.

## 2.9 Why parse AFTER the native call (not during the stream)

- The two parsers are regex/JSON over the COMPLETE block; they need the whole block,
  which only exists once the stream ends. Parsing mid-stream would race partial JSON.
- Keeping parse separate from transport means non-streaming channels (WhatsApp via
  `graph.ainvoke`, writer is None) run the IDENTICAL `parse_agent_output` on the same
  `output` -> zero behavioral divergence between streaming and non-streaming.
- The StreamGuard never has to be "correct" about JSON — it only needs to detect the
  first anchor to stop user-visible streaming. All correctness lives in STAGE B.

## 2.10 Node control flow (final)

    writer = get_stream_writer() if state["_streaming_enabled"] else None     # may be None
    agent_result = await run_agent_graph(..., writer=writer)                  # STAGE A
    response_content = _normalize_llm_content(agent_result["output"]).strip()
    # max-iter recovery (unchanged) ...
    parsed = parse_agent_output(response_content)                            # STAGE B
    response_content      = parsed.clean_prose
    summary_update        = parsed.summary_update
    show_product_handles  = parsed.show_product_handles
    intermediate_steps    = agent_result["intermediate_steps"]
    # ... STAGE C: existing context/topic/focal updates + return dict ...
    if writer:
        writer({"type":"products","handles":show_product_handles or [],
                "recent_products":recent_products})

## 2.11 Build plan (each its own commit)

1. `StreamGuard` + `parse_agent_output` (pure) + unit tests ............... commit
2. `run_agent_graph(writer=...)` streaming variant ....................... commit
3. node: delete `_invoke_native_tool_loop` + `USE_NATIVE_TOOL_LOOP`;
   wire single path + STAGE B/C + products yield ......................... commit
4. `stream_graph_response`: `ainvoke` -> `astream(["custom","updates"])` .. commit
5. verify: web real tokens + whatsapp parity (same parse_agent_output) ... commit

