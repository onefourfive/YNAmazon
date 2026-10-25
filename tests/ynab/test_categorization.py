"""Tests for AI-suggested YNAB categorization."""

import json
from decimal import Decimal
from unittest.mock import MagicMock, Mock, patch

import anthropic
import pytest
from pydantic import SecretStr
from ynab.models.save_sub_transaction import SaveSubTransaction

from ynamazon.ai_categorization import (
    CategorizationResponse,
    ItemAssignment,
    allocate_amounts,
    suggest_categorization,
)
from ynamazon.settings import settings
from ynamazon.ynab_categories import get_budget_categories, is_uncategorized
from ynamazon.ynab_transactions import update_ynab_transaction


def _category(category_id: str, name: str, group: str = "Everyday", note: str | None = None) -> Mock:
    category = Mock()
    category.id = category_id
    category.name = name
    category.category_group_name = group
    category.note = note
    category.hidden = False
    category.deleted = False
    return category


def _item(title: str, price: float | None, quantity: int | None = 1) -> Mock:
    item = Mock()
    item.title = title
    item.price = price
    item.quantity = quantity
    return item


def _amazon_tran(items: list[Mock], transaction_total: str = "30.00", order_total: str = "30.00") -> Mock:
    amazon_tran = Mock()
    amazon_tran.items = items
    amazon_tran.transaction_total = Decimal(transaction_total)
    amazon_tran.order_total = Decimal(order_total)
    return amazon_tran


def _ynab_tran(amount: int = -30000, category_id: str | None = None, category_name: str | None = None) -> Mock:
    ynab_tran = Mock()
    ynab_tran.amount = amount
    ynab_tran.category_id = category_id
    ynab_tran.category_name = category_name
    return ynab_tran


CATEGORIES = [_category("cat-groceries", "Groceries"), _category("cat-pets", "Pets", note="Food and vet")]


def _response(*pairs: tuple[int, int], reason: str = "Matched items to categories.") -> CategorizationResponse:
    return CategorizationResponse(
        assignments=[ItemAssignment(item=item, category=category) for item, category in pairs],
        reason=reason,
    )


# allocate_amounts


def test_allocate_amounts_adds_up_exactly_with_awkward_rounding():
    amounts = allocate_amounts(-10000, {"a": Decimal(1), "b": Decimal(1), "c": Decimal(1)})
    assert sum(amounts.values()) == -10000
    assert sorted(amounts.values()) == [-3340, -3330, -3330]
    assert all(amount % 10 == 0 for amount in amounts.values())


def test_allocate_amounts_is_proportional_and_keeps_order():
    amounts = allocate_amounts(-33000, {"a": Decimal("20.00"), "b": Decimal("10.00")})
    assert list(amounts) == ["a", "b"]
    assert amounts == {"a": -22000, "b": -11000}


def test_allocate_amounts_single_category_and_inflows():
    assert allocate_amounts(-12340, {"a": Decimal(5)}) == {"a": -12340}
    assert allocate_amounts(5000, {"a": Decimal(1), "b": Decimal(1)}) == {"a": 2500, "b": 2500}


def test_allocate_amounts_puts_sub_cent_remainder_on_largest_share():
    amounts = allocate_amounts(-10005, {"a": Decimal(1), "b": Decimal(3)})
    assert sum(amounts.values()) == -10005
    assert amounts["b"] % 10 == -5 % 10


def test_allocate_amounts_rejects_empty_weights():
    with pytest.raises(ValueError):
        allocate_amounts(-1000, {})
    with pytest.raises(ValueError):
        allocate_amounts(-1000, {"a": Decimal(0)})


# suggest_categorization


@pytest.fixture
def anthropic_settings():
    """Select the Anthropic provider for the duration of a test."""
    original_provider = settings.ai_provider
    original_key = settings.anthropic_api_key
    settings.ai_provider = "anthropic"
    settings.anthropic_api_key = SecretStr("test_key")
    yield settings
    settings.ai_provider = original_provider
    settings.anthropic_api_key = original_key


