-- Remediation for the conversation_analytics prompt placeholder RCA.
--
-- Onboarding generated a "BUSINESS POLICIES" section into the analytics prompt
-- referencing four client-policy variables that nothing ever supplied. Three
-- were written bare ({return_exchange_policy}, {delivery_policy},
-- {after_delivery_return_exchange}) and crashed str.format(), so two tenants
-- (Vahro, Clarks) silently fell back to the generic default prompt for every
-- conversation. The code fix supplies all three from client_configs.
--
-- The fourth, support_team_contact_details, was written ESCAPED as
-- {{support_team_contact_details}}. That is valid str.format() input, so it
-- never crashed — it rendered as the literal text "{support_team_contact_details}"
-- and was sent to the LLM verbatim. 17 tenants are affected, including every
-- healthy one. This script unescapes it so the code fills it with the client's
-- real support contact from client_configs.vendor_contact_details.
--
-- ⚠️ ORDERING — APPLY ONLY AFTER THE CODE FIX IS DEPLOYED.
--   The analyzer must already be running the _render_prompt() change
--   (conversation_analyzer.py). Under the OLD str.format() code an unescaped
--   {support_team_contact_details} raises KeyError, which would push all 17
--   tenants onto the default prompt — spreading the very bug this fixes.
--   Verify the deployed revision first, then run this.
--
-- Scope: agent_name = 'conversation_analytics' ONLY. Other agents' prompts also
-- contain escaped braces but are rendered by different code paths that still
-- use str.format(); unescaping those would break them.
--
-- Apply against project snowy-base-61170908 (ecomm-agents).
-- Rows are stamped created_by='rca_analytics_placeholders_20260815'.
-- Pre-change snapshot: agents_config_backup_rca_20260815.
--
-- Rollback:
--   UPDATE agents_config a SET agent_prompt = b.agent_prompt,
--                              created_by   = b.created_by,
--                              updated_at   = b.updated_at
--   FROM agents_config_backup_rca_20260815 b
--   WHERE b.client_id = a.client_id AND b.agent_name = a.agent_name
--     AND a.created_by = 'rca_analytics_placeholders_20260815';

BEGIN;

-- 0. Snapshot before touching anything.
CREATE TABLE IF NOT EXISTS agents_config_backup_rca_20260815 AS
SELECT * FROM agents_config;

-- 1. Unescape the support contact placeholder so the analyzer substitutes the
--    client's real contact details instead of shipping the literal token.
--
--    Expected: 17 rows. Some prompts contain the token twice (Acchao, Fitflop,
--    Mochi Shoes, Pothys, Prathaa) — replace() is global, so both are handled.
--
--    Every one of the 17 has a vendor_contact_details config, so none will
--    render the "(not configured for this client)" marker. Verified 2026-08-15.
--
--    Idempotent: re-running matches nothing once the escaped form is gone.
UPDATE agents_config
SET agent_prompt = replace(
      agent_prompt,
      '{{support_team_contact_details}}',
      '{support_team_contact_details}'),
    updated_at = now(),
    created_by = 'rca_analytics_placeholders_20260815'
WHERE agent_name = 'conversation_analytics'
  AND agent_prompt LIKE '%{{support_team_contact_details}}%';

COMMIT;

-- ---------------------------------------------------------------------------
-- Verification — run after COMMIT.
--
-- 1. No escaped form left, 17 rows carry the bare form and the stamp.
--   SELECT
--     COUNT(*) FILTER (WHERE agent_prompt LIKE '%{{support_team_contact_details}}%')            AS still_escaped,      -- expect 0
--     COUNT(*) FILTER (WHERE agent_prompt ~ '(?<!\{)\{support_team_contact_details\}(?!\})')    AS now_substitutable,  -- expect 17
--     COUNT(*) FILTER (WHERE created_by = 'rca_analytics_placeholders_20260815')                AS stamped            -- expect 17
--   FROM agents_config WHERE agent_name = 'conversation_analytics';
--
-- 2. Every replacement field in every analytics prompt is now supplied by the
--    analyzer — nothing will reach the LLM unfilled, and nothing will trip the
--    "references placeholder(s) ... not supplied" error log. Expect 0 rows.
--   WITH supplied(k) AS (
--     VALUES ('conversation_text'),('client_id'),('phone'),('message_count'),
--            ('duration_minutes'),('post_conversation_orders'),
--            ('return_exchange_policy'),('delivery_policy'),
--            ('after_delivery_return_exchange'),('support_team_contact_details')
--   )
--   SELECT a.client_id, t.tok
--   FROM agents_config a,
--        LATERAL (SELECT (regexp_matches(
--                  a.agent_prompt,
--                  '(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})', 'g'))[1]) AS t(tok)
--   WHERE a.agent_name = 'conversation_analytics'
--     AND t.tok NOT IN (SELECT k FROM supplied);
--
-- 3. Confirm no other agent was touched. Expect 0.
--   SELECT COUNT(*) FROM agents_config
--   WHERE created_by = 'rca_analytics_placeholders_20260815'
--     AND agent_name <> 'conversation_analytics';
--
-- ---------------------------------------------------------------------------
-- Cache: prompts live only in Redis under agents_config:{client_id} with a 600s
-- TTL and no in-memory tier, so the rewrite propagates within 10 minutes. To
-- force it sooner: DEL agents_config:<client_id> for each affected client.
--
-- The policy values themselves are read from client_configs through
-- aget_json_config, which has both a process-memory tier and Redis, so a policy
-- edit takes up to CONFIG_MEMORY_TTL to appear even after busting Redis.
--
-- ---------------------------------------------------------------------------
-- Affected clients (17), all verified to have vendor_contact_details:
--   47350f71 Acchao (x2)        8f03cfa7 Amydus            f9a0957c Clarks
--   7878c45d Do not delete      37c47134 Enamor            3337ddf9 Fitflop (x2)
--   4d732299 Forest Essentials  70710b11 Manyavar          1de622f5 Milton
--   4326786f Mochi Shoes (x2)   a69f65c2 Nalli             5b061c75 Posh Affair
--   7db9c4be Pothys (x2)        d5bf3362 Prathaa (x2)      827f34a8 Rare Rabbit
--   1d81110b Underneat          a78f071b Vahro
--
-- NOT covered by this script — tracked separately:
--   * Backfilling the 31 conversation_analytics rows for Vahro that were
--     analysed with the default prompt (identifiable by prompt_version='2.0').
--   * Onboarding-side validation to stop the generator inventing placeholders
--     no consumer supplies. This script fixes the data, not the source.
