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
    drop_approved,
    get_ynab_transactions,
)


def _ynab_tran(
    txn_id: str, approved: bool, var_date: date = date(2026, 7, 3)
) -> TempYnabTransaction:
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


# drop_approved

OLD, RECENT = date(2026, 6, 1), date(2026, 10, 1)


def _transactions() -> TempYnabTransactions:
    return TempYnabTransactions(
        root=[
            _ynab_tran("old-unapproved", False, OLD),
            _ynab_tran("old-approved", True, OLD),
            _ynab_tran("recent-unapproved", False, RECENT),
            _ynab_tran("recent-approved", True, RECENT),
        ]
    )


@pytest.mark.parametrize(
    ("include_approved", "approved_since", "kept"),
    [
        (False, None, ["old-unapproved", "recent-unapproved"]),
        (False, date(2026, 9, 1), ["old-unapproved", "recent-unapproved"]),
        (True, None, ["old-unapproved", "old-approved", "recent-unapproved", "recent-approved"]),
        # The date limit only applies to approved transactions
        (True, date(2026, 9, 1), ["old-unapproved", "recent-unapproved", "recent-approved"]),
    ],
)
def test_drop_approved(include_approved, approved_since, kept):
    transactions = _transactions()
    removed = drop_approved(
        transactions, include_approved=include_approved, approved_since=approved_since
    )
    assert [t.id for t in transactions] == kept
    assert removed == 4 - len(kept)


# process_transactions


@pytest.fixture
def run(capsys):
    """Run process_transactions and return the ids of the YNAB transactions it tried to match."""

    def _run(ynab_trans: list[TempYnabTransaction], **kwargs):
        by_amount = {t.amount_decimal: t.id for t in ynab_trans}
        with (
            patch("ynamazon.main.get_ynab_transactions") as get_ynab,
            patch("ynamazon.main.AmazonTransactionRetriever") as retriever,
            patch("ynamazon.main.find_amazon_transaction", return_value=None) as find,
            patch("ynamazon.main.settings.use_ai_categorization", False),
        ):
            retriever.return_value.transaction_days = 31
            retriever.return_value.get_amazon_transactions.return_value = []
            get_ynab.return_value = (
                TempYnabTransactions(root=ynab_trans),
                Mock(id="payee-processed"),
            )
            process_transactions(ynab_config=Mock(), budget_id="budget", **kwargs)

        return Mock(
            matched=[by_amount[call.kwargs["amount"]] for call in find.call_args_list],
            get_ynab=get_ynab,
            output=" ".join(capsys.readouterr().out.split()),
        )

    return _run


def _with_amounts(*specs: tuple[str, bool, date]) -> list[TempYnabTransaction]:
    """YNAB transactions with distinct amounts, so the matcher's calls show which were processed."""
    trans = []
    for i, (txn_id, approved, var_date) in enumerate(specs):
        tran = _ynab_tran(txn_id, approved, var_date)
        tran.amount = -10000 * (i + 1)
        trans.append(tran)
    return trans


def _old_and_recent() -> list[TempYnabTransaction]:
    old = date.today() - timedelta(days=31 + MATCH_WINDOW_BUFFER_DAYS + 1)
    recent = date.today() - timedelta(days=3)
    return _with_amounts(
        ("old-unapproved", False, old),
        ("old-approved", True, old),
        ("recent-unapproved", False, recent),
        ("recent-approved", True, recent),
    )


def test_unapproved_only_by_default(run):
    result = run(_old_and_recent())
    assert result.matched == ["old-unapproved", "recent-unapproved"]
    assert result.get_ynab.call_args.kwargs["since_date"] is None


def test_include_approved_limits_only_approved_to_matchable_window(run):
    """Older unapproved transactions are still processed; only approved ones are limited to the window."""
    result = run(_old_and_recent(), include_approved=True)
    assert result.matched == ["old-unapproved", "recent-unapproved", "recent-approved"]
    assert result.get_ynab.call_args.kwargs["since_date"] is None


def test_include_approved_with_date_range_includes_all_approved_in_range(run):
    date_range = DateRange(date(2026, 7, 1), date(2026, 7, 31))
    trans = _with_amounts(
        ("approved", True, date(2026, 7, 3)),
        ("unapproved", False, date(2026, 7, 5)),
        ("after-range", True, date(2026, 8, 3)),
    )
    result = run(trans, include_approved=True, date_range=date_range)
    assert result.matched == ["approved", "unapproved"]
    assert result.get_ynab.call_args.kwargs["since_date"] == date(2026, 7, 1)


def test_empty_range_says_how_many_approved_were_skipped(run):
    date_range = DateRange(date(2026, 7, 1), date(2026, 7, 31))
    trans = _with_amounts(("a", True, date(2026, 7, 3)), ("b", True, date(2026, 7, 5)))
    result = run(trans, date_range=date_range)
    assert result.matched == []
    assert "No unapproved Amazon transactions" in result.output
    assert "(2 approved ones skipped)" in result.output
    assert "--include-approved" in result.output


@pytest.mark.parametrize(
    ("trans", "kwargs"),
    [
        ([], {}),  # nothing in the range at all
        (_with_amounts(("a", True, date(2026, 8, 3))), {}),  # approved, but outside the range
        ([], {"include_approved": True}),
    ],
)
def test_empty_range_without_skipped_approved_has_no_hint(run, trans, kwargs):
    date_range = DateRange(date(2026, 7, 1), date(2026, 7, 31))
    result = run(trans, date_range=date_range, **kwargs)
    assert "No Amazon transactions to process in YNAB for this date range" in result.output
    assert "--include-approved" not in result.output


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


@pytest.mark.parametrize("setting", [True, False])
def test_print_ynab_limits_approved_to_matchable_window(setting):
    from ynamazon.cli.cli import cli

    with (
        patch("ynamazon.cli.cli.get_ynab_transactions") as get_ynab,
        patch("ynamazon.cli.cli.settings.include_approved", setting),
    ):
        get_ynab.return_value = (TempYnabTransactions(root=[]), Mock())
        CliRunner().invoke(cli, ["print-ynab"])

    kwargs = get_ynab.call_args.kwargs
    assert kwargs["include_approved"] is setting
    assert kwargs["approved_since"] == date.today() - timedelta(days=31 + MATCH_WINDOW_BUFFER_DAYS)