@pytest.fixture
def openai_settings():
    """Select the OpenAI provider for the duration of a test."""
    original_provider = settings.ai_provider
    original_key = settings.openai_api_key
    settings.ai_provider = "openai"
    settings.openai_api_key = SecretStr("test_key")
    yield settings
    settings.ai_provider = original_provider
    settings.openai_api_key = original_key


def _suggest(response: CategorizationResponse | None, amazon_tran: Mock, ynab_tran: Mock | None = None):
    with patch("ynamazon.ai_categorization._anthropic_categorization", return_value=response):
        return suggest_categorization(amazon_tran, ynab_tran or _ynab_tran(), CATEGORIES)


def test_suggest_split_weights_by_price_and_quantity(anthropic_settings):
    items = [_item("Rice", 10.00), _item("Dog food", 5.00, quantity=2), _item("Beans", 5.00)]
    suggestion = _suggest(_response((1, 1), (2, 2), (3, 1)), _amazon_tran(items))

    assert suggestion is not None and suggestion.is_split
    assert [(s.category.id, s.amount, s.items) for s in suggestion.splits] == [
        ("cat-groceries", -18000, ["Rice", "Beans"]),  # $15 of $25
        ("cat-pets", -12000, ["Dog food"]),  # 2 x $5 = $10 of $25
    ]

    subtransactions = suggestion.update_kwargs()["subtransactions"]
    assert [(s.category_id, s.amount, s.memo) for s in subtransactions] == [
        ("cat-groceries", -18000, "Rice, Beans"),
        ("cat-pets", -12000, "Dog food"),
    ]


def test_suggest_single_category(anthropic_settings):
    items = [_item("Rice", 10.00), _item("Beans", 5.00)]
    suggestion = _suggest(_response((1, 1), (2, 1)), _amazon_tran(items))

    assert suggestion is not None and not suggestion.is_split
    assert suggestion.update_kwargs() == {"category_id": "cat-groceries"}
    assert suggestion.splits[0].amount == -30000


def test_partial_order_uses_category_with_largest_share(anthropic_settings):
    items = [_item("Rice", 10.00), _item("Dog food", 25.00)]
    suggestion = _suggest(_response((1, 1), (2, 2)), _amazon_tran(items, transaction_total="12.00", order_total="35.00"))

    assert suggestion is not None and not suggestion.is_split
    assert suggestion.update_kwargs() == {"category_id": "cat-pets"}


def test_missing_prices_use_category_with_most_items(anthropic_settings):
    items = [_item("Rice", None), _item("Dog food", 25.00), _item("Beans", 3.00)]
    suggestion = _suggest(_response((1, 1), (2, 2), (3, 1)), _amazon_tran(items))

    assert suggestion is not None and not suggestion.is_split
    assert suggestion.update_kwargs() == {"category_id": "cat-groceries"}


@pytest.mark.parametrize(
    "pairs",
    [
        ((1, 1), (2, 3)),  # category out of range
        ((1, 1), (1, 2)),  # item assigned twice, item 2 missing
        ((1, 1),),  # item 2 missing
        ((1, 1), (2, 1), (3, 1)),  # item out of range
    ],
)
def test_invalid_assignments_return_none(anthropic_settings, pairs):
    items = [_item("Rice", 10.00), _item("Dog food", 20.00)]
    assert _suggest(_response(*pairs), _amazon_tran(items)) is None


def test_failed_request_returns_none(anthropic_settings):
    assert _suggest(None, _amazon_tran([_item("Rice", 10.00)])) is None


# Provider calls


def _anthropic_message(text: str, stop_reason: str = "end_turn") -> Mock:
    thinking, block = Mock(), Mock()
    thinking.type = "thinking"
    block.type = "text"
    block.text = text
    message = Mock()
    message.content = [thinking, block]
    message.stop_reason = stop_reason
    return message


