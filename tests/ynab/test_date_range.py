"""Tests for --since/--until date ranges."""

from datetime import date
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from ynamazon.date_range import DateRange, DateRangeError, months_before, parse_date
from ynamazon.main import process_transactions
from ynamazon.ynab_transactions import TempYnabTransaction, TempYnabTransactions

TODAY = date(2026, 10, 24)


# Parsing and validation


def test_parse_date():
    assert parse_date("20261001", "--since") == date(2026, 10, 1)
    assert parse_date(" 20240229 ", "--since") == date(2024, 2, 29)


@pytest.mark.parametrize("value", ["2026-10-01", "261001", "2026100", "202610011", "abcdefgh", ""])
def test_parse_date_rejects_wrong_format(value):
    with pytest.raises(DateRangeError, match="YYYYMMDD format"):
        parse_date(value, "--since")


@pytest.mark.parametrize("value", ["20260231", "20251301", "20250229", "20260000"])
def test_parse_date_rejects_impossible_dates(value):
    with pytest.raises(DateRangeError, match="isn't a valid date"):
        parse_date(value, "--since")


def test_no_options_means_no_range():
    assert DateRange.from_options(None, None, today=TODAY) is None


def test_until_defaults_to_today_when_missing_or_empty():
    assert DateRange.from_options("20261001", None, today=TODAY) == DateRange(
        date(2026, 10, 1), TODAY
    )
    assert DateRange.from_options("20261001", "", today=TODAY) == DateRange(
        date(2026, 10, 1), TODAY
    )


def test_until_alone_looks_back_default_window():
    assert DateRange.from_options(None, "20260930", today=TODAY) == DateRange(
        date(2026, 8, 30), date(2026, 9, 30)
    )


def test_single_day_range():
    date_range = DateRange.from_options("20261005", "20261005", today=TODAY)
    assert date_range is not None and date_range.days == 1


@pytest.mark.parametrize(
    ("since", "until", "message"),
    [
        ("20261025", None, "--since 20261025 is in the future"),
        (None, "20261025", "--until 20261025 is in the future"),
        ("20261010", "20261001", "--since 20261010 is after --until 20261001"),
        ("19941231", None, "--since 19941231 is before 1995"),
        (None, "19941231", "--until 19941231 is before 1995"),
    ],
)
def test_invalid_ranges(since, until, message):
    with pytest.raises(DateRangeError, match=message):
        DateRange.from_options(since, until, today=TODAY)


# Large ranges


def test_months_before_clamps_to_month_end():
    assert months_before(date(2026, 8, 31), 6) == date(2026, 2, 28)
    assert months_before(date(2026, 3, 15), 6) == date(2025, 9, 15)


@pytest.mark.parametrize(
    ("since", "until", "large"),
    [
        (date(2026, 4, 24), date(2026, 10, 24), False),  # exactly 6 months
        (date(2026, 4, 23), date(2026, 10, 24), True),
        (date(2025, 9, 1), date(2026, 2, 28), False),
        (date(2026, 2, 27), date(2026, 8, 31), True),
    ],
)
def test_is_large(since, until, large):
    assert DateRange(since, until).is_large() is large


# Retrieval windows


def test_amazon_windows_cover_range_with_buffers():
    date_range = DateRange(date(2026, 1, 10), date(2026, 3, 31))
    # Orders from late December can be charged in January
    assert date_range.amazon_order_years() == ["2025", "2026"]
    assert date_range.amazon_transaction_days(today=TODAY) == (TODAY - date(2026, 1, 10)).days + 7

    assert date_range.contains(date(2026, 1, 10)) and date_range.contains(date(2026, 3, 31))
    assert not date_range.contains(date(2026, 4, 1))
    assert date_range.contains_amazon(date(2026, 1, 3)) and date_range.contains_amazon(
        date(2026, 4, 7)
    )
    assert not date_range.contains_amazon(date(2026, 4, 8))


def test_amazon_order_years_spans_multiple_years():
    assert DateRange(date(2024, 6, 1), date(2026, 2, 1)).amazon_order_years() == [
        "2024",
        "2025",
        "2026",
    ]


# process_transactions


def _ynab_tran(txn_id: str, var_date: date, amount: int = -30000) -> TempYnabTransaction:
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


