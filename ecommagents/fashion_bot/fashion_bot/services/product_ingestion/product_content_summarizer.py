"""
Compresses Upstash Search `content` payloads that exceed the 4096-character
field limit before upsert.

Strategy:
1. Serialize content to JSON. If under the soft target, return unchanged.
2. Collect long string values (top-level + metafield_attributes) and ask an
   LLM (LLM_MODEL env var) to summarize them in one call, preserving
   searchable keywords and stripping HTML/repetition/stopwords.
3. Slot compressed values back into the content dict.
4. If still over 4096 (LLM failed, returned malformed JSON, or the
   non-string fields alone exceed the limit), apply deterministic trimming:
   drop the largest metafield_attributes entries, then truncate the longest
   remaining string fields, until content fits.

All I/O methods are async per AGENTS.md §1. Pure-CPU helpers stay sync.
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fashion_bot.core.llm_factory import LLMInvoker

logger = logging.getLogger(__name__)

UPSTASH_CONTENT_LIMIT = 4096
COMPRESSION_TARGET = 3800
LONG_STRING_THRESHOLD = 150
COMPRESSED_VALUE_HARD_CAP = 200

_SYSTEM_PROMPT = (
    "You compress product-catalog text for a search index. "
    "Input is a JSON object mapping opaque ids to long string values "
    "(product descriptions, ingredients, how-to-use, FAQs, etc.). "
    "Return a JSON object with the same ids and compressed string values. "
    "Rules:\n"
    "- PRESERVE searchable / filterable keywords: brand names, materials, "
    "ingredients, colors, sizes, fits, occasions, technical specs, feature words.\n"
    "- DROP marketing fluff, repetition, calls-to-action, and common stopwords.\n"
    "- STRIP raw HTML tags. Keep only the readable text. If the HTML contains "
    "no useful information, return an empty string for that id.\n"
    f"- Each compressed value must be at most {COMPRESSED_VALUE_HARD_CAP} characters.\n"
    "- Output a single JSON object only. No markdown fences, no commentary."
)


class ProductContentSummarizer:
    """Compresses oversize Upstash Search `content` dicts in-place."""

    async def acompress_documents(
        self, documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Mutate documents whose `content` JSON exceeds the Upstash limit.

        Compresses oversize documents concurrently via ``asyncio.gather``.
        """
        oversize = [
            (i, self._content_size(doc["content"]))
            for i, doc in enumerate(documents)
            if isinstance(doc.get("content"), dict)
            and self._content_size(doc["content"]) > UPSTASH_CONTENT_LIMIT
        ]

        if not oversize:
            return documents

        logger.info(
            f"🪄 Content compression: {len(oversize)}/{len(documents)} "
            f"documents exceed {UPSTASH_CONTENT_LIMIT}-char limit"
        )

        await asyncio.gather(
            *(self._acompress_one(documents[i], original_size) for i, original_size in oversize)
        )
        return documents

    async def _acompress_one(self, doc: Dict[str, Any], original_size: int) -> None:
        doc_id = doc.get("id", "unknown")
        try:
            doc["content"] = await self._acompress_content(doc["content"])
            logger.info(
                f"🪄 Compressed content for {doc_id}: "
                f"{original_size} → {self._content_size(doc['content'])} chars"
            )
        except Exception as e:
            logger.error(
                f"❌ Content compression failed for {doc_id}: {e}; "
                f"applying deterministic fallback only"
            )
            doc["content"] = self._deterministic_shrink(doc["content"])
            logger.info(
                f"🪄 Fallback-shrank content for {doc_id}: "
                f"{original_size} → {self._content_size(doc['content'])} chars"
            )

    async def _acompress_content(self, content: Dict[str, Any]) -> Dict[str, Any]:
        long_values, id_to_path = self._collect_long_strings(content)

        if long_values:
            compressed_map = await self._acall_llm(long_values)
            if compressed_map:
                content = self._apply_compressed_values(content, compressed_map, id_to_path)

        if self._content_size(content) <= UPSTASH_CONTENT_LIMIT:
            return content

        return self._deterministic_shrink(content)

    async def _acall_llm(self, long_values: Dict[str, str]) -> Optional[Dict[str, str]]:
        user_prompt = (
            "Compress each value in this JSON object per the rules. "
            "Return the same JSON object with the same keys.\n\n"
            f"{json.dumps(long_values, ensure_ascii=False)}"
        )
        try:
            response_text = await LLMInvoker.ainvoke(
                prompt=user_prompt,
                tool_name="product_content_summarizer",
                system_prompt=_SYSTEM_PROMPT,
                config={"callbacks": []},
            )
        except Exception as e:
            logger.warning(f"⚠️ Content summarizer LLM call failed: {e}")
            return None

        parsed = self._parse_json_response(response_text)
        if not isinstance(parsed, dict):
            logger.warning(
                f"⚠️ Content summarizer returned non-dict: "
                f"{str(parsed)[:120]!r}"
            )
            return None

        return {
            k: (v[:COMPRESSED_VALUE_HARD_CAP] if isinstance(v, str) else "")
            for k, v in parsed.items()
            if k in long_values
        }

    @staticmethod
    def _content_size(content: Dict[str, Any]) -> int:
        return len(json.dumps(content, default=str).encode("utf-8"))

    @staticmethod
    def _collect_long_strings(
        content: Dict[str, Any],
    ) -> Tuple[Dict[str, str], Dict[str, Tuple[str, ...]]]:
        """Return (id->value, id->path) for string values >= threshold."""
        long_values: Dict[str, str] = {}
        id_to_path: Dict[str, Tuple[str, ...]] = {}
        counter = 0

        for key, value in content.items():
            if key == "metafield_attributes" and isinstance(value, dict):
                for sub_key, sub_val in value.items():
                    if isinstance(sub_val, str) and len(sub_val) >= LONG_STRING_THRESHOLD:
                        counter += 1
                        slot = f"f{counter}"
                        long_values[slot] = sub_val
                        id_to_path[slot] = ("metafield_attributes", sub_key)
            elif isinstance(value, str) and len(value) >= LONG_STRING_THRESHOLD:
                counter += 1
                slot = f"f{counter}"
                long_values[slot] = value
                id_to_path[slot] = (key,)

        return long_values, id_to_path

    @staticmethod
    def _parse_json_response(text: str) -> Any:
        text = (text or "").strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    return None
            return None

    @staticmethod
    def _apply_compressed_values(
        content: Dict[str, Any],
        compressed: Dict[str, str],
        id_to_path: Dict[str, Tuple[str, ...]],
    ) -> Dict[str, Any]:
        for slot, new_value in compressed.items():
            path = id_to_path.get(slot)
            if not path:
                continue
            if len(path) == 1:
                content[path[0]] = new_value
            elif len(path) == 2 and isinstance(content.get(path[0]), dict):
                content[path[0]][path[1]] = new_value
        return content

    def _deterministic_shrink(self, content: Dict[str, Any]) -> Dict[str, Any]:
        """Trim content to fit under the limit using fixed rules. Never raises."""
        attrs = content.get("metafield_attributes")
        if isinstance(attrs, dict) and attrs:
            for k, v in list(attrs.items()):
                if isinstance(v, str) and len(v) > 80:
                    attrs[k] = v[:80]
            if self._content_size(content) <= UPSTASH_CONTENT_LIMIT:
                return content

            sized = sorted(
                ((k, len(json.dumps(v, ensure_ascii=False, default=str))) for k, v in attrs.items()),
                key=lambda kv: kv[1],
                reverse=True,
            )
            for k, _ in sized:
                attrs.pop(k, None)
                if self._content_size(content) <= UPSTASH_CONTENT_LIMIT:
                    return content
            content.pop("metafield_attributes", None)

        if self._content_size(content) <= UPSTASH_CONTENT_LIMIT:
            return content

        desc = content.get("description")
        if isinstance(desc, str) and len(desc) > 100:
            content["description"] = desc[:100]
        if self._content_size(content) <= UPSTASH_CONTENT_LIMIT:
            return content

        for k in list(content.keys()):
            v = content[k]
            if isinstance(v, list) and len(v) > 10:
                content[k] = v[:10]
            if self._content_size(content) <= UPSTASH_CONTENT_LIMIT:
                return content

        for k in list(content.keys()):
            v = content[k]
            if isinstance(v, str) and len(v) > 50 and k not in ("title", "brand", "category"):
                content[k] = v[:50]
            if self._content_size(content) <= UPSTASH_CONTENT_LIMIT:
                return content

        logger.warning(
            f"⚠️ Could not shrink content under {UPSTASH_CONTENT_LIMIT} chars "
            f"even after fallback (final size={self._content_size(content)}); "
            f"upstash will likely reject this document"
        )
        return content
