from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, override

from loguru import logger
from pydantic import BaseModel, Field
from rich.console import Console
from amazonorders.exception import AmazonOrdersError
from rich.table import Table
from ynab.exceptions import ApiException

from ynamazon.amazon_transactions import (
    AmazonConfig,
    AmazonTransactionRetriever,
    find_amazon_transaction,
)
from ynamazon.ai_categorization import suggest_categorization
from ynamazon.ai_clients import ai_api_key_configured
from ynamazon.confirm import confirm
from ynamazon.date_range import DEFAULT_RANGE_DAYS, DateRange
from ynamazon.exceptions import YnabSetupError
from ynamazon.settings import settings
from ynamazon.ynab_categories import get_budget_categories, is_uncategorized
from ynamazon.ynab_memo import process_memo
from ynamazon.ynab_transactions import default_configuration as ynab_configuration
from ynamazon.ynab_transactions import (
    get_uncategorized_ynab_transactions,
    drop_approved,
    get_ynab_transactions,
    markdown_formatted_link,
    markdown_formatted_title,
    update_ynab_transaction,
)

try:
    from ynamazon.ynab_memo import process_memo
except ImportError:
    pass

if TYPE_CHECKING:
    from ynab.configuration import Configuration
    from ynab.models.category import Category

    from ynamazon.amazon_transactions import AmazonTransactionWithOrderInfo
    from ynamazon.ynab_transactions import TempYnabTransaction


# Extra days of YNAB transactions to consider beyond the Amazon transaction window, when the YNAB transactions
# would otherwise go back further (needs-category mode, or approved transactions included)
MATCH_WINDOW_BUFFER_DAYS = 7


def matchable_since(transaction_days: int = DEFAULT_RANGE_DAYS) -> date:
    """Returns the earliest YNAB date worth considering when Amazon transactions go back `transaction_days`.

    Older transactions can't be matched to the Amazon transactions that get retrieved. The extra days allow for
    YNAB dating a charge a little earlier than Amazon's completed date.
    """
    return date.today() - timedelta(days=transaction_days + MATCH_WINDOW_BUFFER_DAYS)


class MultiLineText(BaseModel):
    """A class to handle multi-line text."""

    lines: list[str] = Field(default_factory=list)

    @override
    def __str__(self) -> str:
        """Returns the string representation of the object."""
        return "\n".join(self.lines)

    def append(self, line: str) -> None:
        """Appends a line to the text."""
        self.lines.append(line)


def ask_for_categorization(
    console: Console,
    amazon_tran: "AmazonTransactionWithOrderInfo",
    ynab_tran: "TempYnabTransaction",
    categories: "list[Category]",
    retriever: AmazonTransactionRetriever,
) -> dict[str, Any]:
    """Shows an AI-suggested category (or split) and asks whether to apply it.

    Returns:
        The keyword arguments for `update_ynab_transaction` that apply the suggestion, or an empty dict if
        there's no suggestion or it was declined.
    """
    # Splits are calculated from item prices, which only the order details page has. A split is only
    # possible for a multi-item charge that covers the whole order, so only fetch details then.
    whole_order = amazon_tran.transaction_total == amazon_tran.order_total
    if (
        whole_order
        and len(amazon_tran.items) > 1
        and any(item.price is None for item in amazon_tran.items)
    ):
        try:
            items = retriever.get_order_items(amazon_tran.order_number)
            amazon_tran = amazon_tran.model_copy(update={"items": items})
        except AmazonOrdersError as e:
            logger.warning(f"Couldn't load item prices for order {amazon_tran.order_number}: {e}")

    console.print("[cyan]Asking AI for a category suggestion...[/]")
    suggestion = suggest_categorization(amazon_tran, ynab_tran, categories)
    if suggestion is None:
        console.print("[yellow]No AI category suggestion available for this transaction.[/]")
        return {}

    title = "AI-Suggested Split" if suggestion.is_split else "AI-Suggested Category"
    table = Table(title=title, caption=suggestion.reason)
    table.add_column("Category", style="cyan")
    table.add_column("Amount", justify="right", style="green")
    table.add_column("Items")
    for split in suggestion.splits:
        table.add_row(
            f"{split.category.category_group_name}: {split.category.name}",
            f"${split.amount / -1000:.2f}",
            "\n".join(split.items),
        )
    console.print(table)

    if not confirm("[bold cyan]Apply AI-suggested categorization?[/]", console=console):
        console.print("[yellow]Skipping categorization...[/]")
        return {}

    return suggestion.update_kwargs()


