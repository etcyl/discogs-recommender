import json
import logging
import re

import anthropic
import httpx

logger = logging.getLogger(__name__)

VALID_PROVIDERS = {"claude-sonnet", "claude-haiku", "ollama"}


class LLMError(Exception):
    """Raised when an LLM call fails."""


def _scan_top_level_objects(text: str, start: int) -> list[str]:
    """Return the source of each complete `{...}` directly inside an array.

    Walks the text tracking string/escape state and brace depth, so brackets
    and braces that appear *inside* string values (song titles like
    "Blue Monday [12'' Mix]") don't confuse the scan. Anything after a
    truncation point is simply not returned.
    """
    chunks: list[str] = []
    depth = 0
    obj_start = -1
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        ch = text[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    chunks.append(text[obj_start:i + 1])
                    obj_start = -1
        elif ch == "]" and depth == 0:
            break  # end of the top-level array

    return chunks


def parse_llm_json(text: str) -> list[dict]:
    """Extract a JSON array from LLM output, handling common quirks.

    Local models (Ollama) often wrap JSON in markdown fences, add commentary,
    emit trailing commas, or get cut off by a token limit mid-object. This
    function recovers whatever complete objects it can find.
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

    # Locate the start of the array
    start = text.find("[")
    if start < 0:
        logger.warning("Failed to parse LLM JSON (len=%d): %.200s...", len(text), text)
        return []

    # Fix trailing commas before ] or } and retry the whole fragment
    cleaned = re.sub(r",\s*([}\]])", r"\1", text[start:])
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Salvage: pull out every complete top-level object, ignoring whatever
    # was cut off at the end. Objects that individually fail to parse are
    # skipped rather than sinking the whole batch.
    salvaged = []
    for chunk in _scan_top_level_objects(text, start):
        try:
            obj = json.loads(re.sub(r",\s*([}\]])", r"\1", chunk))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            salvaged.append(obj)

    if salvaged:
        logger.info("Salvaged %d objects from malformed LLM JSON (len=%d)",
                    len(salvaged), len(text))
        return salvaged

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
    response_schema: dict | None = None,
) -> str:
    """Send a prompt to the configured LLM provider and return the raw text response.

    response_schema: optional JSON Schema. Ollama uses it to constrain decoding,
    which is the difference between a local model reliably emitting a JSON array
    and it writing three paragraphs of prose first. Ignored by the Claude path,
    which follows the prompt's format instructions on its own.
    """
    if provider == "ollama":
        return _call_ollama(system_prompt, user_prompt, max_tokens, ollama_base_url,
                            ollama_model, response_schema=response_schema)
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


_THINKING_CACHE: dict[str, bool] = {}


def _is_thinking_model(base_url: str, model: str) -> bool:
    """Whether this Ollama model has a reasoning ("thinking") pass.

    Reasoning models (qwen3, deepseek-r1, gpt-oss, …) will happily spend an
    unbounded number of tokens deliberating before writing a single character
    of answer — `reasoning_effort: low` does not reliably bound it. For those
    models we turn thinking off outright.
    """
    if model in _THINKING_CACHE:
        return _THINKING_CACHE[model]
    try:
        resp = httpx.post(f"{base_url}/api/show", json={"model": model}, timeout=10.0)
        resp.raise_for_status()
        caps = resp.json().get("capabilities") or []
    except Exception:
        return False  # don't cache a transient failure
    thinking = "thinking" in caps
    _THINKING_CACHE[model] = thinking
    return thinking


def _call_ollama(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    base_url: str,
    model: str,
    response_schema: dict | None = None,
) -> str:
    """Call Ollama.

    With a response_schema we use the native /api/chat endpoint, which supports
    schema-constrained decoding (`format`). That is far more reliable than
    asking a local model to "return only JSON" — it makes malformed output and
    prose preambles structurally impossible, and it stops reasoning models from
    deliberating until they hit the token limit. Without a schema we fall back
    to the OpenAI-compatible endpoint.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    if response_schema is not None:
        url = f"{base_url}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "format": response_schema,
            "options": {"num_predict": max_tokens, "temperature": 0.8},
        }
        if _is_thinking_model(base_url, model):
            payload["think"] = False
    else:
        url = f"{base_url}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.8,
        }

    try:
        resp = httpx.post(url, json=payload, timeout=600.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        raise LLMError(
            f"Cannot connect to Ollama at {base_url}. "
            "Make sure Ollama is running (ollama serve) and the model is pulled."
        )
    except httpx.TimeoutException:
        raise LLMError("Ollama request timed out after 600 seconds.")
    except httpx.HTTPStatusError as e:
        raise LLMError(f"Ollama returned HTTP {e.response.status_code}: {e.response.text[:200]}")

    data = resp.json()
    if response_schema is not None:
        text = data.get("message", {}).get("content", "")
        logger.info("LLM [ollama/%s] — schema-constrained, eval_count: %s",
                    model, data.get("eval_count"))
        return text

    text = data["choices"][0]["message"]["content"]

    logger.info("LLM [ollama/%s] — tokens: %s", model, data.get("usage", {}))
    return text
