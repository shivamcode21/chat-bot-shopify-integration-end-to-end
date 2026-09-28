# Lead Generation SQL

## Migration / Update Query

Run this once before reading lead fields from `conversation_analytics`.

```sql
CREATE TABLE IF NOT EXISTS global_configs (
    config_key TEXT PRIMARY KEY,
    config_value JSONB NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE conversation_analytics
    ADD COLUMN IF NOT EXISTS is_lead BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS lead_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS lead_status VARCHAR(30),
    ADD COLUMN IF NOT EXISTS lead_source_tags TEXT[] DEFAULT ARRAY[]::TEXT[],
    ADD COLUMN IF NOT EXISTS lead_customer_message_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS lead_generated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS lead_details JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_ca_leads
    ON conversation_analytics (client_id, lead_generated_at DESC, conversation_id)
    WHERE is_lead = TRUE;

CREATE INDEX IF NOT EXISTS idx_ca_lead_status
    ON conversation_analytics (client_id, lead_status, lead_generated_at DESC, conversation_id)
    WHERE is_lead = TRUE;

CREATE INDEX IF NOT EXISTS idx_ca_lead_type
    ON conversation_analytics (client_id, lead_type, lead_generated_at DESC, conversation_id)
    WHERE is_lead = TRUE;
```

## Global Lead Tag Config

Lead tags are global, not client-specific.

Store them in `global_configs`.

```sql
INSERT INTO global_configs (config_key, config_value, description, updated_at)
VALUES (
    'lead_generation_tags',
    '{
      "lead_generation_tags": [
        "Product Query",
        "Recommendation",
        "Size Inquiry",
        "Pricing Query",
        "Discount Query",
        "Delivery Query",
        "Payment Options Query",
        "Wholesale Inquiry"
      ],
      "preorder_lead_tags": [
        "Preorder Inquiry",
        "Back in Stock Inquiry",
        "Availability Inquiry"
      ]
    }'::jsonb,
    'Global tags used by conversation analytics to classify product/preorder leads.',
    NOW()
)
ON CONFLICT (config_key) DO UPDATE
SET config_value = EXCLUDED.config_value,
    description = EXCLUDED.description,
    updated_at = NOW();
```

## Optional Backfill For Existing Analytics Rows

This uses current message tags and the default lead tags.

```sql
WITH customer_tag_rollup AS (
    SELECT
        m.conversation_id,
        COUNT(*) FILTER (WHERE m.message_side = 'user_to_system') AS customer_message_count,
        ARRAY(
            SELECT DISTINCT tag
            FROM messages m2
            CROSS JOIN LATERAL unnest(COALESCE(m2.tags, ARRAY[]::TEXT[])) AS tag
            WHERE m2.conversation_id = m.conversation_id
              AND m2.message_side = 'user_to_system'
              AND tag = ANY(ARRAY[
                  'Product Query',
                  'Recommendation',
                  'Size Inquiry',
                  'Pricing Query',
                  'Discount Query',
                  'Delivery Query',
                  'Payment Options Query',
                  'Wholesale Inquiry'
              ]::TEXT[])
        ) AS lead_source_tags
    FROM messages m
    GROUP BY m.conversation_id
)
UPDATE conversation_analytics ca
SET
    is_lead = (
        r.customer_message_count > 2
        AND cardinality(r.lead_source_tags) > 0
    ),
    lead_type = CASE
        WHEN r.customer_message_count > 2
         AND 'Wholesale Inquiry' = ANY(r.lead_source_tags)
            THEN 'wholesale_interest'
        WHEN r.customer_message_count > 2
         AND cardinality(r.lead_source_tags) > 0
            THEN 'product_interest'
        ELSE NULL
    END,
    lead_status = CASE
        WHEN r.customer_message_count > 2
         AND cardinality(r.lead_source_tags) > 0
         AND ca.order_conversion_assisted = TRUE
            THEN 'converted'
        WHEN r.customer_message_count > 2
         AND cardinality(r.lead_source_tags) > 0
            THEN 'in_market'
        ELSE NULL
    END,
    lead_source_tags = CASE
        WHEN r.customer_message_count > 2
         AND cardinality(r.lead_source_tags) > 0
            THEN r.lead_source_tags
        ELSE ARRAY[]::TEXT[]
    END,
    lead_customer_message_count = COALESCE(r.customer_message_count, 0),
    lead_generated_at = CASE
        WHEN r.customer_message_count > 2
         AND cardinality(r.lead_source_tags) > 0
            THEN COALESCE(ca.lead_generated_at, NOW())
        ELSE NULL
    END,
    lead_details = jsonb_build_object(
        'rule', 'customer_message_count > 2 AND customer_message_tags overlap lead_generation_tags',
        'customer_message_count', COALESCE(r.customer_message_count, 0),
        'matched_lead_tags', COALESCE(r.lead_source_tags, ARRAY[]::TEXT[])
    )
FROM customer_tag_rollup r
WHERE ca.conversation_id = r.conversation_id;
```

