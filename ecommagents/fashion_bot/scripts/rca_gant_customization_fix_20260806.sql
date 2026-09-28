-- Remediation for RCA_GANT_CUSTOMIZATION_FALSE_PROMISE.md
--
-- The GANT bot promised a prepaid trouser-shortening service that GANT does not
-- offer. Root cause: Concept Groove's alteration policy was hard-coded into the
-- cloned product_details_handler prompt, in TWO places, so the agent answered
-- from the prompt and never called get_customization_config.
--
-- Applied against project snowy-base-61170908 (ecomm-agents) on 2026-08-06.
-- Rows are stamped created_by='rca_gant_customization_20260806'.
-- Pre-change snapshot: agents_config_backup_rca_20260806 (602 rows).
--
-- Rollback:
--   UPDATE agents_config a SET agent_prompt = b.agent_prompt
--   FROM agents_config_backup_rca_20260806 b
--   WHERE b.client_id = a.client_id AND b.agent_name = a.agent_name
--     AND a.created_by = 'rca_gant_customization_20260806';

BEGIN;

-- 0. Snapshot before touching anything.
CREATE TABLE IF NOT EXISTS agents_config_backup_rca_20260806 AS
SELECT * FROM agents_config;

-- 1 + 4. [WORKFLOW] block: replace the hard-coded verdicts with a mandatory,
-- config-driven instruction. Matched 20 rows (1 occurrence each).
UPDATE agents_config a
SET agent_prompt = regexp_replace(
      a.agent_prompt,
      'Fetch customization/alteration policy\..*?If unclear, ask for desired final length\.',
      E'🔴 MANDATORY TOOL FETCH: You MUST call `get_customization_config` before answering ANY customization, alteration, shortening, lengthening, tailoring, hemming, or "can the length/fit be adjusted" question. NEVER answer from memory, from earlier turns in this conversation, or from what clothing brands generally do.\n'
      'The policy returned by that tool is the ONLY source of truth. Answer strictly from it:\n'
      '- If it says customization or size alteration is NOT supported, say so plainly. Do NOT offer, hint at, or promise any alteration, shortening, hemming or tailoring service — not as a possibility, not as a "usually", not as a maybe.\n'
      '- If it describes a supported service, state only what the policy actually says, including every condition it lists (for example a prepaid requirement or contact details). Do NOT add conditions the policy does not mention.\n'
      '- If the policy is empty, missing, or does not cover what was asked, do NOT guess and do NOT promise anything. Tell the customer you will confirm this with the team and call `escalate_to_agent`.\n'
      '- NEVER state a customization or alteration policy that is not present in the tool output.\n'
      'Only if the policy supports length alteration and the desired final length is unclear, ask the customer for their desired final length.',
      'g'),
    updated_at = now(),
    created_by = 'rca_gant_customization_20260806'
WHERE a.agent_name = 'product_details_handler'
  AND a.agent_prompt LIKE '%SHORTENING (final length smaller than current)%';

-- 1b. [EXAMPLES] block: a few-shot demonstrating the exact false promise
-- ("Yes, we can shorten the length by 3 inches! This would require a prepaid
-- order."). Present in 19 rows and more dangerous than the workflow rule,
-- because it shows the model the wrong answer rather than describing it.
UPDATE agents_config
SET agent_prompt = regexp_replace(
      agent_prompt,
      'Customization \(shortening\):.*?What would you like to adjust\? 😊"',
      E'Customization/alteration (ALWAYS call `get_customization_config` first — never answer from this example):\n'
      'Customer: "Can I shorten this from 42 inches to 39 inches?"\n'
      'Action: get_customization_config()\n'
      'Response when the returned policy says alteration/customization is NOT supported: "I am sorry — we do not offer alteration or customization on our products, so we are not able to change the length for you. Can I help you find the closest size instead? 😊"\n'
      'Response when the returned policy DOES support it: state only what that policy allows, in its own terms, including every condition it lists. Do not import any allowance, price, turnaround or payment condition from this example.\n'
      'Response when the policy is empty or does not cover the question: "Let me confirm this with our team and get back to you." then call `escalate_to_agent`.',
      'g'),
    updated_at = now(),
    created_by = 'rca_gant_customization_20260806'
WHERE agent_prompt LIKE '%Customization (shortening)%';

-- 2a. Cross-tenant domain leak: groovee.in in cloned prompts, remapped to each
-- client's own domain from clients.domain.
UPDATE agents_config a
SET agent_prompt = replace(a.agent_prompt, 'groovee.in', h.host),
    updated_at = now(),
    created_by = 'rca_gant_customization_20260806'
FROM (SELECT id, rtrim(regexp_replace(domain, '^https?://', ''), '/') AS host FROM clients) h
WHERE h.id = a.client_id
  AND a.client_id <> 'c3ffcb1b-afb9-4ca4-8746-a06698bec870'
  AND a.agent_prompt LIKE '%groovee.in%'
  AND h.host IS NOT NULL AND h.host <> '';

-- 2b. GANT: point social proof at GANT's own Instagram (the handle GANT's
-- product_details_handler already uses) and neutralise Groovee product names.
UPDATE agents_config
SET agent_prompt = regexp_replace(
      replace(replace(agent_prompt, 'Groovee', 'Classic'), 'groovee', 'classic'),
      'https://www\.instagram\.com/cncptgroove(\?igsh=[A-Za-z0-9]+)?',
      'https://www.instagram.com/gant/', 'g'),
    updated_at = now(),
    created_by = 'rca_gant_customization_20260806'
