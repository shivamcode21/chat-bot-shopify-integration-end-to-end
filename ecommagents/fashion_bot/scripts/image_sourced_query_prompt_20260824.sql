-- Image-sourced query guidance for the product-facing handlers.
--
-- Why: a customer sent a product photo on WhatsApp. The image analysis described
-- it correctly ("orange oversized t-shirt with dragonfly print"), search returned
-- five tees that were not it, and the agent replied "I couldn't find an orange
-- oversized t-shirt with a dragonfly print in our current collection" — asserting
-- absence it could not establish. In a second case it denied having the piece and
-- then listed what was very likely that exact piece among the alternatives.
--
-- The SEARCH RESULT RELEVANCE CHECK block was written for category-level misses
-- ("customer wants sneakers, we sell shirts"). The model applies it at item level.
-- This appends a rule that scopes the honest-absence wording so it never fires on
-- a machine-read photo.
--
-- The same text ships in fashion_bot/utils/context_helpers.py DEFAULT_PROMPTS, which
-- is only the fallback: generic_skill_node resolves "<agent>_handler" from
-- agents_config first, so clients with rows there need this migration to see it.
--
-- Idempotent: the guard skips prompts that already carry the block, so re-running
-- changes nothing. Takes effect after the prompt cache TTL (memory 600s, Redis 1h).

BEGIN;

-- Backup before touching anything. Restore with:
--   UPDATE agents_config a SET agent_prompt = b.agent_prompt
--   FROM agents_config_backup_image_query_20260824 b WHERE a.id = b.id;
CREATE TABLE IF NOT EXISTS agents_config_backup_image_query_20260824 AS
SELECT * FROM agents_config;

UPDATE agents_config
SET agent_prompt = agent_prompt || E'\n
## IMAGE-SOURCED QUERIES (CRITICAL)
A message beginning with `[Product from image]:` or `[Text from image]:` was produced by an
automated reading of a photo the customer sent — it is a machine\'s description, not the
customer\'s own words. It can be imprecise, and it never carries a product name or link, so
you CANNOT establish from it that a specific item is or is not in the catalog.
1. NEVER tell the customer that the specific item they photographed is unavailable. You do
   not know that. Say only what you do know: what the search returned.
2. Present the closest matches you found as possibilities, not as the item they sent.
3. Ask for the product name or link so you can confirm the exact piece.
4. The category-level rule above still applies: if the results are a different product type
   entirely, say that plainly.
Example: Customer sends a photo, search returns tees that do not obviously match
→ "I\'m not certain I\'ve found the exact piece you shared, but these are the closest matches
   in our collection. If you can share the product name or link, I\'ll confirm it for you."
'
WHERE agent_name IN ('product_details_handler', 'recommendations_handler')
  AND agent_prompt NOT LIKE '%IMAGE-SOURCED QUERIES%';

-- Expect one row per (client, handler) that did not already have it.
SELECT client_id, agent_name, length(agent_prompt) AS len
FROM agents_config
WHERE agent_name IN ('product_details_handler', 'recommendations_handler')
ORDER BY client_id, agent_name;

COMMIT;