## Pull Lead Count For UI

```sql
SELECT
    COUNT(*) AS total_leads,
    COUNT(*) FILTER (WHERE lead_status = 'converted') AS converted_leads,
    COUNT(*) FILTER (WHERE lead_status = 'in_market') AS in_market_leads,
    COUNT(*) FILTER (WHERE lead_type = 'product_interest') AS product_interest_leads,
    COUNT(*) FILTER (WHERE lead_type = 'wholesale_interest') AS wholesale_interest_leads,
    COUNT(*) FILTER (WHERE lead_type = 'preorder_interest') AS preorder_interest_leads
FROM conversation_analytics
WHERE client_id = $1::uuid
  AND is_lead = TRUE
  AND lead_generated_at >= $2::timestamptz
  AND lead_generated_at < $3::timestamptz;
```

## Pull Full Lead Data For UI, First Page

Use keyset pagination instead of `OFFSET`.

This query first picks a small page from `conversation_analytics`, then joins only those rows to `conversations`.

```sql
WITH lead_page AS (
    SELECT
        ca.conversation_id,
        ca.client_id,
        ca.phone_number,
        ca.is_lead,
        ca.lead_type,
        ca.lead_status,
        ca.lead_source_tags,
        ca.lead_customer_message_count,
        ca.lead_generated_at,
        ca.lead_details,
        ca.order_conversion_assisted,
        ca.converted_order_ids,
        ca.conversion_detected_via,
        ca.customer_satisfaction,
        ca.bot_effectiveness,
        ca.message_count,
        ca.analyzed_at
    FROM conversation_analytics ca
    WHERE ca.client_id = $1::uuid
      AND ca.is_lead = TRUE
      AND ca.lead_generated_at >= $2::timestamptz
      AND ca.lead_generated_at < $3::timestamptz
    ORDER BY ca.lead_generated_at DESC, ca.conversation_id DESC
    LIMIT $4
)
SELECT
    lp.conversation_id::text,
    lp.client_id::text,
    lp.phone_number,
    lp.is_lead,
    lp.lead_type,
    lp.lead_status,
    lp.lead_source_tags,
    lp.lead_customer_message_count,
    lp.lead_generated_at,
    lp.lead_details,
    lp.order_conversion_assisted,
    lp.converted_order_ids,
    lp.conversion_detected_via,
    lp.customer_satisfaction,
    lp.bot_effectiveness,
    lp.message_count,
    lp.analyzed_at,
    c.created_at AS conversation_started_at,
    c.updated_at AS conversation_last_activity_at,
    c.channel_type,
    c.first_message
FROM lead_page lp
JOIN conversations c ON c.conversation_id = lp.conversation_id
ORDER BY lp.lead_generated_at DESC, lp.conversation_id DESC;
```

