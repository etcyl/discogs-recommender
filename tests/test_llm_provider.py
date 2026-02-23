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
        )


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

    def test_empty_on_garbage(self):
        assert parse_llm_json("not json at all") == []

    def test_empty_string(self):
        assert parse_llm_json("") == []
