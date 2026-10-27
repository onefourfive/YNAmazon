"""Tests for matching YNAB transactions to Amazon transactions."""

from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from ynamazon.amazon_transactions import find_amazon_transaction
from ynamazon.main import process_transactions
from ynamazon.ynab_transactions import TempYnabTransaction, TempYnabTransactions


def _amazon_tran(total: str, completed_date: date = date(2026, 10, 3)) -> Mock:
    amazon_tran = Mock()
    amazon_tran.transaction_total = Decimal(total)
    amazon_tran.order_total = Decimal(total)
    amazon_tran.completed_date = completed_date
    amazon_tran.items = []
    return amazon_tran


def _ynab_tran(
    txn_id: str, amount: int = -30000, var_date: date = date(2026, 10, 3)
) -> TempYnabTransaction:
    return TempYnabTransaction.model_validate(
        {
            "id": txn_id,
            "date": var_date.isoformat(),
            "amount": amount,
            "cleared": "uncleared",
            "approved": False,
            "account_id": "acct",
            "account_name": "Card",
            "deleted": False,
            "type": "transaction",
            "payee_id": "payee-needs-memo",
            "category_id": None,
            "subtransactions": [],
        }
    )


# find_amazon_transaction


def test_find_matches_first_transaction():
    first, second = _amazon_tran("30.00"), _amazon_tran("12.00")
    assert find_amazon_transaction([first, second], Decimal("-30.00")) is first
    assert find_amazon_transaction([first, second], Decimal("-12.00")) is second
    assert find_amazon_transaction([first, second], Decimal("-99.00")) is None


def test_find_prefers_same_date_among_equal_amounts():
    earlier = _amazon_tran("30.00", date(2026, 10, 1))
    same_day = _amazon_tran("30.00", date(2026, 10, 3))
    assert (
        find_amazon_transaction([earlier, same_day], Decimal("-30.00"), on_date=date(2026, 10, 3))
        is same_day
    )


def test_find_falls_back_to_first_amount_match_when_no_date_matches():
    first, second = (
        _amazon_tran("30.00", date(2026, 10, 1)),
        _amazon_tran("30.00", date(2026, 10, 2)),
    )
    assert (
        find_amazon_transaction([first, second], Decimal("-30.00"), on_date=date(2026, 10, 9))
        is first
    )


# process_transactions


@pytest.fixture
def run():
    """Run process_transactions with YNAB and Amazon mocked; the real matcher is used."""

    def _run(ynab_trans: list[TempYnabTransaction], amazon_trans: list[Mock], answers: list[bool]):
        with (
            patch("ynamazon.main.get_ynab_transactions") as get_ynab,
            patch("ynamazon.main.AmazonTransactionRetriever") as retriever,
            patch(
                "ynamazon.main.build_memo",
                side_effect=lambda console, a_tran: f"memo for {id(a_tran)}",
            ) as build_memo,
            patch("ynamazon.main.confirm", side_effect=answers) as confirm,
            patch("ynamazon.main.update_ynab_transaction") as update,
            patch("ynamazon.main.settings.use_ai_categorization", False),
        ):
            get_ynab.return_value = (
                TempYnabTransactions(root=ynab_trans),
                Mock(id="payee-processed"),
            )
            retriever.return_value.get_amazon_transactions.return_value = amazon_trans

            process_transactions(ynab_config=Mock(), budget_id="budget")

        return Mock(build_memo=build_memo, confirm=confirm, update=update)

    return _run


def test_first_amazon_transaction_can_be_matched(run):
    """A match on the first Amazon transaction is a match, not "no match" (#5)."""
    first = _amazon_tran("30.00")
    result = run([_ynab_tran("t1")], [first, _amazon_tran("12.00")], answers=[True])

    assert result.build_memo.call_args.args[1] is first
    result.update.assert_called_once()
    assert result.update.call_args.kwargs["memo"] == f"memo for {id(first)}"
    assert result.update.call_args.kwargs["payee_id"] == "payee-processed"


def test_matched_amazon_transaction_is_used_only_once(run):
    """Two YNAB transactions with the same amount match two different Amazon transactions."""
    first, second = _amazon_tran("30.00"), _amazon_tran("30.00")
    result = run([_ynab_tran("t1"), _ynab_tran("t2")], [first, second], answers=[True, True])

    assert [call.args[1] for call in result.build_memo.call_args_list] == [first, second]
    assert [call.kwargs["memo"] for call in result.update.call_args_list] == [
        f"memo for {id(first)}",
        f"memo for {id(second)}",
    ]


def test_amazon_transaction_stays_available_when_nothing_is_updated(run):
    """Declining the update leaves the Amazon transaction for a later YNAB transaction."""
    only = _amazon_tran("30.00")
    result = run([_ynab_tran("t1"), _ynab_tran("t2")], [only], answers=[False, True])

    assert [call.args[1] for call in result.build_memo.call_args_list] == [only, only]
    result.update.assert_called_once()
    assert result.update.call_args.kwargs["transaction"].id == "t2"


def test_declined_date_mismatch_skips_without_building_memo(run):
    """The memo (which may call an AI provider) is only built once the match is accepted."""
    result = run(
        [_ynab_tran("t1", var_date=date(2026, 10, 9))],
        [_amazon_tran("30.00", date(2026, 10, 3))],
        answers=[False],
    )

    result.build_memo.assert_not_called()
    result.update.assert_not_called()
    result.confirm.assert_called_once()  # only the "continue anyway?" prompt
