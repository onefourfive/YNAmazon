# pyright: reportDeprecated=false
import os
import tempfile
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Union  # ,  Self  # not available python <3.11

from amazonorders.entity.item import Item
from amazonorders.entity.order import Order
from amazonorders.entity.transaction import Transaction
from amazonorders.exception import AmazonOrdersError
from amazonorders.orders import AmazonOrders
from amazonorders.session import AmazonSession
from amazonorders.transactions import AmazonTransactions
from cache_decorator import Cache
from loguru import logger
from pydantic import AnyUrl, BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator
from rich import print as rprint
from rich.table import Table

from .date_range import ORDER_LEAD_DAYS
from .exceptions import AmazonLoginError
from .settings import settings
from .types_pydantic import AmazonItemType

# Amazon's transaction history stops after about 100 transactions, however far back it's asked to go. A history
# with fewer than this many transactions is complete.
HISTORY_LIMIT_THRESHOLD = 90

# Extra days of transaction history to ask for: a transaction from before the window shows the history covers it
HISTORY_OVERLAP_DAYS = 30


class AmazonTransactionWithOrderInfo(BaseModel):
    """Amazon transaction with order info."""

    completed_date: date
    transaction_total: Annotated[
        Decimal, Field(description="Value is inverted, e.g. -10.00 -> 10.00")
    ]
    order_total: Decimal
    order_number: str
    order_link: AnyUrl
    items: list[AmazonItemType]

    @field_validator("transaction_total", mode="after")
    @classmethod
    def invert_value(cls, value: Decimal) -> Decimal:
        """Inverts the value."""
        return -value

    # TODO: when dropping support for python <3.11, use Self
    @classmethod
    def from_transaction_and_orders(cls, orders_dict: "dict[str, Order]", transaction: Transaction):
        """Creates an instance from an order and transactions."""
        order = orders_dict.get(transaction.order_number)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
        if order is None:
            raise ValueError(f"Order with number {transaction.order_number} not found.")  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        return cls(
            completed_date=transaction.completed_date,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
            transaction_total=transaction.grand_total,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
            order_total=order.grand_total,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
            order_number=order.order_number,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
            order_link=order.order_details_link,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
            items=order.items,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
        )


class AmazonConfig(BaseModel):
    """Configuration for Amazon transactions.

    Attributes:
        username (EmailStr): Amazon account email.
        password (SecretStr): Amazon account password.
    """

    # Reject unknown fields so a misplaced option raises instead of being silently dropped
    model_config = ConfigDict(extra="forbid")

    username: EmailStr = Field(default_factory=lambda: settings.amazon_user)
    password: SecretStr = Field(default_factory=lambda: settings.amazon_password)
    debug: bool = False

    def amazon_session(self) -> AmazonSession:
        """Creates an Amazon session."""
        logger.debug(f"Creating Amazon session for with debug={self.debug}")
        return AmazonSession(
            username=self.username,
            password=self.password.get_secret_value(),
            debug=self.debug,
        )


