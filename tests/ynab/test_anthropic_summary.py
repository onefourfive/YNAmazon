"""Tests for AI memo summarization using the Anthropic provider."""

from unittest.mock import Mock, patch

import anthropic
import pytest
from pydantic import SecretStr

from ynamazon.exceptions import (
    AnthropicEmptyResponseError,
    InvalidAnthropicAPIKey,
    MissingAnthropicAPIKey,
)
from ynamazon.settings import Settings, settings
from ynamazon.ynab_memo import generate_ai_summary


@pytest.fixture
def anthropic_settings():
    """Select the Anthropic provider for the duration of a test."""
    original_provider = settings.ai_provider
    original_key = settings.anthropic_api_key
    original_markdown = settings.ynab_use_markdown

    settings.ai_provider = "anthropic"
    settings.anthropic_api_key = SecretStr("test_key")
    settings.ynab_use_markdown = False

    yield settings

    settings.ai_provider = original_provider
    settings.anthropic_api_key = original_key
    settings.ynab_use_markdown = original_markdown


def _response(*blocks: Mock, stop_reason: str = "end_turn") -> Mock:
    response = Mock()
    response.content = list(blocks)
    response.stop_reason = stop_reason
    return response


def _block(block_type: str, text: str = "") -> Mock:
    block = Mock()
    block.type = block_type
    block.text = text
    return block


def test_anthropic_summary_returns_text(anthropic_settings):
    """The memo comes from the text blocks; thinking blocks are ignored."""
    with patch("ynamazon.ynab_memo.Anthropic") as mock_anthropic, \
         patch("ynamazon.ynab_memo.OpenAI") as mock_openai:
        mock_client = mock_anthropic.return_value
        mock_client.beta.messages.create.return_value = _response(
            _block("thinking"), _block("text", "Bag of dog treats")
        )

        result = generate_ai_summary(["Dog treats, 2 lb bag"], "https://amazon.com/order/123")

    assert result == "Bag of dog treats"
    mock_anthropic.assert_called_once_with(api_key="test_key")
    mock_openai.assert_not_called()

    kwargs = mock_client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == settings.anthropic_model
    assert kwargs["messages"][0]["role"] == "user"
    assert "Dog treats, 2 lb bag" in kwargs["messages"][0]["content"]


def test_anthropic_summary_truncates_and_adds_partial_order_note(anthropic_settings):
    """Shared post-processing applies to the Anthropic provider too."""
    with patch("ynamazon.ynab_memo.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.return_value = _response(_block("text", "x" * 600))

        result = generate_ai_summary(
            ["Item 1"], "https://amazon.com/order/123", order_total="50.00", transaction_amount="20.00"
        )

    assert result is not None
    assert result.startswith("x" * 500 + " ")
    assert result.endswith("The order total is $50.00-")


def test_anthropic_summary_error_handling(anthropic_settings):
    """Authentication errors raise; other API errors and refusals return None."""
    with patch("ynamazon.ynab_memo.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.side_effect = anthropic.AuthenticationError(
            message="Invalid API key",
            response=Mock(status_code=401, headers={}),
            body={"error": {"message": "Invalid API key"}},
        )
        with pytest.raises(InvalidAnthropicAPIKey):
            generate_ai_summary(["Item 1"], "https://amazon.com/order/123")

    with patch("ynamazon.ynab_memo.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.side_effect = anthropic.APIConnectionError(request=Mock())
        assert generate_ai_summary(["Item 1"], "https://amazon.com/order/123") is None

    with patch("ynamazon.ynab_memo.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.return_value = _response(stop_reason="refusal")
        assert generate_ai_summary(["Item 1"], "https://amazon.com/order/123") is None

    with patch("ynamazon.ynab_memo.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.return_value = _response(_block("thinking"))
        with pytest.raises(AnthropicEmptyResponseError):
            generate_ai_summary(["Item 1"], "https://amazon.com/order/123")


def test_anthropic_summary_missing_key(anthropic_settings):
    """A missing key raises before any request is made."""
    anthropic_settings.anthropic_api_key = None
    with patch("ynamazon.ynab_memo.Anthropic") as mock_anthropic:
        with pytest.raises(MissingAnthropicAPIKey):
            generate_ai_summary(["Item 1"], "https://amazon.com/order/123")
    mock_anthropic.assert_not_called()


def test_settings_require_key_for_selected_provider(monkeypatch):
    """Only the selected provider's key is required when AI summarization is enabled."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    required = {
        "ynab_api_key": "x",
        "ynab_budget_id": "x",
        "amazon_user": "test@example.com",
        "amazon_password": "x",
        "use_ai_summarization": True,
        "ai_provider": "anthropic",
    }

    with pytest.raises(MissingAnthropicAPIKey):
        Settings(_env_file=None, **required)

    configured = Settings(_env_file=None, anthropic_api_key="sk-ant-test", **required)
    assert configured.ai_provider == "anthropic"
    assert configured.openai_api_key is None
