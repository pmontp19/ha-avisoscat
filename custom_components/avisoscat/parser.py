"""Extraction of the inline SMP payload `meteo.cat` embeds in its own pages.

This module's whole job is getting JSON out of an HTML page: it locates the
`Meteocat.avisosSMP({...})` call the server renders inline and lifts the `avisos`
and `episodisPreavisos` arrays out of it (docs/01-data-sources.md §3.2,
docs/04-architecture.md §3). Turning that JSON into objects is `models.py`'s job
and fetching the page is `smp.py`'s, so there is no Home Assistant import, no
network and no data model here.

The page is a third party's markup that can change without notice, which makes
this the highest-risk module of the integration. Three failure modes are known
and each one has a dedicated test in `tests/test_parser.py`:

1. **No greedy regular expression.** The payload carries `[`, `]` and `{` inside
   string values such as `comentari`, so the end of an array is found with a
   bracket counter that knows when it is inside a string. A regex would stop at
   the first `]` that appears in prose.
2. **The call carries decoys.** Keys are read only at the top level of the call's
   argument object, so neither the `avisos` key of the nested `opcions` object
   nor the `"avisos"` key inside every episode of the payload can be mistaken for
   the real one. On top of that, a page may render the call more than once: the
   homepage renders a 1-day visor and a 3-day widget, so the candidates are
   compared and the richest one wins
   (docs/captures/smp-page-choice-2026-08-06.md).
3. **No open episode is normal.** A quiet day renders `avisos: [[]]` or `[]`;
   that is two empty lists, not an error. Only markup we cannot read at all
   raises `SmpParseError`, which the coordinator counts towards its degradation
   threshold (docs/04-architecture.md §8).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Final

_LOGGER = logging.getLogger(__name__)

__all__ = ["SmpParseError", "extract_smp_payload"]

# The call the server renders inline. Matched literally, not by regex: it is a
# fixed JavaScript identifier, and a literal `find` says so more plainly.
_CALL_MARKER: Final = "Meteocat.avisosSMP("

# Key of the open episodes, and key of the pre-warnings. The optional quotes
# accept a payload that ever starts quoting its JavaScript keys; nested keys are
# excluded by depth, not by quoting. `(?<![\w$])` is what stops `avisos` from
# matching the tail of `episodisPreavisos`, which does contain that substring.
_KEY_AVISOS: Final = re.compile(r"""(?<![\w$])["']?avisos["']?\s*:\s*""")
_KEY_PREAVISOS: Final = re.compile(r"""(?<![\w$])["']?episodisPreavisos["']?\s*:\s*""")

_OPENERS: Final = "[{("
_CLOSERS: Final = "]})"
_QUOTES: Final = "\"'`"

# Depth of a key that sits directly inside the call's argument object, counted
# from the `(` of the call: the `(` itself opens depth 1 and the `{` of the
# argument object opens depth 2. Anything deeper belongs to a nested object such
# as `opcions`, or to the JSON payload itself.
_ARGUMENT_DEPTH: Final = 2


class SmpParseError(Exception):
    """The page could not be read as an SMP payload at all.

    Raised only when the markup itself is unusable: the call is missing, its
    episode key is gone, or that key's value will not decode. A page that simply
    has no warning open is not an error (see `extract_smp_payload`).
    """


def _scan_balanced(text: str, start: int) -> int | None:
    """Return the index just past the bracket group opening at `start`.

    A plain depth counter over `[](){}`, with the one addition that is the point
    of the function: characters inside a quoted string are skipped, so the `]` in
    a `comentari` such as "ratxes de vent [rafegues]" does not close the array.
    Both JavaScript quote styles are honoured and a backslash escapes the next
    character.

    `None` means the group never closed before the end of the text, i.e. a
    truncated page. The caller decides what to do about that; guessing where the
    array ended would silently invent data.
    """
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in _QUOTES:
            quote = char
        elif char in _OPENERS:
            depth += 1
        elif char in _CLOSERS:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _depth_at(text: str, start: int, index: int) -> int | None:
    """Bracket depth of `index`, counted from `start`; `None` if inside a string.

    This is what makes key lookup structural instead of textual: the payload
    contains an `"avisos"` key on every single episode and the `opcions` object
    has one too, and both live deeper than the key we want. `None` for a position
    inside a string value means a `comentari` that happens to read
    "avisos: [...]" cannot be mistaken for a key either.
    """
    depth = 0
    quote: str | None = None
    escaped = False
    for position in range(start, index):
        char = text[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in _QUOTES:
            quote = char
        elif char in _OPENERS:
            depth += 1
        elif char in _CLOSERS:
            depth -= 1
    return None if quote is not None else depth


def _call_spans(html: str) -> list[tuple[int, int]]:
    """Locate the argument list of every `Meteocat.avisosSMP(` call in the page.

    Every call is returned, not just the first: `https://www.meteo.cat/` renders
    the call twice (a 1-day front-page visor and a 3-day widget) and the two do
    not carry the same episodes. Each span starts at the `(` of the call, which is
    the origin the depth check counts from.
    """
    spans: list[tuple[int, int]] = []
    search_from = 0
    while (found := html.find(_CALL_MARKER, search_from)) != -1:
        # The marker ends on the `(` itself, which is where the scan must start.
        open_paren = found + len(_CALL_MARKER) - 1
        end = _scan_balanced(html, open_paren)
        if end is None:
            # A truncated final call is still scanned to the end of the page: the
            # keys we want come early in the argument list, so a cut-off tail
            # often still yields them.
            _LOGGER.warning(
                "The Meteocat.avisosSMP( call at offset %d is never closed; "
                "reading it up to the end of the page",
                found,
            )
            spans.append((open_paren, len(html)))
            break
        spans.append((open_paren, end))
        search_from = end
    return spans


def _decode_arrays(
    html: str, spans: list[tuple[int, int]], key: re.Pattern[str], name: str
) -> tuple[list[list[Any]], bool]:
    """Decode the array held by `key` at the top level of every call.

    Returns the decoded arrays plus whether the key was found at all. Those are
    two different failures: a key that is absent, or present but undecodable,
    means the markup changed; a key that decodes to an empty list just means the
    sky is quiet.
    """
    decoded: list[list[Any]] = []
    found_any = False
    for start, end in spans:
        for match in key.finditer(html, start, end):
            if _depth_at(html, start, match.start()) != _ARGUMENT_DEPTH:
                # A nested `avisos` key: the `opcions` decoy, or the one every
                # episode of the payload carries.
                continue
            found_any = True
            value_at = match.end()
            if value_at >= len(html) or html[value_at] != "[":
                _LOGGER.warning(
                    "The SMP `%s` key at offset %d does not hold an array",
                    name,
                    match.start(),
                )
                continue
            closes_at = _scan_balanced(html, value_at)
            if closes_at is None:
                _LOGGER.warning(
                    "The SMP `%s` array at offset %d is never closed, skipping it",
                    name,
                    value_at,
                )
                continue
            try:
                value = json.loads(html[value_at:closes_at])
            except ValueError as err:
                # Not fatal on its own: another copy of the call in the same page
                # may still carry a readable copy of the same data.
                _LOGGER.warning(
                    "The SMP `%s` array at offset %d is not valid JSON: %s",
                    name,
                    value_at,
                    err,
                )
                continue
            # A balanced slice that starts with `[` and decodes at all decodes to
            # a list, so there is no shape left to check here.
            decoded.append(value)
    return decoded, found_any


def _content_size(value: list[Any]) -> int:
    """Count the entries an array carries, seeing through one level of nesting.

    The episodes arrive wrapped one level deeper, one sub-array per forecast day
    (`[[day1…], [day2…], [day3…]]`), so `[[]]` is as empty as `[]` and a 3-day
    copy of the payload counts as bigger than a 1-day one.
    """
    return sum(
        len(entry) if isinstance(entry, list) else 1
        for entry in value
        if entry is not None
    )


def _pick_richest(candidates: list[list[Any]]) -> list[Any]:
    """Pick the candidate carrying the most entries; ties go to the first.

    Richest, not first non-empty: the homepage renders the call twice and its
    first copy holds only today's episodes, so anchoring on the first non-empty
    array silently drops tomorrow's warnings. Measured on 2026-08-06 with an
    episode open, the richest candidate of both candidate pages is the very same
    payload (docs/captures/smp-page-choice-2026-08-06.md).

    A candidate with nothing in it collapses to `[]`, so that a quiet page is
    falsy for the caller: `smp.py` decides whether to try the fallback page by
    asking whether the extraction produced episodes, and `[[]]` would answer yes.
    """
    richest = max(candidates, key=_content_size, default=[])
    return richest if _content_size(richest) else []


def extract_smp_payload(html: str) -> tuple[list[Any], list[Any]]:
    """Extract `(avisos, episodisPreavisos)` from a `meteo.cat` page.

    Both lists are returned exactly as the page encoded them, ready for
    `models.parse_snapshot()`, which is what normalises the nesting and the
    float-shaped numbers.

    A page with no open episode returns two empty lists, whether the page encoded
    that as `[]` or as `[[]]`: a quiet day is the normal state, not a failure, and
    the caller gets one falsy answer for it instead of two shapes to check.
    `SmpParseError` is reserved for markup we
    cannot read: no `Meteocat.avisosSMP(` call, no `avisos` key in it, or a value
    that will not decode. Pre-warnings are treated as secondary: a missing
    `episodisPreavisos` key degrades to an empty list with a warning, because it
    would be a poor reason to discard warnings that are in force right now.
    """
    if not isinstance(html, str) or _CALL_MARKER not in html:
        raise SmpParseError(
            f"No {_CALL_MARKER}...) call in the page: the meteo.cat markup "
            "changed or the response is not an SMP page"
        )

    spans = _call_spans(html)
    avisos_candidates, avisos_found = _decode_arrays(html, spans, _KEY_AVISOS, "avisos")
    if not avisos_found:
        raise SmpParseError(
            "The Meteocat.avisosSMP( call carries no top-level `avisos` key: "
            "the meteo.cat payload shape changed"
        )
    if not avisos_candidates:
        raise SmpParseError(
            "The `avisos` key of the Meteocat.avisosSMP( call could not be "
            "decoded as JSON: the meteo.cat payload shape changed"
        )

    preavisos_candidates, preavisos_found = _decode_arrays(
        html, spans, _KEY_PREAVISOS, "episodisPreavisos"
    )
    if not preavisos_found:
        _LOGGER.warning(
            "No top-level `episodisPreavisos` key in the Meteocat.avisosSMP( "
            "call; continuing without pre-warnings"
        )

    return (
        _pick_richest(avisos_candidates),
        _pick_richest(preavisos_candidates),
    )
