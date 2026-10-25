"""Tests for matching YNAB transactions to Amazon transactions."""

from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

from ynamazon.amazon_transactions import locate_amazon_transaction_by_amount
from ynamazon.main import process_transactions
from ynamazon.ynab_transactions import TempYnabTransaction


def _amazon_tran(total: str) -> Mock:
    amazon_tran = Mock()
    amazon_tran.transaction_total = Decimal(total)
    amazon_tran.order_total = Decimal(total)
    amazon_tran.completed_date = date(2026, 10, 3)
    amazon_tran.items = []
    return amazon_tran


def _ynab_tran(amount: int) -> TempYnabTransaction:
    return TempYnabTransaction.model_validate(
        {
            "id": "txn-1",
            "date": "2026-10-03",
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


def test_locate_returns_index_zero_for_first_transaction():
    amazon_trans = [_amazon_tran("30.00"), _amazon_tran("12.00")]
    assert locate_amazon_transaction_by_amount(amazon_trans, Decimal("-30.00")) == 0
    assert locate_amazon_transaction_by_amount(amazon_trans, Decimal("-12.00")) == 1
    assert locate_amazon_transaction_by_amount(amazon_trans, Decimal("-99.00")) is None


def test_first_amazon_transaction_can_be_matched():
    """A match at index 0 is a match, not "no match" (#5)."""
    with patch("ynamazon.main.get_ynab_transactions") as get_ynab, \
         patch("ynamazon.main.AmazonTransactionRetriever") as retriever, \
         patch("ynamazon.main.build_memo", return_value="memo") as build_memo, \
         patch("ynamazon.main.Confirm.ask", return_value=True), \
         patch("ynamazon.main.update_ynab_transaction") as update, \
         patch("ynamazon.main.settings.use_ai_categorization", False):
        get_ynab.return_value = ([_ynab_tran(-30000)], Mock(id="payee-processed"))
        retriever.return_value.get_amazon_transactions.return_value = [_amazon_tran("30.00"), _amazon_tran("12.00")]

        process_transactions(ynab_config=Mock(), budget_id="budget")

    build_memo.assert_called_once()
    update.assert_called_once()
    assert update.call_args.kwargs["memo"] == "memo"
