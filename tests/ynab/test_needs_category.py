"""Tests for --needs-category: categorizing Amazon transactions whose memo may already be processed."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from ynamazon.main import process_transactions
from ynamazon.ynab_transactions import TempYnabTransaction, get_uncategorized_ynab_transactions


def _payee(payee_id: str, name: str) -> Mock:
    payee = Mock()
    payee.id = payee_id
    payee.name = name
    return payee


NEEDS_MEMO = _payee("payee-needs-memo", "Amazon - Needs Memo")
PROCESSED = _payee("payee-processed", "Amazon")


def _ynab_tran(txn_id: str, payee_id: str, var_date: date, deleted: bool = False) -> TempYnabTransaction:
    return TempYnabTransaction.model_validate(
        {
            "id": txn_id,
            "date": var_date.isoformat(),
            "amount": -30000,
            "cleared": "uncleared",
            "approved": False,
            "account_id": "acct",
            "account_name": "Card",
            "deleted": deleted,
            "type": "transaction",
            "payee_id": payee_id,
            "category_id": None,
            "subtransactions": [],
        }
    )


def test_get_uncategorized_fetches_both_payees():
    since = date(2026, 10, 1)
    older = _ynab_tran("older", PROCESSED.id, date(2026, 10, 3))
    newer = _ynab_tran("newer", NEEDS_MEMO.id, date(2026, 10, 9))
    deleted = _ynab_tran("deleted", NEEDS_MEMO.id, date(2026, 10, 5), deleted=True)
    by_payee = {NEEDS_MEMO.id: [newer, deleted], PROCESSED.id: [older]}

    with patch("ynamazon.ynab_transactions.get_amazon_payees", return_value=(NEEDS_MEMO, PROCESSED)), \
         patch("ynamazon.ynab_transactions.TempYnabTransactions.get_by_payee") as get_by_payee:
        get_by_payee.side_effect = lambda payee, **kwargs: by_payee[payee.id]
        transactions, needs_memo, processed = get_uncategorized_ynab_transactions(
            configuration=Mock(), budget_id="budget", since_date=since
        )

    assert [t.id for t in transactions] == ["older", "newer"]
    assert (needs_memo, processed) == (NEEDS_MEMO, PROCESSED)
    assert [call.args[0] for call in get_by_payee.call_args_list] == [NEEDS_MEMO, PROCESSED]
    for call in get_by_payee.call_args_list:
        assert call.kwargs["type"] == "uncategorized"
        assert call.kwargs["since_date"] == since


@pytest.fixture
def flow():
    """Patch the YNAB, Amazon, and AI calls made by process_transactions in needs-category mode."""
    needs_memo_tran = _ynab_tran("needs-memo", NEEDS_MEMO.id, date(2026, 10, 3))
    processed_tran = _ynab_tran("processed", PROCESSED.id, date(2026, 10, 3))
    amazon_trans = []
    for _ in range(2):
        amazon_tran = Mock()
        amazon_tran.completed_date = date(2026, 10, 3)
        amazon_tran.transaction_total = Decimal("30.00")
        amazon_trans.append(amazon_tran)

    with patch("ynamazon.main.ai_api_key_configured", return_value=True), \
         patch("ynamazon.main.AmazonTransactionRetriever") as retriever, \
         patch("ynamazon.main.get_uncategorized_ynab_transactions") as get_uncategorized, \
         patch("ynamazon.main.get_ynab_transactions") as get_ynab, \
         patch("ynamazon.main.get_budget_categories", return_value=[Mock()]), \
         patch("ynamazon.main.build_memo", return_value="new memo") as build_memo, \
         patch("ynamazon.main.ask_for_categorization", return_value={"category_id": "cat-1"}) as ask, \
         patch("ynamazon.main.Confirm.ask", return_value=True) as confirm, \
         patch("ynamazon.main.update_ynab_transaction") as update:
        retriever.return_value.transaction_days = 31
        retriever.return_value.get_amazon_transactions.return_value = amazon_trans
        get_uncategorized.return_value = ([needs_memo_tran, processed_tran], NEEDS_MEMO, PROCESSED)
        yield Mock(
            get_uncategorized=get_uncategorized,
            get_ynab=get_ynab,
            build_memo=build_memo,
            ask=ask,
            confirm=confirm,
            update=update,
            needs_memo_tran=needs_memo_tran,
            processed_tran=processed_tran,
        )


def test_needs_category_skips_memo_for_processed_transactions(flow):
    process_transactions(ynab_config=Mock(), budget_id="budget", needs_category=True)

    flow.get_ynab.assert_not_called()
    since_date = flow.get_uncategorized.call_args.kwargs["since_date"]
    assert since_date == date.today() - timedelta(days=31 + 7)

    # Only the transaction still under the "needs memo" payee gets a memo
    flow.build_memo.assert_called_once()
    assert flow.confirm.call_count == 1
    assert flow.ask.call_count == 2

    updates = {call.kwargs["transaction"].id: call.kwargs for call in flow.update.call_args_list}
    assert updates["needs-memo"]["memo"] == "new memo"
    assert updates["needs-memo"]["payee_id"] == PROCESSED.id
    assert updates["needs-memo"]["category_id"] == "cat-1"
    assert "memo" not in updates["processed"] and "payee_id" not in updates["processed"]
    assert updates["processed"]["category_id"] == "cat-1"


def test_needs_category_categorizes_even_when_setting_is_off(flow):
    with patch("ynamazon.main.settings.use_ai_categorization", False):
        process_transactions(ynab_config=Mock(), budget_id="budget", needs_category=True)
    assert flow.ask.call_count == 2


def test_needs_category_exits_without_api_key(flow):
    with patch("ynamazon.main.ai_api_key_configured", return_value=False):
        process_transactions(ynab_config=Mock(), budget_id="budget", needs_category=True)

    flow.get_uncategorized.assert_not_called()
    flow.update.assert_not_called()


def test_needs_category_exits_when_nothing_is_uncategorized(flow):
    flow.get_uncategorized.return_value = ([], NEEDS_MEMO, PROCESSED)
    process_transactions(ynab_config=Mock(), budget_id="budget", needs_category=True)
    flow.ask.assert_not_called()
