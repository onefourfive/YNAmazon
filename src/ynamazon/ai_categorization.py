"""Functions for suggesting YNAB categories for Amazon purchases using AI."""

from collections import Counter
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING, Any

import anthropic
import openai
from loguru import logger
from pydantic import BaseModel, ValidationError
from ynab.models.save_sub_transaction import SaveSubTransaction

from ynamazon.ai_clients import get_anthropic_client, get_openai_client
from ynamazon.prompts import CATEGORIZATION_SYSTEM_PROMPT
from ynamazon.settings import settings

from .exceptions import InvalidAnthropicAPIKey, InvalidOpenAIAPIKey

if TYPE_CHECKING:
    from ynab.models.category import Category

    from ynamazon.amazon_transactions import AmazonTransactionWithOrderInfo
    from ynamazon.ynab_transactions import TempYnabTransaction

# YNAB's character limit for memos
SUBTRANSACTION_MEMO_LIMIT = 500


class ItemAssignment(BaseModel):
    """The category the AI assigned to one item."""

    item: int
    category: int


class CategorizationResponse(BaseModel):
    """Structured output requested from the AI provider."""

    assignments: list[ItemAssignment]
    reason: str


@dataclass
class SuggestedSplit:
    """One category in a suggestion, with its share of the transaction."""

    category: "Category"
    amount: int  # milliunits
    items: list[str]


@dataclass
class CategorySuggestion:
    """A suggested category, or split across categories, for a YNAB transaction."""

    splits: list[SuggestedSplit]
    reason: str

    @property
    def is_split(self) -> bool:
        """Returns True if the suggestion spans more than one category."""
        return len(self.splits) > 1

    def update_kwargs(self) -> dict[str, Any]:
        """Returns the keyword arguments for `update_ynab_transaction` that apply this suggestion."""
        if not self.is_split:
            return {"category_id": self.splits[0].category.id}

        return {
            "subtransactions": [
                SaveSubTransaction(
                    amount=split.amount,
                    category_id=split.category.id,
                    memo=_subtransaction_memo(split.items),
                )
                for split in self.splits
            ]
        }


def allocate_amounts(total: int, weights: dict[str, Decimal]) -> dict[str, int]:
    """Divides a transaction amount between categories in proportion to their weights.

    Amounts are allocated in whole cents (10 milliunits) using the largest remainder method, so the
    result always adds up exactly to `total`. Any sub-cent remainder goes to the largest share.

    Args:
        total: The transaction amount in milliunits (negative for outflows).
        weights: The relative weight of each category, keyed by category ID.

    Returns:
        The amount in milliunits for each category, in the same order as `weights`.

    Raises:
        ValueError: If there are no weights or they don't add up to a positive number.
    """
    weight_sum = sum(weights.values(), Decimal(0))
    if not weights or weight_sum <= 0:
        raise ValueError("Weights must add up to a positive number")

    sign = -1 if total < 0 else 1
    cents, leftover_milliunits = divmod(abs(total), 10)

    exact = {key: Decimal(cents) * weight / weight_sum for key, weight in weights.items()}
    shares = {
        key: int(value.to_integral_value(rounding=ROUND_FLOOR)) for key, value in exact.items()
    }

    # Hand out the remaining cents to the categories with the largest fractional parts
    remaining = cents - sum(shares.values())
    by_fraction = sorted(weights, key=lambda key: exact[key] - shares[key], reverse=True)
    for key in by_fraction[:remaining]:
        shares[key] += 1

    amounts = {key: sign * share * 10 for key, share in shares.items()}
    largest = max(weights, key=lambda key: weights[key])
    amounts[largest] += sign * leftover_milliunits

    return amounts


def suggest_categorization(
    amazon_tran: "AmazonTransactionWithOrderInfo",
    ynab_tran: "TempYnabTransaction",
    categories: list["Category"],
) -> CategorySuggestion | None:
    """Uses the configured AI provider to suggest a category, or split, for an Amazon transaction.

    The AI only assigns items to categories; amounts are calculated from item prices. A split is only
    suggested when the transaction covers the whole order and every item has a price. Otherwise the
    whole transaction goes to the category that covers the most of the order.

    Args:
        amazon_tran: The matched Amazon transaction and its order items.
        ynab_tran: The YNAB transaction to categorize.
        categories: The budget categories to choose from.

    Returns:
        The suggestion, or None if the AI request failed or returned an invalid assignment.

    Raises:
        InvalidAIAPIKey: If the configured provider's API key is invalid or authentication fails
    """
    items = amazon_tran.items
    if not items or not categories:
        return None

    prompt = _build_prompt(items, categories)
    if settings.ai_provider == "anthropic":
        response = _anthropic_categorization(prompt)
    else:
        response = _openai_categorization(prompt)

    if response is None:
        return None

    assignments = _validate_assignments(
        response, item_count=len(items), category_count=len(categories)
    )
    if assignments is None:
        return None

    # Group items by category, in the order categories first appear
    items_by_category: dict[int, list[int]] = {}
    for item_index in range(len(items)):
        items_by_category.setdefault(assignments[item_index], []).append(item_index)

    prices_known = all(item.price is not None for item in items)
    weights: dict[int, Decimal] = {}
    if prices_known:
        for category_index, item_indexes in items_by_category.items():
            weights[category_index] = sum(
                (Decimal(str(items[i].price)) * (items[i].quantity or 1) for i in item_indexes),
                Decimal(0),
            )

    whole_order = amazon_tran.transaction_total == amazon_tran.order_total
    can_split = (
        len(items_by_category) > 1 and whole_order and prices_known and sum(weights.values()) > 0
    )

    if can_split:
        amounts = allocate_amounts(ynab_tran.amount, {str(k): v for k, v in weights.items()})
        splits = [
            SuggestedSplit(
                category=categories[category_index],
                amount=amounts[str(category_index)],
                items=[items[i].title for i in item_indexes],
            )
            for category_index, item_indexes in items_by_category.items()
        ]
    else:
        if prices_known and sum(weights.values()) > 0:
            main_category = max(weights, key=lambda key: weights[key])
        else:
            main_category = max(items_by_category, key=lambda key: len(items_by_category[key]))
        splits = [
            SuggestedSplit(
                category=categories[main_category],
                amount=ynab_tran.amount,
                items=[item.title for item in items],
            )
        ]

    return CategorySuggestion(splits=splits, reason=response.reason.strip())


