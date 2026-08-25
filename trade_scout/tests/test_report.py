"""Tests for the report module (Claude CLI seam)."""

import json
from unittest.mock import patch

import pytest

from trade_scout.report import (
    ClaudeAuthError,
    _parse_claude_json,
    check_claude_auth,
    strip_markdown_fences,
)


class TestStripMarkdownFences:
    def test_no_fences(self):
        assert strip_markdown_fences("hello world") == "hello world"

    def test_json_fences(self):
        text = '```json\n{"key": "value"}\n```'
        assert strip_markdown_fences(text) == '{"key": "value"}'

    def test_plain_fences(self):
        text = "```\nsome text\n```"
        assert strip_markdown_fences(text) == "some text"

    def test_strips_whitespace(self):
        assert strip_markdown_fences("  hello  ") == "hello"


class TestParseClaudeJson:
    def test_envelope_with_result_string(self):
        envelope = json.dumps({"result": "the verdict text"})
        assert _parse_claude_json(envelope) == "the verdict text"

    def test_envelope_with_result_dict(self):
        envelope = json.dumps({"result": {"text": "verdict here"}})
        assert _parse_claude_json(envelope) == "verdict here"

    def test_no_envelope(self):
        assert _parse_claude_json("plain text output") == "plain text output"

    def test_fenced_result(self):
        envelope = json.dumps({"result": "```json\n{\"a\":1}\n```"})
        assert _parse_claude_json(envelope) == '{"a":1}'

    def test_empty_string(self):
        assert _parse_claude_json("") == ""

    def test_non_json(self):
        assert _parse_claude_json("just some text") == "just some text"


class TestCheckClaudeAuth:
    @patch("trade_scout.report.subprocess.run")
    def test_auth_success(self, mock_run):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"result": "ok"}'
        mock_run.return_value.stderr = ""
        assert check_claude_auth() is True

    @patch("trade_scout.report.subprocess.run")
    def test_auth_failure(self, mock_run):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "auth failed"
        with pytest.raises(ClaudeAuthError, match="Refresh the token"):
            check_claude_auth()

    @patch("trade_scout.report.subprocess.run", side_effect=FileNotFoundError)
    def test_cli_not_found(self, _):
        with pytest.raises(ClaudeAuthError, match="not found"):
            check_claude_auth()