Return these two values from the last row to fetch the next page:

```text
last_lead_generated_at
last_conversation_id
```

## Pull Full Lead Data For UI, Next Page

```sql
WITH lead_page AS (
    SELECT
        ca.conversation_id,
        ca.client_id,
        ca.phone_number,
        ca.is_lead,
        ca.lead_type,
        ca.lead_status,
        ca.lead_source_tags,
        ca.lead_customer_message_count,
        ca.lead_generated_at,
        ca.lead_details,
        ca.order_conversion_assisted,
        ca.converted_order_ids,
        ca.conversion_detected_via,
        ca.customer_satisfaction,
        ca.bot_effectiveness,
        ca.message_count,
        ca.analyzed_at
    FROM conversation_analytics ca
    WHERE ca.client_id = $1::uuid
      AND ca.is_lead = TRUE
      AND ca.lead_generated_at >= $2::timestamptz
      AND ca.lead_generated_at < $3::timestamptz
      AND (
          ca.lead_generated_at,
          ca.conversation_id
      ) < (
          $4::timestamptz,
          $5::uuid
      )
    ORDER BY ca.lead_generated_at DESC, ca.conversation_id DESC
    LIMIT $6
)
SELECT
    lp.conversation_id::text,
    lp.client_id::text,
    lp.phone_number,
    lp.is_lead,
    lp.lead_type,
    lp.lead_status,
    lp.lead_source_tags,
    lp.lead_customer_message_count,
    lp.lead_generated_at,
    lp.lead_details,
    lp.order_conversion_assisted,
    lp.converted_order_ids,
    lp.conversion_detected_via,
    lp.customer_satisfaction,
    lp.bot_effectiveness,
    lp.message_count,
    lp.analyzed_at,
    c.created_at AS conversation_started_at,
    c.updated_at AS conversation_last_activity_at,
    c.channel_type,
    c.first_message
FROM lead_page lp
JOIN conversations c ON c.conversation_id = lp.conversation_id
ORDER BY lp.lead_generated_at DESC, lp.conversation_id DESC;
```

## Pull Leads By Status

```sql
WITH lead_page AS (
    SELECT
        ca.conversation_id,
        ca.client_id,
        ca.phone_number,
        ca.is_lead,
        ca.lead_type,
        ca.lead_status,
        ca.lead_source_tags,
        ca.lead_customer_message_count,
        ca.lead_generated_at,
        ca.lead_details,
        ca.order_conversion_assisted,
        ca.converted_order_ids,
        ca.conversion_detected_via,
        ca.customer_satisfaction,
        ca.bot_effectiveness,
        ca.message_count,
        ca.analyzed_at
    FROM conversation_analytics ca
    WHERE ca.client_id = $1::uuid
      AND ca.is_lead = TRUE
      AND ca.lead_status = $2
      AND ca.lead_generated_at >= $3::timestamptz
      AND ca.lead_generated_at < $4::timestamptz
    ORDER BY ca.lead_generated_at DESC, ca.conversation_id DESC
    LIMIT $5
)
SELECT
    lp.conversation_id::text,
    lp.client_id::text,
    lp.phone_number,
    lp.is_lead,
    lp.lead_type,
    lp.lead_status,
    lp.lead_source_tags,
    lp.lead_customer_message_count,
    lp.lead_generated_at,
    lp.lead_details,
    lp.order_conversion_assisted,
    lp.converted_order_ids,
    lp.conversion_detected_via,
    lp.customer_satisfaction,
    lp.bot_effectiveness,
    lp.message_count,
    lp.analyzed_at,
    c.created_at AS conversation_started_at,
    c.updated_at AS conversation_last_activity_at,
    c.channel_type,
    c.first_message
FROM lead_page lp
JOIN conversations c ON c.conversation_id = lp.conversation_id
ORDER BY lp.lead_generated_at DESC, lp.conversation_id DESC;
```
