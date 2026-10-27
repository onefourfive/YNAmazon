"""Tests for retrieving Amazon orders and transactions with AmazonTransactionRetriever.

Amazon is never contacted: AmazonOrders, AmazonTransactions, and the session are mocked. Tests that call
get_amazon_transactions() set force_refresh_amazon, so the on-disk cache is never read or written.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from amazonorders.entity.item import Item
from amazonorders.entity.order import Order
from amazonorders.entity.transaction import Transaction
from amazonorders.session import AmazonSession
from faker import Faker

from ynamazon.amazon_transactions import (
    HISTORY_LIMIT_THRESHOLD,
    AmazonConfig,
    AmazonTransactionRetriever,
)
from ynamazon.exceptions import AmazonLoginError

fake = Faker()


def _item(title: str | None = None) -> Item:
    item = MagicMock(spec=Item)
    item.title = title or fake.sentence()
    item.link = fake.url()
    item.price = fake.random_number(digits=2) + 0.99
    item.quantity = fake.random_int(min=1, max=5)
    return item


def _order(
    order_number: str, placed: date, grand_total: float = 100.00, item_count: int = 1
) -> Order:
    order = MagicMock(spec=Order)
    order.order_number = order_number
    order.order_placed_date = placed
    order.grand_total = grand_total
    order.order_details_link = (
        f"https://www.amazon.com/gp/your-account/order-details?orderID={order_number}"
    )
    order.items = [_item() for _ in range(item_count)]
    return order


def _transaction(order_number: str, completed: date, grand_total: float = -100.00) -> Transaction:
    transaction = MagicMock(spec=Transaction)
    transaction.order_number = order_number
    transaction.completed_date = completed
    transaction.grand_total = grand_total
    return transaction


@pytest.fixture
def session():
    """A session that logs in successfully."""
    amazon_session = MagicMock(spec=AmazonSession)
    amazon_session.is_authenticated = True
    with patch.object(AmazonConfig, "amazon_session", return_value=amazon_session):
        yield amazon_session


@pytest.fixture
def amazon_orders(session):
    with patch("ynamazon.amazon_transactions.AmazonOrders") as mock:
        yield mock


@pytest.fixture
def amazon_transactions(session):
    with patch("ynamazon.amazon_transactions.AmazonTransactions") as mock:
        yield mock


def _retriever(**kwargs) -> AmazonTransactionRetriever:
    return AmazonTransactionRetriever(
        amazon_config=AmazonConfig(username="test@example.com", password="x"), **kwargs
    )


# Years


@pytest.mark.parametrize(
    ("years", "expected"), [(["21", "2022"], ["2021", "2022"]), (["2026"], ["2026"])]
)
def test_normalized_years(years, expected):
    assert AmazonTransactionRetriever._normalized_years(years) == expected


def test_normalized_years_defaults_to_current_year():
    with patch("ynamazon.amazon_transactions.date") as mock_date:
        mock_date.today.return_value.year = 2023
        assert AmazonTransactionRetriever._normalized_years(None) == ["2023"]


@pytest.mark.parametrize("year", ["2", "202", "20211"])
def test_normalized_years_rejects_other_lengths(year):
    with pytest.raises(ValueError, match="2 or 4 digits"):
        AmazonTransactionRetriever._normalized_years([year])


# Orders


def test_orders_are_fetched_for_each_year_and_sorted(amazon_orders, session):
    newer, older = _order("456", date(2023, 3, 1)), _order("123", date(2022, 6, 1))
    by_year = {"2022": [older], "2023": [newer]}
    amazon_orders.return_value.get_orders.side_effect = lambda year: by_year[year]

    orders = _retriever(order_years=["2023", "22"])._amazon_orders()

    assert [order.order_number for order in orders] == ["123", "456"]
    amazon_orders.assert_called_once_with(session)
    assert [call.kwargs for call in amazon_orders.return_value.get_orders.call_args_list] == [
        {"year": "2023"},
        {"year": "2022"},
    ]


def test_orders_default_to_current_year(amazon_orders):
    amazon_orders.return_value.get_orders.return_value = []
    with patch("ynamazon.amazon_transactions.date") as mock_date:
        mock_date.today.return_value.year = 2023
        _retriever()._amazon_orders()

    amazon_orders.return_value.get_orders.assert_called_once_with(year="2023")


def test_orders_keep_all_items(amazon_orders):
    amazon_orders.return_value.get_orders.return_value = [
        _order("567", date(2023, 1, 1), item_count=5)
    ]

    orders = _retriever(order_years=["2023"])._amazon_orders()

    assert len(orders) == 1
    assert len(orders[0].items) == 5


def test_orders_are_fetched_once(amazon_orders):
    amazon_orders.return_value.get_orders.return_value = []
    retriever = _retriever(order_years=["2023"])
    retriever._amazon_orders()
    retriever._amazon_orders()

    amazon_orders.return_value.get_orders.assert_called_once()


# Transactions


TODAY = date(2026, 10, 24)


@pytest.fixture
def today():
    """Fix today's date in the retriever; date() still builds dates."""
    with patch("ynamazon.amazon_transactions.date", wraps=date) as mock_date:
        mock_date.today.return_value = TODAY
        yield