def build_memo(console: Console, amazon_tran: "AmazonTransactionWithOrderInfo") -> str:
    """Builds the YNAB memo for an Amazon transaction, processing it with AI if enabled."""
    memo = MultiLineText()
    if amazon_tran.transaction_total != amazon_tran.order_total:
        memo.append(
            f"-This transaction doesn't represent the entire order. The order total is ${amazon_tran.order_total:.2f}-"
        )
    if len(amazon_tran.items) > 1:
        memo.append("**Items**")
        for i, item in enumerate(amazon_tran.items, start=1):
            memo.append(f"{i}. {markdown_formatted_title(item.title, item.link)}")
    elif len(amazon_tran.items) == 1:
        item = amazon_tran.items[0]
        memo.append(f"- {markdown_formatted_title(item.title, item.link)}")

    memo.append(
        markdown_formatted_link(f"\nOrder #{amazon_tran.order_number}", amazon_tran.order_link)
    )

    console.print("[bold u green]Memo:[/]")
    console.print(str(memo))

    # Only use the AI processing if OpenAI is installed
    if "process_memo" in globals():
        processed_memo = process_memo(str(memo))
    else:
        processed_memo = str(memo)

    console.print("[bold u green]Processed Memo:[/]")
    console.print(processed_memo)

    return processed_memo


