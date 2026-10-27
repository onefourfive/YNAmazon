"""Parsing and validation of the date range given with --since and --until."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

DATE_FORMAT = "%Y%m%d"

# When only --until is given, look back this many days (the same as the default Amazon transaction window)
DEFAULT_RANGE_DAYS = 31

# Ranges longer than this many months need to be confirmed, since they take a while to retrieve from Amazon
LARGE_RANGE_MONTHS = 6

# Amazon's completed date and YNAB's date for the same charge can differ by a few days
AMAZON_DATE_BUFFER_DAYS = 7

# An order can be charged a while after it's placed, so orders from a little before the range are retrieved too
ORDER_LEAD_DAYS = 30

# Amazon order history doesn't go back further than this
EARLIEST_DATE = date(1995, 1, 1)


class DateRangeError(ValueError):
    """Raised when --since or --until is invalid."""


def parse_date(value: str, option: str) -> date:
    """Parses a YYYYMMDD date given for a command-line option.

    Raises:
        DateRangeError: If the value isn't a valid YYYYMMDD date.
    """
    value = value.strip()
    if len(value) != 8 or not value.isdigit():
        raise DateRangeError(f"{option} must be a date in YYYYMMDD format, got '{value}'")
    try:
        return datetime.strptime(value, DATE_FORMAT).date()
    except ValueError as e:
        raise DateRangeError(f"{option} '{value}' isn't a valid date") from e


def _check_bounds(option: str, value: date, today: date) -> None:
    """Raises DateRangeError if a date is in the future or before EARLIEST_DATE."""
    if value > today:
        raise DateRangeError(f"{option} {value:%Y%m%d} is in the future")
    if value < EARLIEST_DATE:
        raise DateRangeError(f"{option} {value:%Y%m%d} is before {EARLIEST_DATE:%Y}")


def months_before(day: date, months: int) -> date:
    """Returns the date `months` calendar months before `day`, clamped to the end of shorter months."""
    year_offset, month_index = divmod(day.month - 1 - months, 12)
    year = day.year + year_offset
    month = month_index + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


@dataclass(frozen=True)
class DateRange:
    """An inclusive range of transaction dates to process."""

    since: date
    until: date

    @classmethod
    def from_options(
        cls, since: str | None, until: str | None, today: date | None = None
    ) -> "DateRange | None":
        """Builds a date range from the --since and --until options.

        An empty or missing --until means today. When only --until is given, the range starts
        `DEFAULT_RANGE_DAYS` before it.

        Returns:
            The date range, or None if neither option was given.

        Raises:
            DateRangeError: If a date is invalid, in the future, too early, or the range is reversed.
        """
        if since is None and until is None:
            return None

        today = today or date.today()
        until_date = parse_date(until, "--until") if until and until.strip() else today
        _check_bounds("--until", until_date, today)

        if since and since.strip():
            since_date = parse_date(since, "--since")
            _check_bounds("--since", since_date, today)
        else:
            since_date = max(until_date - timedelta(days=DEFAULT_RANGE_DAYS), EARLIEST_DATE)

        if since_date > until_date:
            raise DateRangeError(
                f"--since {since_date:%Y%m%d} is after --until {until_date:%Y%m%d}"
            )

        return cls(since=since_date, until=until_date)

    @property
    def days(self) -> int:
        """The number of days in the range, including both ends."""
        return (self.until - self.since).days + 1

    def is_large(self) -> bool:
        """Returns True if the range is longer than `LARGE_RANGE_MONTHS` months."""
        return self.since < months_before(self.until, LARGE_RANGE_MONTHS)

    def contains(self, day: date) -> bool:
        """Returns True if a YNAB transaction date is in the range."""
        return self.since <= day <= self.until

    def contains_amazon(self, day: date) -> bool:
        """Returns True if an Amazon transaction date could match a YNAB transaction in the range."""
        return (
            self.since - timedelta(days=AMAZON_DATE_BUFFER_DAYS) <= day <= self.latest_amazon_date()
        )

    def latest_amazon_date(self) -> date:
        """The latest Amazon transaction date that could match a YNAB transaction in the range."""
        return self.until + timedelta(days=AMAZON_DATE_BUFFER_DAYS)

    def amazon_transaction_days(self, today: date | None = None) -> int:
        """The number of days back from today to retrieve Amazon transactions for."""
        today = today or date.today()
        return (today - self.since).days + AMAZON_DATE_BUFFER_DAYS

    def amazon_order_years(self) -> list[str]:
        """The years to retrieve Amazon orders for."""
        first_year = (self.since - timedelta(days=ORDER_LEAD_DAYS)).year
        return [str(year) for year in range(first_year, self.until.year + 1)]
