"""Client construction for the supported AI providers."""

from anthropic import Anthropic
from openai import OpenAI

from ynamazon.settings import settings

from .exceptions import MissingAnthropicAPIKey, MissingOpenAIAPIKey


def get_openai_client() -> OpenAI:
    """Returns an OpenAI client using the configured API key.

    Raises:
        MissingOpenAIAPIKey: If OpenAI API key is not found
    """
    if settings.openai_api_key is None or not settings.openai_api_key.get_secret_value():
        raise MissingOpenAIAPIKey("OpenAI API key not found")

    return OpenAI(api_key=settings.openai_api_key.get_secret_value())


def get_anthropic_client() -> Anthropic:
    """Returns an Anthropic client using the configured API key.

    Raises:
        MissingAnthropicAPIKey: If Anthropic API key is not found
    """
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise MissingAnthropicAPIKey("Anthropic API key not found")

    return Anthropic(api_key=settings.anthropic_api_key.get_secret_value())


def ai_api_key_configured() -> bool:
    """Returns True if the configured AI provider has an API key."""
    key = (
        settings.anthropic_api_key
        if settings.ai_provider == "anthropic"
        else settings.openai_api_key
    )
    return key is not None and bool(key.get_secret_value())
