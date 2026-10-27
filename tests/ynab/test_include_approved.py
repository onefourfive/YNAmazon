"""Tests for --include-approved: processing approved transactions under the "needs memo" payee."""

from datetime import date, timedelta
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from ynamazon.date_range import DateRange
from ynamazon.main import MATCH_WINDOW_BUFFER_DAYS, process_transactions
from ynamazon.ynab_transactions import (
    TempYnabTransaction,
    TempYnabTransactions,
    get_ynab_transactions,
)


def _ynab_tran(txn_id: str, approved: bool, var_date: date = date(2026, 7, 3)) -> TempYnabTransaction:
    return TempYnabTransaction.model_validate(
        {
            "id": txn_id,
            "date": var_date.isoformat(),
            "amount": -30000,
            "cleared": "uncleared",
            "approved": approved,
            "account_id": "acct",
            "account_name": "Card",
            "deleted": False,
            "type": "transaction",
            "payee_id": "payee-needs-memo",
            "category_id": None,
            "subtransactions": [],
        }
    )


# get_ynab_transactions


@pytest.mark.parametrize(
    ("include_approved", "expected"), [(False, ["unapproved"]), (True, ["approved", "unapproved"])]
)
def test_get_ynab_transactions_filters_approved(include_approved, expected):
    found = [_ynab_tran("approved", True), _ynab_tran("unapproved", False)]
    with (
        patch(
            "ynamazon.ynab_transactions.get_amazon_payees",
            return_value=(Mock(id="payee-needs-memo"), Mock(id="payee-processed")),
        ),
        patch(
            "ynamazon.ynab_transactions.TempYnabTransactions.get_by_payee",
            return_value=TempYnabTransactions(root=found),
        ),
    ):
        transactions, _payee = get_ynab_transactions(
            configuration=Mock(), budget_id="budget", include_approved=include_approved
        )

    assert [t.id for t in transactions] == expected


# process_transactions


@pytest.fixture
def get_ynab():
    """Patch YNAB and Amazon so process_transactions stops after finding no YNAB transactions."""
    with (
        patch("ynamazon.main.get_ynab_transactions") as get_ynab,
        patch("ynamazon.main.AmazonTransactionRetriever") as retriever,
    ):
        retriever.return_value.transaction_days = 31
        retriever.return_value.get_amazon_transactions.return_value = []
        get_ynab.return_value = (TempYnabTransactions(root=[]), Mock(id="payee-processed"))
        yield get_ynab


def test_unapproved_only_looks_back_without_limit(get_ynab):
    process_transactions(ynab_config=Mock(), budget_id="budget")
    assert get_ynab.call_args.kwargs["since_date"] is None
    assert get_ynab.call_args.kwargs["include_approved"] is False


def test_include_approved_limits_to_matchable_window(get_ynab):
    process_transactions(ynab_config=Mock(), budget_id="budget", include_approved=True)
    assert get_ynab.call_args.kwargs["include_approved"] is True
    assert get_ynab.call_args.kwargs["since_date"] == date.today() - timedelta(
        days=31 + MATCH_WINDOW_BUFFER_DAYS
    )


def test_include_approved_uses_date_range(get_ynab):
    date_range = DateRange(date(2026, 7, 1), date(2026, 7, 31))
    process_transactions(
        ynab_config=Mock(), budget_id="budget", include_approved=True, date_range=date_range
    )
    assert get_ynab.call_args.kwargs["since_date"] == date(2026, 7, 1)


@pytest.mark.parametrize(
    ("include_approved", "hint_shown"), [(False, True), (True, False)]
)
def test_empty_range_message_mentions_approved(get_ynab, capsys, include_approved, hint_shown):
    date_range = DateRange(date(2026, 7, 1), date(2026, 7, 31))
    process_transactions(
        ynab_config=Mock(),
        budget_id="budget",
        include_approved=include_approved,
        date_range=date_range,
    )
    output = " ".join(capsys.readouterr().out.split())
    assert ("--include-approved" in output) is hint_shown
    assert "to process in YNAB for this date range" in output


# CLI


def _include_approved_seen(args: list[str], setting: bool = False) -> bool:
    """Runs the CLI and returns the include_approved value passed to process_transactions."""
    from ynamazon.cli.cli import cli

    with (
        patch("ynamazon.cli.cli.process_transactions") as process,
        patch("ynamazon.cli.cli.settings.include_approved", setting),
        patch("sys.argv", ["yna", *args]),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    return process.call_args.kwargs["include_approved"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["ynamazon", "--include-approved"], True),
        (["--include-approved", "ynamazon"], True),  # flag given to yna, before the subcommand
        (["--include-approved"], True),  # plain yna re-runs ynamazon from the command line
        (["ynamazon"], False),
        ([], False),
    ],
)
def test_cli_include_approved_flag(args, expected):
    assert _include_approved_seen(args) is expected


@pytest.mark.parametrize("args", [["ynamazon"], []])
def test_cli_include_approved_setting(args):
    assert _include_approved_seen(args, setting=True) is True
