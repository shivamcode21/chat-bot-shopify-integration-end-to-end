-- Enamor: no Shopify order notes on escalation + "reach out to support" wording.
--
-- Client request (Enamor, client_id 37c47134-3f64-432b-915a-4816c298d80b):
--   1. Escalations must not write anything into the Shopify order notes.
--   2. Every escalation must tell the customer the issue HAS BEEN escalated and
--      ask them to contact the support team (phone + email) themselves. It must
--      never promise "someone will reach out to you soon".
--
-- Neither is fully achievable from the prompts alone, so this script is the DB
-- half of a two-part change:
--
--   * CODE (already merged): two per-client switches —
--       - escalation_policy.order_notes_enabled  gates the [Bloomerce] … /
--         [Bot - FAILED] … notes that orchestration code writes on the
--         escalation path (agent_config.aescalation_order_notes_enabled →
--         order_utils.aadd_escalation_order_note);
--       - escalation_messaging.customer_message  overrides the escalation
--         confirmation text, including the web-chat reply that
--         generic_skill_node force-sets over whatever the LLM wrote
--         (agent_config.aget_escalation_customer_message).
--     Both default to today's behaviour, so no other client is affected.
--
--   * THIS SCRIPT: sets those two config rows for Enamor and edits the prompt
--     lines that carry the old wording (or that instruct annotate_order on an
--     escalation).
--
-- ⚠️ ORDERING — APPLY ONLY AFTER THE CODE FIX IS DEPLOYED.
--   Before the code change the two config keys are simply unread: section 1 is
--   inert and section 2 alone would leave the web-chat escalation still saying
--   "someone from our support team will be in touch with you shortly".
--
-- ⚠️ CACHE: config and prompts are read through the three-tier cache
--   (memory → Redis → Postgres). Bust the Redis keys for this client after
--   applying (client_configs:<client_id>, agents_config:<client_id>) or wait
--   out the ~10 min local TTL.
--
-- Apply against project snowy-base-61170908 (ecomm-agents).
-- Pre-change prompt snapshot: agents_config_backup_enamor_esc_20260826.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. Backup the prompts this script edits
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents_config_backup_enamor_esc_20260826 AS
SELECT * FROM agents_config WHERE 1 = 0;

INSERT INTO agents_config_backup_enamor_esc_20260826
SELECT *
FROM agents_config
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name IN ('escalation_handler', 'cancellation_handler', 'discount_handler');

-- ---------------------------------------------------------------------------
-- 1. No Shopify order notes on the escalation path
-- ---------------------------------------------------------------------------
-- Enamor has no escalation_policy row today, so this INSERT creates it. The
-- ON CONFLICT branch merges the key in without disturbing gate_enabled /
-- resolution_first_tools if the row is added by something else first.
INSERT INTO client_configs (client_id, config_key, config_value)
VALUES (
    '37c47134-3f64-432b-915a-4816c298d80b',
    'escalation_policy',
    '{"order_notes_enabled": false}'::jsonb
)
ON CONFLICT (client_id, config_key)
DO UPDATE SET config_value =
    client_configs.config_value || '{"order_notes_enabled": false}'::jsonb;

-- ---------------------------------------------------------------------------
-- 2. Escalation confirmation wording
-- ---------------------------------------------------------------------------
-- Applies to EVERY escalation path in code, including the web-chat override
-- that discards the LLM's reply. The support phone/email are appended
-- separately by get_contact_details_message() from vendor_contact_details
-- (customercare@enamor.co / +917406844400) — do NOT repeat them here or the
-- customer sees them twice.
INSERT INTO client_configs (client_id, config_key, config_value)
VALUES (
    '37c47134-3f64-432b-915a-4816c298d80b',
    'escalation_messaging',
    jsonb_build_object(
        'customer_message',
        'I''ve escalated your issue to our support team. Please reach out to them directly so they can resolve it for you right away.'
    )
)
ON CONFLICT (client_id, config_key)
DO UPDATE SET config_value =
    client_configs.config_value || EXCLUDED.config_value;

-- ---------------------------------------------------------------------------
-- 3. escalation_handler prompt — customer-facing templates
-- ---------------------------------------------------------------------------
UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'Callback Confirmation:
"Perfect! We will call you at the scheduled time. Thank you for your time! 😊. You may also contact us directly at {{support_team_contact_details}}, Monday to Saturday, 11:30 AM to 6:30 PM."',
        'Callback Confirmation:
"Thank you! I''ve escalated your callback request to our support team. Please also reach out to them directly at {{support_team_contact_details}}, Monday to Saturday, 11:30 AM to 6:30 PM."'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'escalation_handler';

UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'Frustration Escalation:
"This seems important. I''ll transfer you to a team member who will reach out to you shortly. Thank you for your patience! 🙏. You may also contact us directly at {{support_team_contact_details}}, Monday to Saturday, 11:30 AM to 6:30 PM."',
        'Frustration Escalation:
"I''m really sorry about this — I''ve escalated it to our support team. Please reach out to them directly at {{support_team_contact_details}}, Monday to Saturday, 11:30 AM to 6:30 PM, and they''ll help you right away. 🙏"'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'escalation_handler';

UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'General Escalation:
"I''ve shared this with our team, and someone will contact you shortly to help further. You may also contact us directly at {{support_team_contact_details}}, Monday to Saturday, 11:30 AM to 6:30 PM."',
        'General Escalation:
"I''ve escalated your issue to our support team. Please reach out to them directly at {{support_team_contact_details}}, Monday to Saturday, 11:30 AM to 6:30 PM."'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'escalation_handler';