# TODO: reduce complexity of this function
def process_transactions(  # noqa: C901
    amazon_config: AmazonConfig | None = None,
    ynab_config: "Configuration | None" = None,
    budget_id: str | None = None,
    force_refresh_amazon: bool = False,
    needs_category: bool = False,
    date_range: DateRange | None = None,
    include_approved: bool = False,
) -> None:
    """Match YNAB transactions to Amazon Transactions and optionally update YNAB Memos.

    With `needs_category`, process uncategorized transactions for both Amazon payees instead, including ones
    whose memo was already processed, and suggest categories for them. Transactions that still need a memo
    get the memo prompt too.

    With `date_range`, only YNAB transactions dated within it are processed, and Amazon orders and
    transactions are retrieved to cover it instead of the default window.

    Only unapproved transactions are processed unless `include_approved` is set. `needs_category` always
    includes approved transactions.
    """
    amazon_config = amazon_config or AmazonConfig()
    ynab_config = ynab_config or ynab_configuration
    budget_id = budget_id or settings.ynab_budget_id.get_secret_value()

    console = Console()

    if needs_category and not ai_api_key_configured():
        console.print(
            f"[bold red]--needs-category needs an API key for the '{settings.ai_provider}' AI provider. Exiting.[/]"
        )
        return

    if date_range is None:
        amazon_retriever = AmazonTransactionRetriever(
            amazon_config=amazon_config, force_refresh_amazon=force_refresh_amazon
        )
    else:
        console.print(
            f"[cyan]Processing transactions from {date_range.since} to {date_range.until}.[/]"
        )
        amazon_retriever = AmazonTransactionRetriever(
            amazon_config=amazon_config,
            order_years=date_range.amazon_order_years(),
            transaction_days=date_range.amazon_transaction_days(),
            force_refresh_amazon=force_refresh_amazon,
            latest_date=date_range.latest_amazon_date(),
        )

    # In needs-category mode, only transactions with this payee still need a memo
    needs_memo_payee_id: str | None = None
    if date_range is not None:
        since_date = date_range.since
    elif needs_category:
        since_date = matchable_since(amazon_retriever.transaction_days)
    else:
        since_date = None
    try:
        if needs_category:
            ynab_trans, amazon_needs_memo_payee, amazon_with_memo_payee = (
                get_uncategorized_ynab_transactions(
                    configuration=ynab_config, budget_id=budget_id, since_date=since_date
                )
            )
            needs_memo_payee_id = amazon_needs_memo_payee.id
        else:
            ynab_trans, amazon_with_memo_payee = get_ynab_transactions(
                configuration=ynab_config,
                budget_id=budget_id,
                since_date=since_date,
                # Approved transactions are dropped below, once the ones skipped in a date range can be counted
                include_approved=True,
            )
    except YnabSetupError:
        console.print("[bold red]No matching Transactions found in YNAB. Exiting.[/]")
        return

    if date_range is not None:
        # YNAB can only filter by start date, so apply the end of the range here
        ynab_trans.filter(lambda t: date_range.contains(t.var_date))

    skipped_approved = 0
    if not needs_category:
        # Without a date range, only approved transactions recent enough to be matched are included
        skipped_approved = drop_approved(
            ynab_trans,
            include_approved=include_approved,
            approved_since=matchable_since(amazon_retriever.transaction_days)
            if include_approved and date_range is None
            else None,
        )

    if date_range is not None:
        if not ynab_trans:
            if skipped_approved and not include_approved:
                console.print(
                    "[green]No unapproved Amazon transactions to process in YNAB for this date range "
                    f"({skipped_approved} approved ones skipped). Use --include-approved to include them.[/]"
                )
            else:
                console.print(
                    "[green]No Amazon transactions to process in YNAB for this date range.[/]"
                )
            return
        console.print(f"[green]{len(ynab_trans)} YNAB transactions in this date range.[/]")

    if needs_category:
        if not ynab_trans:
            console.print("[green]No uncategorized Amazon transactions found in YNAB.[/]")
            return
        console.print(
            f"[green]{len(ynab_trans)} uncategorized Amazon transactions found in YNAB.[/]"
        )

    console.print("[cyan]Starting search for Amazon transactions...[/]")
    amazon_trans = amazon_retriever.get_amazon_transactions()
    if date_range is not None:
        # Amazon can only limit transactions by how far back to go, so apply the end of the range here
        amazon_trans = [
            a_tran for a_tran in amazon_trans if date_range.contains_amazon(a_tran.completed_date)
        ]

    console.print(f"[green]{len(amazon_trans)} Amazon transactions retrieved successfully.[/]")

    categories: list[Category] | None = None
    if settings.use_ai_categorization or needs_category:
        try:
            categories = get_budget_categories(configuration=ynab_config, budget_id=budget_id)
        except ApiException as e:
            if needs_category:
                console.print(f"[bold red]Couldn't load YNAB categories. Exiting: {e}[/]")
                return
            console.print(
                f"[bold red]Couldn't load YNAB categories; AI categorization is off for this run: {e}[/]"
            )

    console.print("[cyan]Starting to look for matching transactions...[/]")
    for ynab_tran in ynab_trans:
        console.print(
            f"[cyan]Looking for an Amazon Transaction that matches this YNAB transaction:[/] {ynab_tran.var_date} ${ynab_tran.amount / -1000:.2f}"
        )
        # because YNAB uses "milliunits" for amounts, we need to convert to dollars
        logger.debug(f"YNAB transaction amount [dollars]: {ynab_tran.amount_decimal}")
        amazon_tran = find_amazon_transaction(
            amazon_trans=amazon_trans, amount=ynab_tran.amount_decimal, on_date=ynab_tran.var_date
        )
        if amazon_tran is None:
            console.print("[bold yellow]**** Could not find a matching Amazon Transaction![/]")
            continue

        console.print(
            f"[green]Matching Amazon Transaction:[/] {amazon_tran.completed_date} ${amazon_tran.transaction_total:.2f}"
        )

        if amazon_tran.completed_date != ynab_tran.var_date:
            console.print(
                f"[yellow]**** The dates don't match! YNAB: {ynab_tran.var_date} Amazon: {amazon_tran.completed_date}[/]"
            )
            continue_match = confirm(
                "[bold red]Continue matching this transaction anyway?[/]",
                console=console,
            )
            if not continue_match:
                console.print("[yellow]Skipping this transaction...[/]")
                continue

        memo_needed = needs_memo_payee_id is None or ynab_tran.payee_id == needs_memo_payee_id
        if memo_needed:
            memo = build_memo(console, amazon_tran)
        else:
            console.print("[dim]Memo already processed; only suggesting a category.[/]")

        update_fields: dict[str, Any] = {}
        if memo_needed:
            update_memo = confirm("[bold cyan]Update YNAB transaction memo?[/]", console=console)
            if update_memo:
                update_fields.update(memo=memo, payee_id=amazon_with_memo_payee.id)
            else:
                console.print("[yellow]Skipping YNAB transaction memo update...[/]\n\n")
                console.print("[cyan i]Memo Preview[/]:")
                console.print(str(memo))

        if categories is not None:
            if is_uncategorized(ynab_tran):
                update_fields.update(
                    ask_for_categorization(
                        console, amazon_tran, ynab_tran, categories, amazon_retriever
                    )
                )
            else:
                console.print(
                    f"[dim]Already categorized as {ynab_tran.category_name}; skipping AI categorization.[/]"
                )

        if not update_fields:
            continue

        console.print("[green]Updating YNAB transaction...[/]")

        update_ynab_transaction(
            transaction=ynab_tran,
            configuration=ynab_config,
            budget_id=budget_id,
            **update_fields,
        )

        # This Amazon transaction is now accounted for, so later YNAB transactions can't match it too
        amazon_trans = [a_tran for a_tran in amazon_trans if a_tran is not amazon_tran]
        console.print("\n\n")


if __name__ == "__main__":
    process_transactions()