def _build_prompt(items: list[Any], categories: list["Category"]) -> str:
    """Builds the numbered category and item lists sent to the AI."""
    category_lines = []
    for number, category in enumerate(categories, start=1):
        line = f"{number}. {category.category_group_name}: {category.name}"
        if category.note:
            line += f" (note: {' '.join(category.note.split())})"
        category_lines.append(line)

    item_lines = []
    for number, item in enumerate(items, start=1):
        details = []
        if item.quantity and item.quantity > 1:
            details.append(f"quantity {item.quantity}")
        if item.price is not None:
            details.append(f"${item.price:.2f}")
        suffix = f" ({', '.join(details)})" if details else ""
        item_lines.append(f"{number}. {item.title}{suffix}")

    return "Categories:\n" + "\n".join(category_lines) + "\n\nItems:\n" + "\n".join(item_lines)


def _validate_assignments(
    response: CategorizationResponse, item_count: int, category_count: int
) -> dict[int, int] | None:
    """Checks that every item was assigned exactly once to a valid category.

    Returns:
        A map of 0-based item index to 0-based category index, or None if the assignments are invalid.
    """
    counts = Counter(assignment.item for assignment in response.assignments)
    if set(counts) != set(range(1, item_count + 1)) or any(count > 1 for count in counts.values()):
        logger.warning(
            f"AI categorization didn't assign each of the {item_count} items exactly once"
        )
        return None

    if any(not 1 <= assignment.category <= category_count for assignment in response.assignments):
        logger.warning("AI categorization referenced a category that doesn't exist")
        return None

    return {assignment.item - 1: assignment.category - 1 for assignment in response.assignments}


def _subtransaction_memo(item_titles: list[str]) -> str:
    """Joins item titles into a subtransaction memo that fits YNAB's limit."""
    memo = ", ".join(item_titles)
    if len(memo) > SUBTRANSACTION_MEMO_LIMIT:
        memo = memo[: SUBTRANSACTION_MEMO_LIMIT - 3] + "..."
    return memo


def _openai_categorization(prompt: str) -> CategorizationResponse | None:
    """Get item category assignments from OpenAI. Returns None on recoverable API errors."""
    client = get_openai_client()

    try:
        completion = client.beta.chat.completions.parse(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": CATEGORIZATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format=CategorizationResponse,
        )
    except openai.AuthenticationError as e:
        raise InvalidOpenAIAPIKey("Invalid OpenAI API key") from e
    except openai.APIError as e:
        logger.error(f"OpenAI API error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error using OpenAI API: {e}")
        return None

    if not completion.choices or completion.choices[0].message.parsed is None:
        logger.warning("OpenAI didn't return a categorization")
        return None

    return completion.choices[0].message.parsed


def _anthropic_categorization(prompt: str) -> CategorizationResponse | None:
    """Get item category assignments from Anthropic's Claude API. Returns None on recoverable API errors."""
    client = get_anthropic_client()

    try:
        # Matching items to categories doesn't need deep reasoning, so use low effort.
        # If a safety classifier declines the request, the server retries it on a fallback model.
        response = client.beta.messages.create(
            model=settings.anthropic_model,
            max_tokens=16000,
            system=CATEGORIZATION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "effort": "low",
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(CategorizationResponse),
                },
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise InvalidAnthropicAPIKey("Invalid Anthropic API key") from e
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error using Anthropic API: {e}")
        return None

    if response.stop_reason == "refusal":
        logger.warning("Anthropic declined to categorize this transaction")
        return None

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        return CategorizationResponse.model_validate_json(text)
    except ValidationError as e:
        logger.warning(f"Anthropic returned an invalid categorization: {e}")
        return None
