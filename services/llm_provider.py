import json
import logging
import re

import anthropic
import httpx

logger = logging.getLogger(__name__)

VALID_PROVIDERS = {"claude-sonnet", "claude-haiku", "ollama"}


class LLMError(Exception):
    """Raised when an LLM call fails."""


def parse_llm_json(text: str) -> list[dict]:
    """Extract a JSON array from LLM output, handling common quirks.

    Local models (Ollama) often wrap JSON in markdown fences, add commentary,
    or produce trailing commas. This function handles those cases.
    """
    # Strip markdown code fences
    text = re.sub(r"```(?:json)?\s*\n?", "", text).strip()

    # Try direct parse first
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Extract the JSON array portion
    start = text.find("[")
    if start < 0:
        logger.warning("Failed to parse LLM JSON (len=%d): %.200s...", len(text), text)
        return []

    end = text.rfind("]")
    fragment = text[start:end + 1] if end > start else text[start:]

    # Try direct parse of fragment
    try:
        return json.loads(fragment)
    except json.JSONDecodeError:
        pass

    # Fix trailing commas before ] or }
    cleaned = re.sub(r",\s*([}\]])", r"\1", fragment)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Truncation recovery: if the array was cut off mid-object, try to
    # salvage the valid objects before the break.
    last_brace = cleaned.rfind("}")
    if last_brace > 0:
        truncated = cleaned[:last_brace + 1] + "]"
        # Remove trailing comma before the new ]
        truncated = re.sub(r",\s*\]$", "]", truncated)
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse LLM JSON (len=%d): %.200s...", len(text), text)
    return []


def call_llm(
    system_prompt: str,
    user_prompt: str,
    provider: str = "claude-sonnet",
    max_tokens: int = 6000,
    anthropic_api_key: str = "",
    ollama_base_url: str = "http://localhost:11434",
    ollama_model: str = "llama3.1:8b",
) -> str:
    """Send a prompt to the configured LLM provider and return the raw text response."""
    if provider == "ollama":
        return _call_ollama(system_prompt, user_prompt, max_tokens, ollama_base_url, ollama_model)
    elif provider == "claude-haiku":
        return _call_claude(system_prompt, user_prompt, max_tokens, anthropic_api_key,
                            model="claude-haiku-4-5-20251001", use_cache=False)
    else:
        return _call_claude(system_prompt, user_prompt, max_tokens, anthropic_api_key,
                            model="claude-sonnet-4-20250514", use_cache=True)


def _call_claude(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    api_key: str,
    model: str,
    use_cache: bool = True,
) -> str:
    if not api_key:
        raise LLMError("Anthropic API key is not configured. Set ANTHROPIC_API_KEY in .env or switch to Ollama.")

    client = anthropic.Anthropic(api_key=api_key)

    system_block = [{"type": "text", "text": system_prompt}]
    if use_cache:
        system_block[0]["cache_control"] = {"type": "ephemeral"}

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_block,
        messages=[{"role": "user", "content": user_prompt}],
    )

    u = message.usage
    cached = getattr(u, "cache_read_input_tokens", 0) or 0
    logger.info("LLM [%s] — in:%d out:%d cached:%d", model, u.input_tokens, u.output_tokens, cached)

    return message.content[0].text


def _call_ollama(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    base_url: str,
    model: str,
) -> str:
    """Call Ollama via its OpenAI-compatible API endpoint."""
    url = f"{base_url}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.8,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=180.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        raise LLMError(
            f"Cannot connect to Ollama at {base_url}. "
            "Make sure Ollama is running (ollama serve) and the model is pulled."
        )
    except httpx.TimeoutException:
        raise LLMError("Ollama request timed out after 180 seconds.")
    except httpx.HTTPStatusError as e:
        raise LLMError(f"Ollama returned HTTP {e.response.status_code}: {e.response.text[:200]}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]

    logger.info("LLM [ollama/%s] — tokens: %s", model, data.get("usage", {}))
    return text
