# Lead Generation Execution Plan

## Goal

Mark a conversation as a lead when the customer shows buying interest and has had enough interaction with the bot.

## Simple Rule

A conversation becomes a lead when:

1. Customer has sent more than 2 messages.
2. At least one customer message has a lead-related tag.

## Tags To Use Today

Use these existing tags as lead signals:

- Product Query
- Recommendation
- Size Inquiry
- Pricing Query
- Discount Query
- Delivery Query
- Payment Options Query
- Wholesale Inquiry

Do not use `General Query` for now because it is too broad.

## Preorder Leads

Today we do not have a clear preorder tag.

To identify preorder leads properly, first add specific tags like:

- Preorder Inquiry
- Back in Stock Inquiry
- Availability Inquiry

Then a preorder lead can be marked when:

1. Customer has sent more than 2 messages.
2. Customer message tags include one of the preorder tags.

## What To Store

Store these fields so analytics can query them later:

- `is_lead`
- `lead_type`
- `lead_source_tags`
- `lead_customer_message_count`
- `lead_generated_at`

Example:

```json
{
  "is_lead": true,
  "lead_type": "product_interest",
  "lead_source_tags": ["Recommendation", "Size Inquiry"],
  "lead_customer_message_count": 4
}
```

## How To Execute

1. Add a global DB config for lead tags.

Lead tags are global, not client-specific. Store them in `global_configs`.

```json
{
  "lead_generation_tags": [
    "Product Query",
    "Recommendation",
    "Size Inquiry",
    "Pricing Query",
    "Discount Query",
    "Delivery Query",
    "Payment Options Query",
    "Wholesale Inquiry"
  ]
}
```

2. In the analytics cron, count customer messages.

Only count messages where:

```text
message_side = 'user_to_system'
```

3. Read customer message tags.

Check tags on customer messages first.

Use conversation-level tags only as a fallback.

4. Mark the conversation as a lead if the rule matches.

```text
customer_message_count > 2
AND
customer_message_tags overlap lead_generation_tags
```

5. Save the lead result in the analytics table or conversations table.

## Conversion Tags

Keep conversion tags separate.

Current conversion tags are:

- Nudged to Order
- Prevented Return
- Updated Order Details
- Prevented Cancellation

These describe the outcome of the conversation.

Lead tags describe customer interest.

## Recommended Order

1. Implement broad product-interest lead detection using existing tags.
2. Add specific preorder tags.
3. Implement preorder lead detection using those new tags.