def test_transactions_cover_the_window_and_are_sorted(amazon_transactions, session, today):
    # 2026-09-01 is before the 45-day window (from 2026-09-09), so the history covers the window
    before_window = _transaction("0", date(2026, 9, 1))
    later, earlier = _transaction("2", date(2026, 10, 5)), _transaction("1", date(2026, 10, 1))
    amazon_transactions.return_value.get_transactions.return_value = [later, earlier, before_window]

    transactions = _retriever(transaction_days=45)._amazon_transactions()

    assert [t.order_number for t in transactions] == ["1", "2"]
    amazon_transactions.assert_called_once_with(amazon_session=session)
    # Asks for more than the window, to see whether the history reaches past its start
    amazon_transactions.return_value.get_transactions.assert_called_once_with(days=45 + 30)


def _cut_off_history(first_day: date, count: int = HISTORY_LIMIT_THRESHOLD) -> list[Transaction]:
    """A history Amazon cut off, starting on `first_day`, with one transaction per day after it."""
    return [
        _transaction(f"history-{i}", first_day + timedelta(days=i), grand_total=-1.0 - i)
        for i in range(count)
    ]


@pytest.fixture
def lookups(amazon_orders, amazon_transactions):
    """Amazon with a history and per-order transactions; returns the order numbers looked up one by one."""

    def _setup(history, orders, by_order, orders_by_year=None):
        amazon_orders.return_value.get_orders.side_effect = lambda year: (
            orders_by_year[year] if orders_by_year else orders
        )

        def get_transactions(days=None, order_id=None):
            if order_id is None:
                return list(history)
            result = by_order[order_id]
            if isinstance(result, Exception):
                raise result
            return result

        amazon_transactions.return_value.get_transactions.side_effect = get_transactions

    def _looked_up():
        return [
            call.kwargs["order_id"]
            for call in amazon_transactions.return_value.get_transactions.call_args_list
            if "order_id" in call.kwargs
        ]

    _setup.looked_up = _looked_up
    return _setup


def test_orders_beyond_the_history_are_looked_up_one_by_one(lookups, today):
    """Amazon's history stops after about 100 transactions; older ones are found by order number."""
    # Window starts 2026-04-01; the history only reaches back to 2026-06-02
    history = _cut_off_history(date(2026, 6, 2))
    lookups(
        history,
        orders=[
            _order("too-old", date(2026, 2, 1)),  # before the window and its lead
            _order("march", date(2026, 3, 31)),  # placed before the window, charged in it
            _order("may", date(2026, 5, 10)),
            _order("june", date(2026, 6, 3)),  # placed after the history starts: already covered
        ],
        by_order={
            "march": [_transaction("march", date(2026, 4, 1))],
            "may": [_transaction("may", date(2026, 5, 12))],
        },
    )

    transactions = _retriever(transaction_days=206)._amazon_transactions()

    assert [(t.order_number, t.completed_date) for t in transactions][:3] == [
        ("march", date(2026, 4, 1)),
        ("may", date(2026, 5, 12)),
        ("history-0", date(2026, 6, 2)),
    ]
    assert len(transactions) == 2 + len(history)
    assert lookups.looked_up() == ["march", "may"]


