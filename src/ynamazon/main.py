from typing import TYPE_CHECKING, Any, override

from loguru import logger
from pydantic import BaseModel, Field
from rich.console import Console
from rich.prompt import Confirm
from amazonorders.exception import AmazonOrdersError
from rich.table import Table
from ynab.exceptions import ApiException

from ynamazon.amazon_transactions import (
    AmazonConfig,
    AmazonTransactionRetriever,
    locate_amazon_transaction_by_amount,
)
from ynamazon.ai_categorization import suggest_categorization
from ynamazon.exceptions import YnabSetupError
from ynamazon.settings import settings
from ynamazon.ynab_categories import get_budget_categories, is_uncategorized
from ynamazon.ynab_memo import process_memo
from ynamazon.ynab_transactions import default_configuration as ynab_configuration
from ynamazon.ynab_transactions import (
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
    if whole_order and len(amazon_tran.items) > 1 and any(item.price is None for item in amazon_tran.items):
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

    if not Confirm.ask("[bold cyan]Apply AI-suggested categorization?[/]", console=console):
        console.print("[yellow]Skipping categorization...[/]")
        return {}

    return suggestion.update_kwargs()


# TODO: reduce complexity of this function
def process_transactions(  # noqa: C901
    amazon_config: AmazonConfig | None = None,
    ynab_config: "Configuration | None" = None,
    budget_id: str | None = None,
    force_refresh_amazon: bool = False,
) -> None:
    """Match YNAB transactions to Amazon Transactions and optionally update YNAB Memos."""
    amazon_config = amazon_config or AmazonConfig()
    ynab_config = ynab_config or ynab_configuration
    budget_id = budget_id or settings.ynab_budget_id.get_secret_value()

    console = Console()

    try:
        ynab_trans, amazon_with_memo_payee = get_ynab_transactions(
            configuration=ynab_config, budget_id=budget_id
        )
    except YnabSetupError:
        console.print("[bold red]No matching Transactions found in YNAB. Exiting.[/]")
        return

    console.print("[cyan]Starting search for Amazon transactions...[/]")
    amazon_retriever = AmazonTransactionRetriever(
        amazon_config=amazon_config, force_refresh_amazon=force_refresh_amazon
    )
    amazon_trans = amazon_retriever.get_amazon_transactions()

    console.print(f"[green]{len(amazon_trans)} Amazon transactions retrieved successfully.[/]")

    categories: list[Category] | None = None
    if settings.use_ai_categorization:
        try:
            categories = get_budget_categories(configuration=ynab_config, budget_id=budget_id)
        except ApiException as e:
            console.print(f"[bold red]Couldn't load YNAB categories; AI categorization is off for this run: {e}[/]")

    console.print("[cyan]Starting to look for matching transactions...[/]")
    for ynab_tran in ynab_trans:
        console.print(
            f"[cyan]Looking for an Amazon Transaction that matches this YNAB transaction:[/] {ynab_tran.var_date} ${ynab_tran.amount / -1000:.2f}"
        )
        # because YNAB uses "milliunits" for amounts, we need to convert to dollars
        logger.debug(f"YNAB transaction amount [dollars]: {ynab_tran.amount_decimal}")
        amazon_tran_index = locate_amazon_transaction_by_amount(
            amazon_trans=amazon_trans, amount=ynab_tran.amount_decimal
        )
        if not amazon_tran_index:
            console.print("[bold yellow]**** Could not find a matching Amazon Transaction![/]")
            continue

        amazon_tran = amazon_trans[amazon_tran_index]
        console.print(
            f"[green]Matching Amazon Transaction:[/] {amazon_tran.completed_date} ${amazon_tran.transaction_total:.2f}"
        )

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
            memo = process_memo(str(memo))

        console.print("[bold u green]Processed Memo:[/]")
        console.print(memo)

        if amazon_tran.completed_date != ynab_tran.var_date:
            console.print(
                f"[yellow]**** The dates don't match! YNAB: {ynab_tran.var_date} Amazon: {amazon_tran.completed_date}[/]"
            )
            continue_match = Confirm.ask(
                "[bold red]Continue matching this transaction anyway?[/]",
                console=console,
            )
            if not continue_match:
                console.print("[yellow]Skipping this transaction...[/]")
                continue
            else:
                _ = amazon_trans.pop(amazon_tran_index)
                console.log("Removing matched transaction from search")

        update_fields: dict[str, Any] = {}
        update_memo = Confirm.ask(
            "[bold cyan]Update YNAB transaction memo?[/]", console=console
        )
        if update_memo:
            update_fields.update(memo=memo, payee_id=amazon_with_memo_payee.id)
        else:
            console.print("[yellow]Skipping YNAB transaction memo update...[/]\n\n")
            console.print("[cyan i]Memo Preview[/]:")
            console.print(str(memo))

        if categories is not None:
            if is_uncategorized(ynab_tran):
                update_fields.update(
                    ask_for_categorization(console, amazon_tran, ynab_tran, categories, amazon_retriever)
                )
            else:
                console.print(f"[dim]Already categorized as {ynab_tran.category_name}; skipping AI categorization.[/]")

        if not update_fields:
            continue

        console.print("[green]Updating YNAB transaction...[/]")

        update_ynab_transaction(
            transaction=ynab_tran,
            configuration=ynab_config,
            budget_id=budget_id,
            **update_fields,
        )
        console.print("\n\n")


if __name__ == "__main__":
    process_transactions()
