"""Whether an attempt still needs making, and how an error should be remembered.

The numbers this module works with come from a real scan rather than a guess. Throwing
one scene at every installed fragment scraper produced, on a live library:

    21 MATCH   64 NO_MATCH   101 ERROR   5 TIMEOUT

Over half of the attempts errored, and almost all of those errors were a scraper
failing at a scene it has nothing to do with: a query URL built out of nothing and
fetched ("http error 404 ... /watch//"), or a script exiting non-zero. Those failures
are properties of the pairing, not of the moment - they will fail identically tomorrow.
Cached for a day, as a transient error should be, they would be re-attempted on every
scan forever, which is a hundred pointless external requests per scene per day.

So an error is classified when it is stored: `permanent` is cached like a no-match,
`transient` is retried soon. Both stay ERROR in the attempt row, because the
distinction is a caching decision and should not blur what actually happened.
"""

from __future__ import annotations

import re

PERMANENT = "permanent"
TRANSIENT = "transient"

# Substrings that mean "this scraper cannot work from this scene", checked against the
# lowercased message. Conservative on purpose: anything not recognised is treated as
# transient, so a misclassification costs a retry rather than a permanently missed
# match.
_PERMANENT_PATTERNS = (
    "http error 404",
    "http error 400",
    "http error 401",
    "http error 403",
    "http error 410",
    "scraper script error: exit status",  # a script that refused the input
    "cannot use scraper",                # capability mismatch reported by Stash
    "not supported",
    "no such scraper",
    "invalid memory address or nil pointer dereference",  # the scraper is broken
    "index out of range",
    "unmarshal",                         # the site returned something unparseable
    "no query url",
    "query url is empty",
)

# Substrings that are clearly momentary and should be retried soon, even if one of the
# patterns above also matches somewhere in a long message.
_TRANSIENT_PATTERNS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "temporary failure",
    "no such host",
    "eof",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "context canceled",
    "tls",
)

_WHITESPACE = re.compile(r"\s+")


def classify_error(message) -> str:
    """`permanent` or `transient` for one error message."""
    text = _WHITESPACE.sub(" ", str(message or "")).strip().lower()
    if not text:
        return TRANSIENT
    for pattern in _TRANSIENT_PATTERNS:
        if pattern in text:
            return TRANSIENT
    for pattern in _PERMANENT_PATTERNS:
        if pattern in text:
            return PERMANENT
    return TRANSIENT


def error_signature(message) -> str:
    """One error message reduced to the shape of the failure.

    Raw messages embed the URL that was fetched and the scraper's own name, so grouping
    by the exact text turns one recurring failure into a hundred singletons - useless
    for answering "what is wrong with this scraper?". These three, from the same run,
    are one problem:

        scraper YouPorn: failed to load URL "https://www.youporn.com/watch//": 404
        scraper Xvideos: failed to load URL "https://www.xvideos.com/video./x": 404
        scraper xhamster: failed to load URL "https://xhamster.com/videos/": 404

    The prefix naming the scraper goes, quoted values become a placeholder, and runs of
    digits collapse, leaving the sentence that describes the fault.
    """
    text = _WHITESPACE.sub(" ", str(message or "")).strip()
    if not text:
        return "(no message)"

    # "scraper Foo: ..." and "error while ... with scraper Foo: ..." both just say who.
    text = re.sub(r"^scraper\s+\S+?:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^error while [a-z ]*scraping with scraper\s+\S+?:\s*", "", text,
                  flags=re.IGNORECASE)
    # Quoted URLs and bare URLs are the variable part of the same complaint.
    text = re.sub(r'"[^"]*"', '"..."', text)
    text = re.sub(r"https?://\S+", "<url>", text)
    text = re.sub(r"\d+", "#", text)
    text = _WHITESPACE.sub(" ", text).strip(" .:")
    return text[:180] or "(no message)"


class Policy:
    """How long a stored attempt stays usable, given the configuration."""

    def __init__(self, config, scraper_id=""):
        self.config = config
        self.scraper_id = scraper_id or ""

    def days_for(self, status, error_kind=None):
        """TTL in days for an attempt with this status. 0 means "do not reuse"."""
        if status == "ERROR":
            if error_kind == PERMANENT:
                # Remembered as long as a no-match: it is one, in every way that
                # matters to whether the attempt is worth making again.
                return self.config.cache_days(self.scraper_id, "NO_MATCH")
            return self.config.cache_days(self.scraper_id, "ERROR")
        return self.config.cache_days(self.scraper_id, status)

    def as_callable(self):
        """A `ttl_days_for(status, error_kind)` for the repository layer."""
        return self.days_for
