"""Transport deliverability: refuse a reply the caller provably cannot receive.

``Config.max_resource_response_bytes`` is a property of the *transport*, not
of any one file. The OpenAI tunnel control plane rejects a response body over
10 MiB with HTTP 413, and that rejection kills the stdio child — so an
oversized reply does not degrade one call, it takes the connection down and
every later call with it.

The rule everywhere in this module is **refuse, never truncate**. A partial
original breaks ``resources/read``'s contract; a partial document breaks the
hash-guarded edit flow, which needs ``document_sha256`` to describe the whole
file; and ``get_context`` must never silently drop a rule (spec-mcp §4). A
caller that gets an error here is told which contributor was too large and
what bounded call to make instead, which is recoverable. A dead tunnel is not.

Refusing to *serve* is also not refusing to *index*. Nothing in this module is
reachable from the indexer: a workspace that already contains an oversized
document stays searchable and editable through the bounded read surfaces.
"""

from __future__ import annotations

from typing import Final

from ferumind.core.errors import FileTooLargeError, ResponseTooLargeError
from ferumind.core.types import JsonObject

#: Base64 inflates a blob by 4/3. A resource whose encoded form would exceed
#: the transport ceiling is refused rather than truncated.
_BASE64_INFLATION_NUMERATOR: Final = 4
_BASE64_INFLATION_DENOMINATOR: Final = 3

#: Text is charged at its UTF-8 size plus 1/16 for the JSON envelope it rides
#: in. Escaping ordinary Markdown is dominated by newlines, each one byte
#: becoming two, so a shade over 6% is a deliberately generous allowance for
#: that plus the surrounding field names and structure. It exists so the
#: guard trips slightly before the transport does rather than slightly after;
#: the point of an early refusal is lost if the estimate can run under.
_JSON_OVERHEAD_DIVISOR: Final = 16


def charged_text_bytes(measured_bytes: int) -> int:
    """Charge text at its UTF-8 size plus the JSON envelope allowance."""
    return measured_bytes + measured_bytes // _JSON_OVERHEAD_DIVISOR


def assert_blob_is_deliverable(size_bytes: int, max_response_bytes: int | None) -> None:
    """Refuse an original that the caller's transport provably cannot carry.

    Checked against the stat size *before* the file is read, so an oversized
    resource costs nothing to reject and never has to be held in memory.

    This is a hard refusal on purpose. ``resources/read`` promises the exact
    original, so silently truncating would break that contract; and emitting
    an oversized reply is worse than useless, because a transport that
    rejects the response body can tear down the connection carrying it and
    leave the server unreachable for every later call.
    """
    if max_response_bytes is None:
        return
    encoded_estimate = (size_bytes * _BASE64_INFLATION_NUMERATOR) // _BASE64_INFLATION_DENOMINATOR
    if encoded_estimate <= max_response_bytes:
        return
    usable = (max_response_bytes * _BASE64_INFLATION_DENOMINATOR) // _BASE64_INFLATION_NUMERATOR
    raise FileTooLargeError(
        "This file is too large to return as a resource. Its base64-encoded "
        f"form would be about {encoded_estimate} bytes, over the "
        f"{max_response_bytes}-byte transport limit. Use read_file for a "
        "bounded rendition or text slice of the same file; that is the "
        "supported way to get this content into context.",
        details={
            "size_bytes": size_bytes,
            "encoded_estimate_bytes": encoded_estimate,
            "max_response_bytes": max_response_bytes,
            "max_original_bytes": usable,
            "recommended_tool": "read_file",
            "recommended_action": (
                "Call read_file with this path for an image rendition or a "
                "bounded text slice. The original stays on disk untouched."
            ),
        },
    )


class ResponseBudget:
    """A transport budget spent across the parts of one assembled response.

    Each contributor is charged *before* its bytes are read wherever the size
    is knowable from a stat or from stored metadata, so the common oversized
    case is refused without ever holding the content in memory.

    Two ways to spend it, because the two shapes of result want different
    failures. :meth:`charge` raises, for results that are all-or-nothing —
    a document, a context payload. :meth:`try_charge` returns ``False``, for
    results that already carry per-component omission flags and stay useful
    with a part left out, like a snapshot.

    A ``limit_bytes`` of ``None`` means no known transport ceiling, and every
    charge succeeds. That is the correct default for in-process callers (the
    CLI, the dashboard, tests) which do not go over a relay at all.
    """

    def __init__(self, limit_bytes: int | None, *, surface: str) -> None:
        self._limit_bytes = limit_bytes
        self._surface = surface
        self._used_bytes = 0

    @property
    def used_bytes(self) -> int:
        """Charged bytes so far, JSON allowance included."""
        return self._used_bytes

    @property
    def remaining_bytes(self) -> int | None:
        """Headroom left, or ``None`` when there is no ceiling."""
        if self._limit_bytes is None:
            return None
        return max(0, self._limit_bytes - self._used_bytes)

    def try_charge(self, measured_bytes: int) -> bool:
        """Charge *measured_bytes* if they fit, reporting whether they did."""
        charged = charged_text_bytes(measured_bytes)
        if self._limit_bytes is not None and self._used_bytes + charged > self._limit_bytes:
            return False
        self._used_bytes += charged
        return True

    def charge(self, measured_bytes: int, *, source: str, remedy: str) -> None:
        """Charge *measured_bytes*, or refuse the whole response.

        *source* names the contributor that crossed the line — a path, or a
        description of an aggregate — and *remedy* is the bounded call or
        operator action that resolves it. Both reach the caller in the error's
        structured details, because an agent that cannot see which file broke
        the budget has no way to route around it.
        """
        limit_bytes = self._limit_bytes
        if limit_bytes is None or self.try_charge(measured_bytes):
            return
        overshoot = self._used_bytes + charged_text_bytes(measured_bytes)
        details: JsonObject = {
            "surface": self._surface,
            "source": source,
            "source_bytes": measured_bytes,
            "response_bytes_so_far": self._used_bytes,
            "estimated_response_bytes": overshoot,
            "max_response_bytes": limit_bytes,
            "recommended_action": remedy,
        }
        raise ResponseTooLargeError(
            f"This {self._surface} result cannot be delivered: {source} would "
            f"bring it to about {overshoot} bytes, over the {limit_bytes}-byte "
            f"limit the caller's transport will carry. {remedy}",
            details=details,
        )
