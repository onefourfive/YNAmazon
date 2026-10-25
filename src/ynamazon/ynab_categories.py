"""Functions for retrieving the YNAB categories that transactions can be assigned to."""

from typing import TYPE_CHECKING, Union

from ynab.api.categories_api import CategoriesApi
from ynab.api_client import ApiClient
from ynab.configuration import Configuration

if TYPE_CHECKING:
    from ynab.models.category import Category
    from ynab.models.hybrid_transaction import HybridTransaction

# Category groups YNAB manages itself; transactions shouldn't be assigned to these
EXCLUDED_CATEGORY_GROUPS = {"Internal Master Category", "Credit Card Payments", "Hidden Categories"}


def get_budget_categories(configuration: Configuration, budget_id: str) -> list["Category"]:
    """Returns the visible categories in a budget that transactions can be assigned to.

    Args:
        configuration (Configuration): The YNAB API configuration.
        budget_id (str): The budget ID.

    Returns:
        list[Category]: Categories that aren't hidden, deleted, or managed by YNAB.
    """
    with ApiClient(configuration=configuration) as api_client:
        response = CategoriesApi(api_client).get_categories(budget_id=budget_id)

    categories: list[Category] = []
    for group in response.data.category_groups:
        if group.hidden or group.deleted or group.name in EXCLUDED_CATEGORY_GROUPS:
            continue
        for category in group.categories:
            if category.hidden or category.deleted:
                continue
            if category.category_group_name is None:
                category.category_group_name = group.name
            categories.append(category)

    return categories


def is_uncategorized(transaction: Union["HybridTransaction", None]) -> bool:
    """Returns True if a transaction has no category (or YNAB's 'Uncategorized' placeholder).

    Split transactions report a category name of 'Split' and count as categorized.
    """
    if transaction is None:
        return False
    return transaction.category_id is None or transaction.category_name == "Uncategorized"
