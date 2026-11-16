from typing import Optional


class StockTrimError(Exception):
    """Base class: every StockTrim failure is a subclass, so callers can never mistake one for success."""

    partial_results: Optional[list] = None  # set when a fatal error aborted a bulk run: every BulkItem so far


class StockTrimConfigError(StockTrimError):
    """A StockTrim call was attempted without credentials."""


class StockTrimRejected(StockTrimError):
    """StockTrim answered with a non-retryable 4xx. Never retried; the caller decides what to do."""

    def __init__(self, status: int, body: str, identifier: Optional[str] = None):
        self.status, self.body, self.identifier = status, body, identifier
        super().__init__(f"StockTrim rejected request ({status}){f' for {identifier!r}' if identifier else ''}: {body[:300]}")


class StockTrimAuthError(StockTrimRejected):
    """401/403: bad or missing credentials. Never retried; fatal for a bulk run."""


class StockTrimUnavailable(StockTrimError):
    """Transport error / 429 / 5xx on every attempt (one attempt for non-idempotent calls), or the per-call time cap hit."""

    def __init__(self, message: str, identifier: Optional[str] = None):
        self.identifier = identifier
        super().__init__(message)


class StockTrimAborted(StockTrimError):
    """A bulk item that was not attempted because an earlier call failed fatally."""


class StockTrimBadResponse(StockTrimError):
    """A 2xx answer that is not the JSON we expect (empty body, HTML from a proxy, schema drift)."""

    def __init__(self, status: int, content_type: str, body: str, identifier: Optional[str] = None, summary: str = ""):
        self.status, self.content_type, self.body, self.identifier = status, content_type, body[:300], identifier
        # JSON bodies may hold client data: the message carries only length + validation locations/types. The raw text stays on `.body`.
        # Non-JSON bodies (an HTML proxy page) are shown (first 300 chars) because they are what an operator needs.
        detail = f"{len(body)} chars; {summary}" if "json" in content_type.lower() else self.body
        super().__init__(f"StockTrim returned an unparseable response ({status}, {content_type or 'no content-type'})"
                         f"{f' for {identifier!r}' if identifier else ''}: {detail}")


class StockTrimBulkError(StockTrimError):
    """A bulk run had failures; `.failures` holds the failed BulkItems (item + original exception)."""

    def __init__(self, failures: list, total: int):
        self.failures = failures
        super().__init__(f"{len(failures)} of {total} StockTrim calls failed; first: {type(failures[0].error).__name__}")
