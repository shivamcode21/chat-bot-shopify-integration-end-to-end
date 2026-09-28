"""
Prompt for the second-opinion attribution verification pass.

Used when conversation_analytics's LLM already marked an order as
conversion_assisted via free-form inference (conversion_detected_via =
"llm_inferred", not the deterministic pre-sales-tag gate) and the caller
wants a focused, single-purpose re-check before trusting that verdict.
"""

PROMPT_VERSION = "1.0"

SYSTEM_PROMPT = (
    "You are an expert e-commerce attribution auditor. Given a customer-support "
    "chat transcript and an order it was tentatively linked to, decide whether "
    "the transcript shows genuine pre-sales engagement that plausibly drove that "
    "order, or whether the order link is a false positive. "
    "Always return valid JSON. Never include text outside the JSON object."
)

USER_PROMPT_TEMPLATE = """Decide whether the conversation below shows genuine pre-sales engagement \
(product discovery, or size/discount/pricing discussion for something not yet purchased) that \
plausibly drove order {order_number} — versus pre-sales-flavored language that is incidental to \
what is really a post-order service request (cancellation, return, or delivery issue) that only \
mentions size/product in passing.

== How this was flagged ==
- Detection method: {conversion_detected_via}
- Original LLM reasoning for marking this as an assisted conversion:
{existing_llm_reasoning}

== Transcript ==
{transcript_text}

== Decision rules ==
1. REJECT when the terminal (last) substantive customer message is a post-order category
   (cancellation, return, exchange, delivery issue, order status) AND the transcript/reasoning
   never actually names order {order_number} or the products in it.
2. CONFIRM when there is real product-discovery signal (browsing, size/price/discount questions
   about something not yet bought) that plausibly led to this order, OR the order/its products
   are explicitly referenced in the transcript.
3. Base the decision on what the customer actually discussed, not on the tags alone — tags are a
   hint, not proof.

== Output Schema ==

Return ONLY a valid JSON object (no markdown fences, no commentary) with this exact schema:

{{
  "verdict": "confirmed" | "rejected",
  "confidence": 0.0 to 1.0,
  "reasoning": "1-3 sentences explaining the verdict, referencing what was or wasn't discussed"
}}
"""