def test_history_first_day_is_completed_by_lookups_without_duplicates(lookups, today):
    """The history can stop partway through its first day; lookups fill in the rest of that day."""
    history = _cut_off_history(date(2026, 6, 2))
    in_history = _transaction("split", date(2026, 6, 2), grand_total=-20.0)
    history.append(in_history)
    lookups(
        history,
        orders=[_order("split", date(2026, 5, 30)), _order("cut-off", date(2026, 6, 1))],
        by_order={
            # A multi-shipment order: one charge before the history, one in it
            "split": [
                _transaction("split", date(2026, 5, 30), grand_total=-10.0),
                _transaction("split", date(2026, 6, 2), grand_total=-20.0),
            ],
            # Charged on the history's first day, but cut off from it
            "cut-off": [_transaction("cut-off", date(2026, 6, 2), grand_total=-5.0)],
        },
    )

    transactions = _retriever(transaction_days=206)._amazon_transactions()

    found = [(t.order_number, t.completed_date, t.grand_total) for t in transactions]
    assert ("split", date(2026, 5, 30), -10.0) in found
    assert ("cut-off", date(2026, 6, 2), -5.0) in found
    assert found.count(("split", date(2026, 6, 2), -20.0)) == 1
    assert len(found) == len(history) + 2


def test_identical_charges_are_both_kept(lookups, today):
    """Two identical charges on the history's first day, one cut off, both survive."""
    history = _cut_off_history(date(2026, 6, 2))
    history.append(_transaction("twice", date(2026, 6, 2), grand_total=-7.0))
    lookups(
        history,
        orders=[_order("twice", date(2026, 6, 1))],
        by_order={"twice": [_transaction("twice", date(2026, 6, 2), grand_total=-7.0)] * 2},
    )

    transactions = _retriever(transaction_days=206)._amazon_transactions()

    assert sum(t.order_number == "twice" for t in transactions) == 2


def test_short_history_is_complete(lookups, today):
    """A history well under Amazon's limit isn't cut off, even if nothing in it is older than the window."""
    lookups(
        [_transaction("recent", date(2026, 10, 20))],
        orders=[_order("recent", date(2026, 10, 18)), _order("older", date(2026, 9, 20))],
        by_order={},
    )

    transactions = _retriever(transaction_days=31)._amazon_transactions()

    assert [t.order_number for t in transactions] == ["recent"]
    assert lookups.looked_up() == []


def test_empty_history_looks_nothing_up(lookups, today):
    lookups([], orders=[_order("recent", date(2026, 10, 18))], by_order={})
    assert _retriever(transaction_days=31)._amazon_transactions() == []
    assert lookups.looked_up() == []


def test_lookups_stop_at_latest_date(lookups, today):
    """With a past date range, orders placed after the range (plus its buffer) aren't looked up."""
    lookups(
        _cut_off_history(date(2026, 6, 2)),
        orders=[_order("in-range", date(2026, 4, 5)), _order("after-range", date(2026, 5, 15))],
        by_order={"in-range": [_transaction("in-range", date(2026, 4, 6))]},
    )

    _retriever(transaction_days=206, latest_date=date(2026, 5, 7))._amazon_transactions()

    assert lookups.looked_up() == ["in-range"]