class AmazonTransactionRetriever:
    def __init__(
        self,
        amazon_config: AmazonConfig,
        order_years: list[str] | None = None,
        transaction_days: int = 31,
        force_refresh_amazon: bool = False,
        latest_date: date | None = None,
    ):
        """Initialize an AmazonTransactionRetriever.

        amazon_config (AmazonConfig): Configuration for Amazon, primarily credentials
        order_years (list[int] | None): A list of years to fetch transactions for. `None` for the current year.
        transaction_days (int): Number of days to fetch transactions for. Defaults to 31.
        force_refresh_amazon (bool): Refresh cache by fetching transactions directly from Amazon.
        latest_date (date | None): The latest transaction date that's needed. Orders placed after it aren't looked
            up one by one when the transaction history runs out. `None` for no limit.
        """
        self.amazon_config = amazon_config
        self.order_years = self.__class__._normalized_years(order_years)
        self.transaction_days = transaction_days
        self.force_refresh_amazon = force_refresh_amazon
        self.latest_date = latest_date

        # for memoizing the results of method calls
        self._memo = {}

    def get_amazon_transactions(self) -> list[AmazonTransactionWithOrderInfo]:
        """Get Amazon transactions linked to orders.

        This method exists as a layer to force caching to work one level below with all relevant parameters considered

        Returns:
            list[TransactionWithOrderInfo]: A list of transactions with order info
        """
        return self._get_amazon_transactions(
            order_years=self.order_years,
            transaction_days=self.transaction_days,
            amazon_config=self.amazon_config,
            latest_date=self.latest_date,
            use_cache=not self.force_refresh_amazon,
        )

    @Cache(
        validity_duration="2h",
        enable_cache_arg_name="use_cache",
        cache_path=os.path.join(
            tempfile.gettempdir(),
            "ynamazon",
            "amazon_transactions_get_amazon_transactions_{_hash}.pkl",
        ),
    )
    def _get_amazon_transactions(
        self,
        order_years: list[str],
        transaction_days: int,
        amazon_config: AmazonConfig,
        latest_date: date | None,
    ) -> list[AmazonTransactionWithOrderInfo]:
        amazon_transactions = self._amazon_transactions()

        # Looking up older transactions can fetch orders from years before order_years, so join afterwards
        orders_dict = {order.order_number: order for order in self._fetched_orders()}

        amazon_transaction_with_order_details: list[AmazonTransactionWithOrderInfo] = []
        for transaction in amazon_transactions:
            try:
                amazon_transaction_with_order_details.append(
                    AmazonTransactionWithOrderInfo.from_transaction_and_orders(
                        orders_dict=orders_dict, transaction=transaction
                    )
                )
            except ValueError:
                logger.debug(
                    f"Transaction {transaction.order_number} not found in retrieved orders."
                )
                continue

        return amazon_transaction_with_order_details

    def _amazon_orders(self) -> list[Order]:
        """Returns the Amazon orders placed in `order_years`, oldest first.

        Returns:
            list[Order]: A list of Amazon orders.
        """
        all_orders = [order for year in self.order_years for order in self._orders_for_year(year)]
        all_orders.sort(key=lambda order: order.order_placed_date)

        return all_orders

    def _orders_for_year(self, year: str) -> list[Order]:
        """Fetches the Amazon orders placed in a year, once."""
        orders_by_year = self._memo.setdefault("orders_by_year", {})
        if year not in orders_by_year:
            if "orders_api" not in self._memo:
                self._memo["orders_api"] = AmazonOrders(self._session())
            orders_by_year[year] = self._memo["orders_api"].get_orders(year=year)

        return orders_by_year[year]

    def _fetched_orders(self) -> list[Order]:
        """Returns the orders for `order_years`, plus any other years fetched to look up older transactions."""
        self._amazon_orders()

        return [order for orders in self._memo["orders_by_year"].values() for order in orders]

    def _amazon_transactions(self) -> list[Transaction]:
        """Fetches and sorts Amazon transactions.

        Amazon's transaction history stops after about 100 transactions, however far back it's asked to go. When
        it's cut off before the requested window starts, the transactions for orders placed in the gap are looked
        up order by order, which Amazon still allows.
        """
        if "amazon_transactions" in self._memo:
            return self._memo["amazon_transactions"]

        start = date.today() - timedelta(days=self.transaction_days)
        amazon_transactions = AmazonTransactions(amazon_session=self._session())

        transactions = amazon_transactions.get_transactions(
            days=self.transaction_days + HISTORY_OVERLAP_DAYS
        )
        history_start = min((t.completed_date for t in transactions), default=start)
        # A short history is complete, even if nothing in it is from before the window
        if history_start >= start and len(transactions) >= HISTORY_LIMIT_THRESHOLD:
            transactions.extend(
                self._transactions_by_order(amazon_transactions, start, transactions)
            )

        transactions = [t for t in transactions if t.completed_date >= start]
        transactions.sort(key=lambda trans: trans.completed_date)

        self._memo["amazon_transactions"] = transactions

        return self._memo["amazon_transactions"]

    def _transactions_by_order(
        self,
        amazon_transactions: AmazonTransactions,
        start: date,
        history: list[Transaction],
    ) -> list[Transaction]:
        """Looks up the transactions the cut-off history didn't reach, for orders placed in the gap.

        Orders are looked up from those placed a little before `start` (they can be charged later) through the
        history's earliest date, or `latest_date` if that's earlier. The history can stop partway through its
        earliest date, so transactions on that date are kept unless the history already has them.

        Args:
            amazon_transactions (AmazonTransactions): The transaction API to look the orders up with.
            start (date): The start of the requested window.
            history (list[Transaction]): The transactions from the history.

        Returns:
            list[Transaction]: The transactions the history didn't have.
        """
        history_start = min(t.completed_date for t in history)
        latest = history_start if self.latest_date is None else min(history_start, self.latest_date)
        orders = self._orders_placed_between(start - timedelta(days=ORDER_LEAD_DAYS), latest)
        if not orders:
            return []

        rprint(
            f"[yellow]Amazon's transaction history only goes back to {history_start}. "
            f"Looking up {len(orders)} older orders one at a time...[/]"
        )
        already_found = Counter(
            _transaction_key(t) for t in history if t.completed_date == history_start
        )
        found: list[Transaction] = []
        failed = 0
        for order in orders:
            try:
                order_transactions = amazon_transactions.get_transactions(
                    order_id=order.order_number
                )
            except AmazonOrdersError as e:
                logger.warning(
                    f"Couldn't look up the transactions for order {order.order_number}: {e}"
                )
                failed += 1
                continue

            for transaction in order_transactions:
                if transaction.completed_date > history_start:
                    continue
                key = _transaction_key(transaction)
                if already_found[key]:
                    already_found[key] -= 1
                    continue
                found.append(transaction)

        if failed:
            rprint(
                f"[yellow]Couldn't look up {failed} of the older orders; their transactions won't be matched.[/]"
            )

        return found

    def _orders_placed_between(self, earliest: date, latest: date) -> list[Order]:
        """Returns the orders placed from `earliest` through `latest`, fetching any years not fetched yet."""
        return [
            order
            for year in range(earliest.year, latest.year + 1)
            for order in self._orders_for_year(str(year))
            if earliest <= order.order_placed_date <= latest
        ]

    def get_order_items(self, order_number: str) -> list[Item]:
        """Fetches an order's details page to get its items with prices, which the order history doesn't include.

        Args:
            order_number (str): The Amazon order number.

        Returns:
            list[Item]: The order's items.
        """
        return AmazonOrders(self._session()).get_order(order_number).items

    def _session(self) -> AmazonSession:
        """Returns a logged-in Amazon session, logging in on first use.

        Raises:
            AmazonLoginError: If logging in doesn't produce an authenticated session.
        """
        if "session" in self._memo:
            return self._memo["session"]

        amazon_session = self.amazon_config.amazon_session()
        amazon_session.login()

        if not amazon_session.is_authenticated:
            raise AmazonLoginError("Couldn't log in to Amazon: the session isn't authenticated.")

        self._memo["session"] = amazon_session
        return self._memo["session"]

    @classmethod
    def _normalized_years(cls, years: list[str] | None = None) -> list[str]:
        if years is None:
            return [str(date.today().year)]

        result: list[str] = []

        for year in years:
            if len(year) == 2:
                result.append("20" + year)
            elif len(year) == 4:
                result.append(year)
            else:
                raise ValueError("Year must be specified as 2 or 4 digits (e.g. 21 or 2021)")

        return result