WHERE client_id = 'f5a737a7-c274-48d3-a6d2-d067f14b755b'
  AND agent_name = 'recommendations_handler';

-- 2c. Remaining clients: no per-client Instagram handle exists in any config
-- table, so the wrong handle is removed rather than replaced with a guess.
UPDATE agents_config
SET agent_prompt = regexp_replace(
      regexp_replace(agent_prompt,
        'Suggest Instagram: https://www\.instagram\.com/cncptgroove\?igsh=[A-Za-z0-9]+\s*for customer reviews and styling inspiration\.',
        'Do NOT share any social media handle or link unless it appears in tool output or client configuration — never state one from memory.', 'g'),
      '\s*Also, feel free to check out customer reviews and styling inspiration on our Instagram: https://www\.instagram\.com/cncptgroove\?igsh=[A-Za-z0-9]+\s*📸✨',
      '', 'g'),
    updated_at = now(),
    created_by = 'rca_gant_customization_20260806'
WHERE client_id <> 'c3ffcb1b-afb9-4ca4-8746-a06698bec870'
  AND agent_prompt LIKE '%cncptgroove%';

-- 2d. Dummy/test client "Do not delete": brand word left in example prose.
UPDATE agents_config
SET agent_prompt = replace(replace(agent_prompt, 'Groovee', 'Classic'), 'groovee', 'classic'),
    updated_at = now(),
    created_by = 'rca_gant_customization_20260806'
WHERE client_id = '7878c45d-a39d-488d-b37e-7fa4fc04ed7a'
  AND agent_prompt ~ '[Gg]roovee';

-- 3. Blank customization_policy rows: a row of empty strings is a truthy dict
-- that says nothing. After the code change these already fail safe (the tool
-- reports policy_found=false and the agent escalates), but escalating a routine
-- question is not the desired end state.
--
-- Set to a conservative "No" for the 15 clients whose product_details_handler
-- actually handles customization. Explicitly approved as a business decision —
-- the real per-brand policy was not available, and the accepted trade-off is
-- that a brand which DOES offer alterations will wrongly deny them until its
-- real value is recorded. Revisit with client success.
--
-- The other 8 blank clients (Fitflop, Forest Essentials, Kerala Ayurvedic,
-- Love Beauty Planet, Manyavar, Mochi Shoes, Tattvalogy, Underneat) are left
-- blank on purpose: their prompts do not handle customization at all.
--
-- Snapshot: client_configs_backup_rca_20260806 (1244 rows).
-- Rollback:
--   UPDATE client_configs cc SET config_value = b.config_value
--   FROM client_configs_backup_rca_20260806 b WHERE b.id = cc.id;
UPDATE client_configs cc
SET config_value = jsonb_build_object(
      'Do you support customization?', 'No',
      'Do you support size alteration?', 'No')
WHERE cc.config_key = 'customization_policy'
  AND NOT EXISTS (SELECT 1 FROM jsonb_each_text(cc.config_value) e WHERE btrim(e.value) <> '')
  AND EXISTS (SELECT 1 FROM agents_config a
              WHERE a.client_id = cc.client_id
                AND a.agent_name = 'product_details_handler'
                AND a.agent_prompt LIKE '%get_customization_config%');

COMMIT;

-- Verification (all counts must be 0 except the two "new" columns).
--   SELECT
--     COUNT(*) FILTER (WHERE agent_prompt LIKE '%SHORTENING (final length smaller%')            AS old_workflow_rule,
--     COUNT(*) FILTER (WHERE agent_prompt LIKE '%Yes, we can shorten the length by 3 inches%')  AS old_false_example,
--     COUNT(*) FILTER (WHERE agent_prompt LIKE '%We can only shorten products, not lengthen%')  AS old_lengthen_example,
--     COUNT(*) FILTER (WHERE client_id <> 'c3ffcb1b-afb9-4ca4-8746-a06698bec870'
--                        AND agent_prompt ~ 'cncptgroove|[Gg]roovee|8607845846|admin@groovee')  AS cross_tenant_leaks,
--     COUNT(*) FILTER (WHERE agent_prompt LIKE '%MANDATORY TOOL FETCH: You MUST call `get_customization_config`%') AS new_workflow_rule,
--     COUNT(*) FILTER (WHERE agent_prompt LIKE '%never answer from this example%')              AS new_example
--   FROM agents_config;
--
-- Cache: prompts live only in Redis under agents_config:{client_id} with a 600s
-- TTL and no in-memory tier, so the rewrite propagates within 10 minutes. To
-- force it sooner: DEL agents_config:<client_id> for each affected client.