def test_lookups_reach_orders_from_the_previous_year(lookups, amazon_orders):
    """Early in the year, orders placed in December can be charged in the window."""
    december = _order("december", date(2025, 12, 28), grand_total=12.0)
    lookups(
        _cut_off_history(date(2026, 1, 20)),
        orders=None,
        by_order={"december": [_transaction("december", date(2026, 1, 3), grand_total=-12.0)]},
        orders_by_year={"2025": [december], "2026": []},
    )

    with patch("ynamazon.amazon_transactions.date", wraps=date) as mock_date:
        mock_date.today.return_value = date(2026, 2, 1)
        results = _retriever(
            order_years=["2026"], transaction_days=31, force_refresh_amazon=True
        ).get_amazon_transactions()

    years = [call.kwargs["year"] for call in amazon_orders.return_value.get_orders.call_args_list]
    assert sorted(years) == ["2025", "2026"]
    # Joined to its order even though 2025 isn't in order_years
    assert [r.order_number for r in results] == ["december"]
    assert results[0].order_total == Decimal("12.0")


def test_failed_lookup_skips_only_that_order(lookups, today):
    from amazonorders.exception import AmazonOrdersError

    lookups(
        _cut_off_history(date(2026, 6, 2)),
        orders=[_order("broken", date(2026, 4, 5)), _order("fine", date(2026, 4, 10))],
        by_order={
            "broken": AmazonOrdersError("page didn't load"),
            "fine": [_transaction("fine", date(2026, 4, 11))],
        },
    )

    transactions = _retriever(transaction_days=206)._amazon_transactions()

    assert lookups.looked_up() == ["broken", "fine"]
    assert transactions[0].order_number == "fine"


def test_amazon_transactions_are_joined_to_their_orders(amazon_orders, amazon_transactions):
    order = _order("111", date(2026, 9, 28), grand_total=42.50, item_count=2)
    amazon_orders.return_value.get_orders.return_value = [order]
    amazon_transactions.return_value.get_transactions.return_value = [
        _transaction("111", date(2026, 10, 1), grand_total=-42.50),
        _transaction("not-retrieved", date(2026, 10, 2)),  # order outside the retrieved years
    ]

    results = _retriever(order_years=["2026"], force_refresh_amazon=True).get_amazon_transactions()

    assert len(results) == 1
    result = results[0]
    assert result.order_number == "111"
    assert result.completed_date == date(2026, 10, 1)
    assert result.transaction_total == Decimal("42.50")  # inverted from Amazon's negative charge
    assert result.order_total == Decimal("42.50")
    assert str(result.order_link) == order.order_details_link
    assert result.items == order.items


@pytest.mark.parametrize(("force_refresh", "use_cache"), [(True, False), (False, True)])
def test_force_refresh_bypasses_cache(force_refresh, use_cache):
    retriever = _retriever(
        order_years=["2026"], transaction_days=10, force_refresh_amazon=force_refresh
    )
    with patch.object(
        AmazonTransactionRetriever, "_get_amazon_transactions", return_value=[]
    ) as cached:
        retriever.get_amazon_transactions()

    kwargs = cached.call_args.kwargs
    assert kwargs["use_cache"] is use_cache
    assert kwargs["order_years"] == ["2026"]
    assert kwargs["transaction_days"] == 10


# Order details


def test_get_order_items_reads_order_details(amazon_orders, session):
    items = [_item("Rice"), _item("Beans")]
    amazon_orders.return_value.get_order.return_value.items = items

    assert _retriever().get_order_items("111-222") == items
    amazon_orders.assert_called_once_with(session)
    amazon_orders.return_value.get_order.assert_called_once_with("111-222")


# Session


def test_session_logs_in_once(session):
    retriever = _retriever()
    assert retriever._session() is session
    assert retriever._session() is session
    session.login.assert_called_once()


def test_unauthenticated_session_raises():
    amazon_session = MagicMock(spec=AmazonSession)
    amazon_session.is_authenticated = False
    with (
        patch.object(AmazonConfig, "amazon_session", return_value=amazon_session),
        pytest.raises(AmazonLoginError, match="isn't authenticated"),
    ):
        _retriever()._session()