def _transaction_key(transaction: Transaction) -> tuple[str | None, date, Decimal | float]:
    """Identifies a transaction, to tell whether the history and an order lookup both returned it."""
    return (transaction.order_number, transaction.completed_date, transaction.grand_total)


def print_amazon_transactions(
    amazon_transaction_with_order_details: list[AmazonTransactionWithOrderInfo],
):
    """Prints a list of transactions to the screen for inspection.

    Args:
        amazon_transaction_with_order_details (list[TransactionWithOrderInfo]): a list of transactions to print
    """
    rprint(f"found {len(amazon_transaction_with_order_details)} transactions")
    table = Table(title="Amazon Transactions")
    table.add_column("Completed Date", justify="center")
    table.add_column("Transaction Total", justify="right")
    table.add_column("Order Total", justify="right")
    table.add_column("Order Number", justify="center")
    table.add_column("Order Link", justify="center")
    table.add_column("Item Names", justify="left")

    for transaction in amazon_transaction_with_order_details:
        table.add_row(
            str(transaction.completed_date),
            f"${transaction.transaction_total:.2f}",
            f"${transaction.order_total:.2f}",
            transaction.order_number,
            str(transaction.order_link),
            " | ".join(_truncate_title(item.title) for item in transaction.items),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportAttributeAccessIssue]
        )

    rprint(table)


def _truncate_title(title: str, max_length: int = 20) -> str:
    """Truncates the title to a maximum length."""
    if len(title) > max_length:
        return title[: max_length - 3] + "..."
    return title


def find_amazon_transaction(
    amazon_trans: list[AmazonTransactionWithOrderInfo],
    amount: Union[float, Decimal],
    on_date: Union[date, None] = None,
) -> Union[AmazonTransactionWithOrderInfo, None]:
    """Find the Amazon transaction that matches a YNAB transaction's amount.

    When several Amazon transactions have the same amount, one completed on `on_date` is preferred.
    Otherwise the first one with that amount is returned.

    Args:
        amazon_trans (list[TransactionWithOrderInfo]): A list of Amazon transactions
        amount (float | Decimal): The YNAB transaction amount (negative for outflows)
        on_date (date | None): The YNAB transaction date

    Returns:
        TransactionWithOrderInfo | None: The matching Amazon transaction, or None if no match
    """
    amount = Decimal(amount)
    matches = [a_tran for a_tran in amazon_trans if a_tran.transaction_total == -amount]
    if not matches:
        return None

    for a_tran in matches:
        if a_tran.completed_date == on_date:
            return a_tran

    return matches[0]


# if __name__ == "__main__":
# print_amazon_transactions(AmazonTransactionRetriever.new()
