"""Tests for --force-refresh-amazon (#6)."""

from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ynamazon.amazon_transactions import AmazonConfig
from ynamazon.cli.cli import cli
from ynamazon.date_range import DateRange
from ynamazon.main import process_transactions
from ynamazon.ynab_transactions import TempYnabTransactions


@pytest.mark.parametrize(("args", "expected"), [(["--force-refresh-amazon"], True), ([], False)])
def test_cli_passes_force_refresh_to_process_transactions(args, expected):
    with patch("ynamazon.cli.cli.process_transactions") as process:
        result = CliRunner().invoke(cli, ["ynamazon", *args])

    assert result.exit_code == 0, result.output
    assert process.call_args.kwargs["force_refresh_amazon"] is expected
    assert isinstance(process.call_args.kwargs["amazon_config"], AmazonConfig)


@pytest.mark.parametrize("date_range", [None, DateRange.from_options("20261001", "20261015")])
def test_process_transactions_passes_force_refresh_to_retriever(date_range):
    with patch("ynamazon.main.get_ynab_transactions") as get_ynab, \
         patch("ynamazon.main.AmazonTransactionRetriever") as retriever:
        get_ynab.return_value = (TempYnabTransactions(root=[]), Mock(id="payee-processed"))
        retriever.return_value.get_amazon_transactions.return_value = []
        process_transactions(ynab_config=Mock(), budget_id="budget", force_refresh_amazon=True, date_range=date_range)

    assert retriever.call_args.kwargs["force_refresh_amazon"] is True


def test_amazon_config_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="force_refresh_amazon"):
        AmazonConfig(username="test@example.com", password="x", force_refresh_amazon=True)  # type: ignore[call-arg]