-- ---------------------------------------------------------------------------
-- 4. escalation_handler prompt — the flow instructions behind those templates
-- ---------------------------------------------------------------------------
UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'Respond to the customer with a friendly confirmation including the callback time and show support team contact details.',
        'Respond to the customer confirming the request HAS BEEN escalated, including the callback time, and ask them to reach out to the support team directly — show the support team contact details. Never promise that someone will contact or reach out to them.'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'escalation_handler';

UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'Respond with an empathetic message confirming transfer to a team member and show support team contact details.',
        'Respond with an empathetic message confirming the issue HAS BEEN escalated to the support team, and ask the customer to reach out to that team directly — show the support team contact details. Never promise that someone will contact or reach out to them.'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'escalation_handler';

UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'Confirm to the customer that an agent will reach out shortly and show support team contact details.',
        'Confirm to the customer that the issue HAS BEEN escalated to the support team and ask them to reach out directly — show the support team contact details. Never promise that someone will contact or reach out to them.'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'escalation_handler';

-- ---------------------------------------------------------------------------
-- 5. escalation_handler prompt — standing rule (backstop for any wording the
--    templates above don't cover)
-- ---------------------------------------------------------------------------
UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        '
Now handle the escalation using the tools available.',
        '
[ESCALATION REPLY RULE — ALWAYS]
Every escalation reply must (a) tell the customer their issue HAS BEEN escalated
to the support team, and (b) ask them to contact that team directly, showing
{{support_team_contact_details}}. NEVER promise a callback or that someone will
reach out / contact / get back to the customer — not in any wording, not for any
category, including callback requests. Never write anything into the order notes
as part of an escalation.

Now handle the escalation using the tools available.'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'escalation_handler';

-- ---------------------------------------------------------------------------
-- 6. cancellation_handler — drop the annotate_order step on the manual-handling
--    escalation, and fix its closing line
-- ---------------------------------------------------------------------------
-- This is the one prompt-driven Shopify order note that fires on an escalation.
-- annotate_order stays available to the agent for its legitimate use (recording
-- an alternate mobile number on the order) — only the escalation step is removed.
UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        '    You MUST perform these 2 actions before responding to the customer:
    1) Call annotate_order with the order_id and a note summarizing the request (e.g., "Customer requested size change from S to L. Price difference: ₹200. Requires manual handling." or "Customer requested product change to [new product name]. Price difference: ₹300. Requires manual handling.").
    2) Call escalate_to_agent',
        '    You MUST perform this action before responding to the customer:
    1) Call escalate_to_agent'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'cancellation_handler';

UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        '    After both tools are called, inform the customer that their request has been received and the support team will reach out to assist with the change.',
        '    After the tool is called, tell the customer their request HAS BEEN escalated to the support team and ask them to reach out to that team directly at {{support_team_contact_details}}. Never say the support team will reach out to them.'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'cancellation_handler';

-- ---------------------------------------------------------------------------
-- 7. discount_handler — same "team will reach out" promise on its hand-off
-- ---------------------------------------------------------------------------
UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'Confirm that the appropriate team will reach out and show support team contact details: {{support_team_contact_details}} (Available Mon - Sat, 9 AM - 7 PM IST).',
        'Confirm that the request HAS BEEN escalated to the appropriate team and ask the customer to reach out to them directly: {{support_team_contact_details}} (Available Mon - Sat, 9 AM - 7 PM IST). Never say the team will reach out to the customer.'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'discount_handler';

UPDATE agents_config
SET agent_prompt = replace(
        agent_prompt,
        'If get_contact_information returns no contact details, tell the customer our team will reach out and do NOT fabricate any email or phone number.',
        'If get_contact_information returns no contact details, tell the customer the request has been escalated to our team and do NOT fabricate any email or phone number.'
    ),
    updated_at = NOW()
WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
  AND agent_name = 'discount_handler';

COMMIT;

-- ---------------------------------------------------------------------------
-- VERIFY
-- ---------------------------------------------------------------------------
-- Both config rows present and shaped as expected:
--
--   SELECT config_key, config_value
--   FROM client_configs
--   WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
--     AND config_key IN ('escalation_policy', 'escalation_messaging');
--
-- No promise wording left in any Enamor prompt (expect 0 rows apart from the
-- RE-ESCALATION detection line in escalation_handler, which DESCRIBES a past
-- promise in the conversation history and must stay, and the order_status
-- "the delivery person will contact you soon" line, which is a delivery fact,
-- not an escalation):
--
--   SELECT agent_name, ln FROM (
--     SELECT agent_name, unnest(string_to_array(agent_prompt, E'\n')) AS ln
--     FROM agents_config WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
--   ) t
--   WHERE ln ILIKE '%will reach out to you%'
--      OR ln ILIKE '%someone will contact you%'
--      OR ln ILIKE '%will be in touch%'
--      OR ln ILIKE '%team will reach out%';
--
-- No escalation step still calls annotate_order:
--
--   SELECT agent_name FROM agents_config
--   WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
--     AND agent_prompt ILIKE '%Requires manual handling.")%';
--
-- ---------------------------------------------------------------------------
-- ROLLBACK
-- ---------------------------------------------------------------------------
--   UPDATE agents_config a
--   SET agent_prompt = b.agent_prompt, updated_at = NOW()
--   FROM agents_config_backup_enamor_esc_20260826 b
--   WHERE a.client_id = b.client_id AND a.agent_name = b.agent_name;
--
--   DELETE FROM client_configs
--   WHERE client_id = '37c47134-3f64-432b-915a-4816c298d80b'
--     AND config_key IN ('escalation_policy', 'escalation_messaging');