def test_anthropic_request(anthropic_settings):
    payload = json.dumps({"assignments": [{"item": 1, "category": 2}], "reason": "It's dog food."})
    with patch("ynamazon.ai_clients.Anthropic") as mock_anthropic:
        create = mock_anthropic.return_value.beta.messages.create
        create.return_value = _anthropic_message(payload)
        suggestion = suggest_categorization(_amazon_tran([_item("Dog food", 30.00)]), _ynab_tran(), CATEGORIES)

    assert suggestion is not None
    assert suggestion.update_kwargs() == {"category_id": "cat-pets"}
    assert suggestion.reason == "It's dog food."

    kwargs = create.call_args.kwargs
    assert kwargs["model"] == settings.anthropic_model
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    prompt = kwargs["messages"][0]["content"]
    assert "2. Everyday: Pets (note: Food and vet)" in prompt
    assert "1. Dog food ($30.00)" in prompt


def test_anthropic_refusal_error_and_bad_json(anthropic_settings):
    amazon_tran = _amazon_tran([_item("Dog food", 30.00)])

    with patch("ynamazon.ai_clients.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.return_value = _anthropic_message("", stop_reason="refusal")
        assert suggest_categorization(amazon_tran, _ynab_tran(), CATEGORIES) is None

    with patch("ynamazon.ai_clients.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.side_effect = anthropic.APIConnectionError(request=Mock())
        assert suggest_categorization(amazon_tran, _ynab_tran(), CATEGORIES) is None

    with patch("ynamazon.ai_clients.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.beta.messages.create.return_value = _anthropic_message("not json")
        assert suggest_categorization(amazon_tran, _ynab_tran(), CATEGORIES) is None


def test_openai_request(openai_settings):
    with patch("ynamazon.ai_clients.OpenAI") as mock_openai:
        parse = mock_openai.return_value.beta.chat.completions.parse
        parse.return_value.choices = [Mock()]
        parse.return_value.choices[0].message.parsed = _response((1, 1))
        suggestion = suggest_categorization(_amazon_tran([_item("Rice", 30.00)]), _ynab_tran(), CATEGORIES)

    assert suggestion is not None
    assert suggestion.update_kwargs() == {"category_id": "cat-groceries"}
    assert parse.call_args.kwargs["response_format"] is CategorizationResponse

    with patch("ynamazon.ai_clients.OpenAI") as mock_openai:
        parse = mock_openai.return_value.beta.chat.completions.parse
        parse.return_value.choices = [Mock()]
        parse.return_value.choices[0].message.parsed = None  # refusal
        assert suggest_categorization(_amazon_tran([_item("Rice", 30.00)]), _ynab_tran(), CATEGORIES) is None


# YNAB categories


def test_get_budget_categories_filters_hidden_deleted_and_internal():
    def group(name: str, categories: list[Mock], hidden: bool = False) -> Mock:
        g = Mock()
        g.name = name
        g.hidden = hidden
        g.deleted = False
        g.categories = categories
        return g

    hidden, deleted = _category("h", "Hidden"), _category("d", "Deleted")
    hidden.hidden = True
    deleted.deleted = True
    groceries = _category("g", "Groceries", group=None)
    response = Mock()
    response.data.category_groups = [
        group("Internal Master Category", [_category("u", "Uncategorized")]),
        group("Credit Card Payments", [_category("cc", "Visa")]),
        group("Old Stuff", [_category("o", "Old")], hidden=True),
        group("Everyday", [groceries, hidden, deleted]),
    ]

    with patch("ynamazon.ynab_categories.ApiClient"), patch("ynamazon.ynab_categories.CategoriesApi") as mock_api:
        mock_api.return_value.get_categories.return_value = response
        categories = get_budget_categories(configuration=Mock(), budget_id="budget")

    assert [c.id for c in categories] == ["g"]
    assert categories[0].category_group_name == "Everyday"


@pytest.mark.parametrize(
    ("category_id", "category_name", "expected"),
    [(None, None, True), ("u", "Uncategorized", True), ("g", "Groceries", False), (None, "Split", True), ("s", "Split", False)],
)
def test_is_uncategorized(category_id, category_name, expected):
    assert is_uncategorized(_ynab_tran(category_id=category_id, category_name=category_name)) is expected


# update_ynab_transaction


def _existing_transaction() -> Mock:
    transaction = Mock()
    transaction.id = "txn-1"
    transaction.to_dict.return_value = {
        "account_id": "acct",
        "date": "2026-10-01",
        "amount": -30000,
        "payee_id": "payee-needs-memo",
        "category_id": None,
        "memo": "original memo",
        "approved": False,
    }
    return transaction


def _sent_transaction(**kwargs):
    with patch("ynamazon.ynab_transactions.ApiClient", MagicMock()), \
         patch("ynamazon.ynab_transactions.TransactionsApi") as mock_api:
        update_ynab_transaction(_existing_transaction(), configuration=Mock(), budget_id="budget", **kwargs)
    return mock_api.return_value.update_transaction.call_args.kwargs["data"].transaction


def test_update_category_only_keeps_memo_and_payee():
    sent = _sent_transaction(category_id="cat-pets")
    assert sent.category_id == "cat-pets"
    assert sent.memo == "original memo"
    assert sent.payee_id == "payee-needs-memo"
    assert sent.subtransactions is None


def test_update_split():
    subtransactions = [
        SaveSubTransaction(amount=-20000, category_id="cat-groceries", memo="Rice"),
        SaveSubTransaction(amount=-10000, category_id="cat-pets", memo="Dog food"),
    ]
    sent = _sent_transaction(subtransactions=subtransactions)
    assert sent.category_id is None
    assert sent.subtransactions == subtransactions
    assert sent.amount == -30000


def test_update_memo_and_category_together():
    sent = _sent_transaction(memo="new memo", payee_id="payee-done", category_id="cat-groceries")
    assert (sent.memo, sent.payee_id, sent.category_id) == ("new memo", "payee-done", "cat-groceries")


# ask_for_categorization


def _ask(amazon_tran: Mock, suggestion, confirm: bool = True):
    from ynamazon.main import ask_for_categorization

    retriever = Mock()
    retriever.get_order_items.return_value = [_item("Rice", 10.00), _item("Dog food", 20.00)]
    with patch("ynamazon.main.suggest_categorization", return_value=suggestion) as mock_suggest, \
         patch("ynamazon.main.confirm", return_value=confirm):
        result = ask_for_categorization(Mock(), amazon_tran, _ynab_tran(), CATEGORIES, retriever)
    return result, retriever, mock_suggest


def _priced_copy(amazon_tran: Mock) -> Mock:
    copy = _amazon_tran([])
    amazon_tran.model_copy.side_effect = lambda update: copy.configure_mock(items=update["items"]) or copy
    return copy


def test_ask_fetches_prices_for_multi_item_whole_order():
    amazon_tran = _amazon_tran([_item("Rice", None), _item("Dog food", None)])
    copy = _priced_copy(amazon_tran)

    _, retriever, mock_suggest = _ask(amazon_tran, suggestion=None)

    retriever.get_order_items.assert_called_once_with(amazon_tran.order_number)
    assert mock_suggest.call_args.args[0] is copy
    assert [item.price for item in copy.items] == [10.00, 20.00]


@pytest.mark.parametrize(
    "amazon_tran",
    [
        _amazon_tran([_item("Rice", None)]),  # single item
        _amazon_tran([_item("Rice", None), _item("Dog food", None)], transaction_total="12.00"),  # partial order
        _amazon_tran([_item("Rice", 10.00), _item("Dog food", 20.00)]),  # prices already known
    ],
)
def test_ask_skips_price_fetch_when_split_not_possible_or_not_needed(amazon_tran):
    _, retriever, _ = _ask(amazon_tran, suggestion=None)
    retriever.get_order_items.assert_not_called()


def test_ask_returns_update_fields_only_when_accepted():
    from ynamazon.ai_categorization import CategorySuggestion, SuggestedSplit

    suggestion = CategorySuggestion(splits=[SuggestedSplit(CATEGORIES[0], -30000, ["Rice"])], reason="Food.")
    amazon_tran = _amazon_tran([_item("Rice", 30.00)])

    assert _ask(amazon_tran, suggestion, confirm=True)[0] == {"category_id": "cat-groceries"}
    assert _ask(amazon_tran, suggestion, confirm=False)[0] == {}
    assert _ask(amazon_tran, None)[0] == {}
