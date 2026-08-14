"""Tests for services/llm_provider.py."""
from unittest.mock import MagicMock, patch

import pytest

from services.llm_provider import call_llm, LLMError, parse_llm_json


class TestCallLlm:
    """Tests for the call_llm dispatcher."""

    @patch("services.llm_provider._call_claude")
    def test_defaults_to_claude_sonnet(self, mock_claude):
        mock_claude.return_value = "response"
        result = call_llm("sys", "user", anthropic_api_key="sk-ant-test")
        assert result == "response"
        mock_claude.assert_called_once_with(
            "sys", "user", 6000, "sk-ant-test",
            model="claude-sonnet-4-20250514", use_cache=True,
        )

    @patch("services.llm_provider._call_claude")
    def test_claude_haiku_provider(self, mock_claude):
        mock_claude.return_value = "response"
        call_llm("sys", "user", provider="claude-haiku", anthropic_api_key="sk-ant-test")
        mock_claude.assert_called_once_with(
            "sys", "user", 6000, "sk-ant-test",
            model="claude-haiku-4-5-20251001", use_cache=False,
        )

    @patch("services.llm_provider._call_ollama")
    def test_ollama_provider(self, mock_ollama):
        mock_ollama.return_value = "response"
        call_llm("sys", "user", provider="ollama",
                 ollama_base_url="http://localhost:11434",
                 ollama_model="llama3.1:8b")
        mock_ollama.assert_called_once_with(
            "sys", "user", 6000,
            "http://localhost:11434", "llama3.1:8b",
            response_schema=None,
        )

    @patch("services.llm_provider._call_ollama")
    def test_ollama_forwards_response_schema(self, mock_ollama):
        mock_ollama.return_value = "[]"
        schema = {"type": "array"}
        call_llm("sys", "user", provider="ollama", response_schema=schema)
        assert mock_ollama.call_args.kwargs["response_schema"] is schema

    @patch("services.llm_provider._call_claude")
    def test_schema_not_forwarded_to_claude(self, mock_claude):
        """Claude follows the prompt's format instructions; it takes no schema."""
        mock_claude.return_value = "[]"
        call_llm("sys", "user", anthropic_api_key="sk-ant-test",
                 response_schema={"type": "array"})
        assert "response_schema" not in mock_claude.call_args.kwargs


class TestCallClaude:
    """Tests for _call_claude."""

    def test_raises_without_api_key(self):
        with pytest.raises(LLMError, match="API key"):
            call_llm("sys", "user", provider="claude-sonnet", anthropic_api_key="")


class TestCallOllama:
    """Tests for _call_ollama."""

    @patch("services.llm_provider.httpx.post")
    def test_successful_call(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "test response"}}],
            "usage": {"total_tokens": 100},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_resp

        result = call_llm("sys", "user", provider="ollama")
        assert result == "test response"

    @patch("services.llm_provider.httpx.post")
    def test_connect_error(self, mock_post):
        import httpx
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(LLMError, match="Cannot connect to Ollama"):
            call_llm("sys", "user", provider="ollama")

    @patch("services.llm_provider.httpx.post")
    def test_timeout_error(self, mock_post):
        import httpx
        mock_post.side_effect = httpx.ReadTimeout("timed out")

        with pytest.raises(LLMError, match="timed out"):
            call_llm("sys", "user", provider="ollama")


class TestParseLlmJson:
    """Tests for parse_llm_json."""

    def test_clean_json(self):
        assert parse_llm_json('[{"a": 1}]') == [{"a": 1}]

    def test_markdown_fenced(self):
        text = '```json\n[{"a": 1}]\n```'
        assert parse_llm_json(text) == [{"a": 1}]

    def test_surrounding_text(self):
        text = 'Here are the results:\n[{"a": 1}]\nHope this helps!'
        assert parse_llm_json(text) == [{"a": 1}]

    def test_trailing_comma(self):
        text = '[{"a": 1},{"b": 2},]'
        assert parse_llm_json(text) == [{"a": 1}, {"b": 2}]

    def test_trailing_comma_in_object(self):
        text = '[{"a": 1, "b": 2,}]'
        assert parse_llm_json(text) == [{"a": 1, "b": 2}]

    def test_truncated_array(self):
        text = '[{"a": 1}, {"b": 2}, {"c":'
        result = parse_llm_json(text)
        assert result == [{"a": 1}, {"b": 2}]

    def test_truncated_with_bracket_inside_string_value(self):
        """Song titles containing [] must not break truncation recovery.

        The old rfind("]") scan landed inside the string value and produced
        an unparseable fragment, dropping the whole batch.
        """
        text = ('[{"artist": "New Order", "title": "Blue Monday [12\\" Mix]"}, '
                '{"artist": "A Guy Called Gerald", "title": "Voodoo Ray"}, '
                '{"artist": "808 State", "title": "Paci')
        result = parse_llm_json(text)
        assert len(result) == 2
        assert result[0]["title"] == 'Blue Monday [12" Mix]'
        assert result[1]["artist"] == "A Guy Called Gerald"

    def test_nested_arrays_and_objects_survive(self):
        text = ('[{"artist": "Neu!", "match_attributes": ["a", "b"], '
                '"similar_to": [{"artist": "Can", "why": "motorik"}]}, '
                '{"artist": "Cluster", "similar_to": [{"artist": "Har')
        result = parse_llm_json(text)
        assert len(result) == 1
        assert result[0]["similar_to"][0]["artist"] == "Can"

    def test_trailing_commentary_after_array(self):
        text = '[{"a": 1}]\n\nHope that helps! Let me know if you want more.'
        assert parse_llm_json(text) == [{"a": 1}]

    def test_empty_on_garbage(self):
        assert parse_llm_json("not json at all") == []

    def test_empty_string(self):
        assert parse_llm_json("") == []