def _amazon_tran(total: str, completed_date: date) -> Mock:
    amazon_tran = Mock()
    amazon_tran.transaction_total = Decimal(total)
    amazon_tran.order_total = Decimal(total)
    amazon_tran.completed_date = completed_date
    amazon_tran.items = []
    return amazon_tran


def test_process_transactions_limits_work_to_range():
    date_range = DateRange(date(2026, 9, 1), date(2026, 9, 30))
    in_range = _ynab_tran("in-range", date(2026, 9, 15))
    after_range = _ynab_tran("after-range", date(2026, 10, 5), amount=-12000)
    # Same amount as in_range, but too far after the range to be its Amazon transaction
    too_late = _amazon_tran("30.00", date(2026, 10, 20))
    matching = _amazon_tran("30.00", date(2026, 9, 15))

    with (
        patch("ynamazon.main.get_ynab_transactions") as get_ynab,
        patch("ynamazon.main.AmazonTransactionRetriever") as retriever,
        patch(
            "ynamazon.main.build_memo", side_effect=lambda console, a_tran: f"memo for {id(a_tran)}"
        ),
        patch("ynamazon.main.confirm", return_value=True),
        patch("ynamazon.main.update_ynab_transaction") as update,
        patch("ynamazon.main.settings.use_ai_categorization", False),
    ):
        get_ynab.return_value = (
            TempYnabTransactions(root=[in_range, after_range]),
            Mock(id="payee-processed"),
        )
        retriever.return_value.get_amazon_transactions.return_value = [too_late, matching]

        process_transactions(ynab_config=Mock(), budget_id="budget", date_range=date_range)

    assert get_ynab.call_args.kwargs["since_date"] == date(2026, 9, 1)
    retriever_kwargs = retriever.call_args.kwargs
    assert retriever_kwargs["order_years"] == ["2026"]
    assert retriever_kwargs["transaction_days"] == date_range.amazon_transaction_days()

    update.assert_called_once()
    assert update.call_args.kwargs["transaction"].id == "in-range"
    assert update.call_args.kwargs["memo"] == f"memo for {id(matching)}"


def test_process_transactions_without_range_is_unchanged():
    with (
        patch("ynamazon.main.get_ynab_transactions") as get_ynab,
        patch("ynamazon.main.AmazonTransactionRetriever") as retriever,
    ):
        get_ynab.return_value = (TempYnabTransactions(root=[]), Mock(id="payee-processed"))
        retriever.return_value.get_amazon_transactions.return_value = []
        process_transactions(ynab_config=Mock(), budget_id="budget")

    assert get_ynab.call_args.kwargs["since_date"] is None
    assert "order_years" not in retriever.call_args.kwargs
    assert "transaction_days" not in retriever.call_args.kwargs


# CLI


@pytest.fixture
def cli_run():
    from ynamazon.cli.cli import cli

    def _run(args: list[str], input: str | None = None):
        with patch("ynamazon.cli.cli.process_transactions") as process:
            result = CliRunner().invoke(cli, ["ynamazon", *args], input=input)
        return result, process

    return _run


def test_cli_passes_date_range(cli_run):
    result, process = cli_run(["--since", "20261001", "--until", "20261015"])
    assert result.exit_code == 0, result.output
    assert process.call_args.kwargs["date_range"] == DateRange(
        date(2026, 10, 1), date(2026, 10, 15)
    )


def test_cli_without_dates_passes_no_range(cli_run):
    result, process = cli_run([])
    assert result.exit_code == 0, result.output
    assert process.call_args.kwargs["date_range"] is None


def test_cli_rejects_invalid_date(cli_run):
    result, process = cli_run(["--since", "20261301"])
    assert result.exit_code != 0
    assert "isn't a valid date" in result.output
    process.assert_not_called()


def test_cli_large_range_needs_confirmation(cli_run):
    result, process = cli_run(["--since", "20250101", "--until", "20260101"], input="n\n")
    assert result.exit_code == 0
    assert "more than 6 months" in result.output
    process.assert_not_called()

    result, process = cli_run(["--since", "20250101", "--until", "20260101"], input="y\n")
    assert result.exit_code == 0, result.output
    process.assert_called_once()
