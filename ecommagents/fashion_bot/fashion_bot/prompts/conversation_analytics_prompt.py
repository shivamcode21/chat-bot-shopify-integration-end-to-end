"""
Central analytics prompt for post-hoc conversation analysis.

This prompt is sent to the LLM along with a full conversation transcript.
The LLM returns a structured JSON covering multiple analysis dimensions
(cancellation aversion, order conversion, satisfaction, etc.) in one pass.

Canonical source of truth for the default prompt.
In production the prompt is loaded from the DB (agents_config, agent_name =
'conversation_analytics') with this as the fallback.
"""

PROMPT_VERSION = "2.0"

SYSTEM_PROMPT = (
    "You are an expert e-commerce conversation analyst specializing in "
    "cancellation aversion, RTO prevention, and order lifecycle analysis. "
    "You read customer-support chat transcripts and extract structured insights. "
    "You are extremely attentive to cancellation signals — even subtle ones. "
    "Always return valid JSON. Never include text outside the JSON object."
)

USER_PROMPT_TEMPLATE = """Analyze the following customer-support conversation and extract insights across every dimension listed below.

== Conversation ==
{conversation_text}

== Metadata ==
- Client ID: {client_id}
- Phone: {phone}
- Message count: {message_count}
- Conversation duration: {duration_minutes} minutes

== Post-Conversation Orders (data-driven) ==
{post_conversation_orders}

== CRITICAL ANALYSIS RULES ==

1. CANCELLATION AVERSION — READ VERY CAREFULLY:
   A cancellation is "attempted" when the customer says ANYTHING indicating they want to cancel,
   including but not limited to:
     - "cancel my order", "I want to cancel", "can I cancel"
     - "I don't want this anymore", "please remove this order"
     - "I changed my mind", "refund please"

   A cancellation is "averted" when the customer expressed cancellation intent BUT the order
   was NOT actually cancelled. Instead, the customer accepted an ALTERNATIVE such as:
     - Size change / variant change (customer wanted to cancel but changed size instead)
     - Exchange (customer pivoted from cancel to exchange)
     - Address correction (customer wanted to cancel due to wrong address, bot fixed it)
     - Name correction
     - Product swap
     - Discount or offer
     - The customer simply said "don't cancel" or "never mind" after initially asking to cancel

   IMPORTANT: If in the SAME conversation the customer cancels one order but RETAINS another
   order (by updating it instead of cancelling), that counts as cancellation averted for the
   retained order.

   EXAMPLE of cancellation aversion:
     Customer: "can I cancel my order GV14216"
     Bot: "To cancel, please share the reason"
     Customer: "I want to exchange maybe"
     Bot: "What size would you like?"
     Customer: "I want to change size and NOT cancel my order"
     Bot: "Size updated from L to S"
     → cancellation_attempted = TRUE, cancellation_averted = TRUE, aversion_method = "product_change"

2. ORDER UPDATES — RTO PREVENTION:
   Any successful order modification counts as an update. Look for confirmation messages like:
     - "has been updated", "changed from X to Y", "updated to", "updated successfully"
   Types: address, name, phone, email, size, product/variant
   Address corrections and size changes PREVENT RTO (return to origin) — flag them as updates.
   If the bot confirms an update was made, update_performed = true, even if there were errors
   along the way that were later resolved.

3. ORDER CONVERSION - PRE-SALES ORDER ASSISTANCE:
   Mark conversion_assisted = true when either condition is met:
     - The bot helped the customer place a new order or complete a purchase in the transcript.
     - The Post-Conversation Orders section shows one or more orders created within 12 hours
       after a qualified pre-sales exchange: more than 3 customer messages in the conversation
       and at least one configured pre-sales/preorder tag such as product recommendation, size/price/discount,
       availability, delivery estimate, payment options, preorder, or wholesale inquiry.
   If a customer had a qualified pre-sales exchange, ordered, and then continued in the same
   conversation with post-order support/order queries, the order can still be assisted.
   Do NOT mark conversion_assisted = true for pure post-order support/order queries such as
   payment stuck after checkout, order status, tracking, cancellation, return/exchange, or
   order update conversations where there was no configured pre-sales/preorder tag.
   Do NOT mark conversion_assisted = true for product browsing, size/price/discount guidance,
   add-to-cart, availability checks, purchase intent, or support escalation if the bot did not
   actually assist order placement/checkout and there is no post-chat order.

4. BOT EFFECTIVENESS:
   - "resolved" = ALL customer requests were handled successfully
   - "partially_resolved" = SOME requests handled, some failed or had errors
   - "unresolved" = customer's main issue was NOT addressed
   If the bot had errors but eventually completed the task, that is "partially_resolved" or
   "resolved" depending on whether all tasks succeeded in the end.

== Output Schema ==

Return ONLY a valid JSON object (no markdown fences, no commentary) with this exact schema:

{{
  "cancellation_analysis": {{
    "cancellation_attempted": true | false,
    "cancellation_averted": true | false,
    "aversion_method": "persuasion" | "discount_offered" | "exchange" | "return" | "address_fix" | "name_fix" | "product_change" | "size_change" | "context_shift" | "escalation" | null,
    "cancellation_reason": "brief reason the customer wanted to cancel, or null",
    "order_ids_involved": ["list of order IDs mentioned in cancellation context"]
  }},
  "order_conversion": {{
    "conversion_assisted": true | false,
    "conversion_method": "product_recommendation" | "discount" | "size_guidance" | "availability_check" | "reorder" | null,
    "post_chat_order_linked": true | false
  }},
  "order_updates": {{
    "update_performed": true | false,
    "update_types": ["address", "phone", "email", "name", "size", "product"],
    "order_ids_updated": ["list of order IDs that were updated"],
    "rto_prevention": true | false
  }},
  "customer_satisfaction": {{
    "sentiment": "positive" | "neutral" | "negative",
    "evidence": "one-sentence justification"
  }},
  "bot_effectiveness": {{
    "verdict": "resolved" | "partially_resolved" | "unresolved",
    "evidence": "one-sentence justification"
  }},
  "escalation": {{
    "needed": true | false,
    "reason": "string or null"
  }},
  "key_actions": ["list of significant actions taken during conversation, include order IDs"],
  "confidence": 0.0 to 1.0,
  "reasoning": "2-3 sentence summary of what happened in the conversation and the outcome"
}}

== Definitions ==
  cancellation_attempted  = customer expressed ANY intent to cancel an order at any point
  cancellation_averted    = despite expressing cancellation intent, the order was NOT cancelled;
                            the customer was retained via an alternative action
  aversion_method         = HOW the cancellation was averted (size_change if customer changed size
                            instead of cancelling, exchange if they chose exchange, address_fix if
                            wrong address was the reason and bot fixed it, etc.)
  order_ids_involved      = all order IDs mentioned when customer discussed cancellation
  conversion_assisted     = bot helped the customer place a new order or complete a purchase,
                            or a post-chat order followed a qualified pre-sales conversation.
                            Do not count pure post-order support/order-status/payment-stuck
                            conversations as assisted, but later support messages do not erase
                            a qualified earlier pre-sales exchange.
  post_chat_order_linked  = true ONLY if the Post-Conversation Orders section shows orders
  update_performed        = bot successfully changed ANY order detail (address, name, size, etc.)
  rto_prevention          = true if address/name/size corrections were made (these prevent
                            failed deliveries / return-to-origin)
  order_ids_updated       = order IDs for which updates were successfully made
  sentiment               = overall emotional tone of the customer
  bot_effectiveness       = whether the bot resolved ALL of the customer's requests
  escalation.needed       = conversation was handed off to a human agent or should have been
"""
