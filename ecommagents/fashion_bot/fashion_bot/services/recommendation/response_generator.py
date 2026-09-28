"""
Response Generator — LLM Call #2.

Takes reranked product results and generates a natural language response
with "why" reasoning for each recommendation.
"""

import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


async def generate_response(
    products: List[Dict[str, Any]],
    user_query: str,
    filters_applied: Dict[str, Any],
    clarifying_question: Optional[str] = None,
    max_products: int = 8,
) -> Dict[str, Any]:
    """
    LLM Call #2: Generate natural language response with product recommendations.

    Args:
        products: Reranked product dicts (with content + metadata)
        user_query: Original user query
        filters_applied: Filters that were applied
        clarifying_question: Optional follow-up question from query understanding
        max_products: Max products to include in response

    Returns:
        Dict with 'text' (NL response), 'products' (structured list), 'clarifying_question'
    """
    products = products[:max_products]

    if not products:
        msg = "I couldn't find products matching your criteria. Could you try a broader search or different filters?"
        if clarifying_question:
            msg += f"\n\n{clarifying_question}"
        return {
            "text": msg,
            "products": [],
            "clarifying_question": clarifying_question,
        }

    product_summaries = []
    structured_products = []
    for i, p in enumerate(products):
        content = p.get("content", {})
        metadata = p.get("metadata", {})
        summary = (
            f"{i+1}. {content.get('title', 'Unknown')} "
            f"(₹{content.get('price_min', '?')})"
        )
        details = []
        if content.get("material"):
            details.append(f"material: {content['material']}")
        if content.get("occasion"):
            details.append(f"occasion: {', '.join(content['occasion'])}")
        if content.get("style"):
            details.append(f"style: {', '.join(content['style'])}")
        if content.get("colors"):
            details.append(f"colors: {', '.join(content['colors'][:3])}")
        if content.get("sizes"):
            details.append(f"sizes: {', '.join(content['sizes'][:6])}")
        if details:
            summary += f" — {'; '.join(details)}"
        product_summaries.append(summary)

        structured_products.append({
            "product_id": metadata.get("product_id", ""),
            "title": content.get("title", ""),
            "price": content.get("price_min", 0),
            "compare_at_price": content.get("compare_at_price_min"),
            "image_url": metadata.get("image_url", ""),
            "product_url": metadata.get("product_url", ""),
            "in_stock": content.get("in_stock", True),
            "available_sizes": content.get("sizes", []),
            "colors": content.get("colors", []),
            "score": p.get("score", 0),
        })

    products_text = "\n".join(product_summaries)

    system_prompt = """You are a friendly e-commerce shopping assistant. Given product results and the user's query, write a helpful response.

RULES:
- Be concise and conversational
- For each product, add a brief "why" reason (1 sentence) explaining why it fits the query
- Mention price and key attributes
- If there's a clarifying question, ask it naturally at the end
- Do NOT use markdown headers or bullet points — write in flowing prose with numbered items
- Keep the total response under 300 words"""

    user_message = f"""User asked: "{user_query}"
Filters applied: {json.dumps(filters_applied)}
{f'Clarifying question to ask: {clarifying_question}' if clarifying_question else ''}

Products found:
{products_text}

Write a helpful response recommending these products."""

    try:
        from fashion_bot.core.llm_config import get_smaller_llm_config
        from fashion_bot.core.llm_factory import LLMFactory
        from langchain_core.messages import SystemMessage, HumanMessage

        # max_tokens sized for the full 8-product listing (max_products default).
        # Each item ≈ 60–100 tokens (title + reasoning + URL) plus a preface and an
        # optional clarifying question — the previous 600-token budget was reliably
        # exhausted around items 4–5, producing replies that ended mid-URL / mid-word
        # (AGENT_TECHNICAL_ERROR in daily QA). 1500 leaves comfortable headroom
        # while staying well under the model's context and the 300-word style cap.
        llm = LLMFactory.get_llm(
            tool_name="recommendation_response",
            override_config=get_smaller_llm_config(temperature=0.7, max_tokens=1500),
        )

        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])

        return {
            "text": response.content.strip(),
            "products": structured_products,
            "clarifying_question": clarifying_question,
        }

    except Exception as e:
        logger.error(f"Response generation failed: {e}")
        fallback_text = f"Here are {len(structured_products)} products matching '{user_query}':"
        for sp in structured_products:
            fallback_text += f"\n• {sp['title']} (₹{sp['price']})"
        return {
            "text": fallback_text,
            "products": structured_products,
            "clarifying_question": clarifying_question,
        }
