"""Parse uploaded files (text/PDF) into structured track lists using Claude."""
import json
import logging

from services.llm_provider import call_llm

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 2 * 1024 * 1024       # 2 MB
MAX_TEXT_LENGTH = 50_000               # characters sent to LLM
ALLOWED_CONTENT_TYPES = {"text/plain", "application/pdf"}


class UploadParseError(Exception):
    """Raised when file parsing fails."""


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    import io
    import pdfplumber

    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages[:50]:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


def parse_tracks_with_claude(text: str, api_key: str,
                             ai_model: str = "claude-sonnet") -> list[dict]:
    """Use LLM to extract structured track/artist/album data from freeform text."""
    if not text or not text.strip():
        raise UploadParseError("File appears to be empty")

    text = text[:MAX_TEXT_LENGTH]

    system_prompt = """You extract music tracks, songs, albums, and artists from text.
Return a JSON array of objects with keys: "artist", "title", "album", "year"
If you can't determine a field, use an empty string.
Deduplicate entries. Maximum 100 items.
Return ONLY the JSON array, no other text."""

    user_prompt = f"""Extract all music references from this text. The text could be a playlist,
a list of favorite albums, concert setlists, or any format containing music references.

TEXT TO PARSE:
{text}"""

    response_text = call_llm(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        provider=ai_model,
        max_tokens=4000,
        anthropic_api_key=api_key,
    )

    try:
        tracks = json.loads(response_text)
    except json.JSONDecodeError:
        start = response_text.find("[")
        end = response_text.rfind("]") + 1
        if start >= 0 and end > start:
            tracks = json.loads(response_text[start:end])
        else:
            raise UploadParseError("Could not parse tracks from file content")

    if not isinstance(tracks, list) or len(tracks) == 0:
        raise UploadParseError("No tracks found in the uploaded file")

    clean_tracks = []
    for t in tracks[:100]:
        if not isinstance(t, dict):
            continue
        artist = str(t.get("artist", "")).strip()[:200]
        title = str(t.get("title", "")).strip()[:200]
        if not artist and not title:
            continue
        clean_tracks.append({
            "artist": artist or "Unknown",
            "title": title or artist,
            "album": str(t.get("album", "")).strip()[:200],
            "year": str(t.get("year", "")).strip()[:10],
        })

    if not clean_tracks:
        raise UploadParseError("No valid tracks extracted from file")

    logger.info("Upload parse: extracted %d tracks from file", len(clean_tracks))
    return clean_tracks
