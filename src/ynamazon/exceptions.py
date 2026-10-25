class YnabSetupError(Exception):
    """Custom exception for YNAB setup errors."""

    pass

class MissingAIAPIKey(Exception):
    """Raised when the API key for the configured AI provider is required but not found."""
    pass

class InvalidAIAPIKey(Exception):
    """Raised when the API key for the configured AI provider is invalid or authentication fails."""
    pass

class AIEmptyResponseError(Exception):
    """Raised when the configured AI provider returns an empty or invalid response."""
    pass

class MissingOpenAIAPIKey(MissingAIAPIKey):
    """Raised when OpenAI API key is required but not found."""
    pass

class InvalidOpenAIAPIKey(InvalidAIAPIKey):
    """Raised when OpenAI API key is invalid or authentication fails."""
    pass

class OpenAIEmptyResponseError(AIEmptyResponseError):
    """Raised when OpenAI returns an empty or invalid response."""
    pass

class MissingAnthropicAPIKey(MissingAIAPIKey):
    """Raised when Anthropic API key is required but not found."""
    pass

class InvalidAnthropicAPIKey(InvalidAIAPIKey):
    """Raised when Anthropic API key is invalid or authentication fails."""
    pass

class AnthropicEmptyResponseError(AIEmptyResponseError):
    """Raised when Anthropic returns an empty or invalid response."""
    pass
