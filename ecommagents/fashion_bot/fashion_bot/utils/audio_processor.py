import os
import logging
import asyncio
from openai import AsyncOpenAI
from pathlib import Path
from typing import Optional

from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)

# Lazy OpenAI client - initialized on first use
_client = None

def _get_openai_client():
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client

def extract_audio_url(data: dict) -> Optional[str]:
    """Extract audio URL from Gupshup webhook payload."""
    try:
        payload = data.get("payload", {})
        if payload.get("type") == "audio":
            return payload.get("payload", {}).get("url")
    except Exception as e:
        logger.error(f"Error extracting audio URL: {e}")
    return None

async def download_audio(url: str, trace_id: str) -> Path:
    """Download audio file from URL with retry logic."""
    temp_dir = Path("/tmp/fashion_bot_audio")
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / f"{trace_id}_voice.ogg"

    for attempt in range(3):
        try:
            client = await get_shared_async_http_client()
            response = await client.get(url, timeout=30)
            response.raise_for_status()
            file_path.write_bytes(response.content)
            return file_path
        except Exception as e:
            if attempt == 2:
                logger.error(f"Failed to download audio after 3 attempts: {e}")
                raise
            await asyncio.sleep(1)
    
    raise Exception("Download failed")

async def speech_to_text(audio_path: Path) -> str:
    """
    Converts WhatsApp OGG/WAV audio → text using OpenAI Whisper.
    Works on Python 3.13.
    """
    try:
        with open(audio_path, "rb") as f:
            # Note: Whisper-1 is the standard model for transcriptions.
            # gpt-4o-mini is a chat model and doesn't support the transcriptions endpoint.
            transcript = await _get_openai_client().audio.transcriptions.create(
                model="whisper-1",
                file=f,
            )
        return transcript.text.strip()
    except Exception as e:
        logger.error(f"OpenAI transcription failed: {e}")
        raise Exception(f"Failed to convert audio to text: {str(e)}")

async def gupshup_audio_to_text(webhook_data: dict, trace_id: str) -> Optional[str]:
    """Single entry point to convert Gupshup audio to text."""
    audio_url = extract_audio_url(webhook_data)
    if not audio_url:
        return None

    ogg_path = None
    try:
        logger.info(f"[{trace_id}] Processing audio message from {audio_url}")
        ogg_path = await download_audio(audio_url, trace_id)
        
        # Check file size (25MB limit for OpenAI Whisper)
        if ogg_path.stat().st_size > 25 * 1024 * 1024:
            logger.warning(f"[{trace_id}] Audio file too large: {ogg_path.stat().st_size} bytes")
            return "Audio message too long. Please send a shorter message."

        text = await speech_to_text(ogg_path)
        logger.info(f"[{trace_id}] Successfully transcribed audio: {text}")
        return text

    except Exception as e:
        logger.error(f"[{trace_id}] Audio processing error: {e}")
        raise e
    finally:
        # Cleanup
        if ogg_path and ogg_path.exists():
            ogg_path.unlink(missing_ok=True)
