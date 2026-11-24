"""One definition of how a `title_include` / `title_exclude` keyword matches a title.

Ported from career-ops `title-keywords.mjs` (MIT, github.com/career-ops-hq/career-ops).
It lives in its own module for the same reason the original does: more than one
path filters titles, and a second copy of this logic is how the two drift apart.

Three matching modes, chosen per keyword:

    agent         Agentforce yes   Agents yes   Reagents yes    <- the default
    stem:agent    Agentforce yes   Agents yes   Reagents NO     <- must START a word
    word:agent    Agentforce NO    Agents NO    Reagents NO     <- the whole word

A plain substring is not "the loose option"; it is two loosenesses at once, and
usually only one of them was meant. The prefixes let one entry ask for the half
it means. A 2-3 letter all-letter keyword ("ai", "ml", "sde") is anchored
automatically, because such a run inside a longer word is always an accident
("COO" in "Coordinator", "sde" in "sdet").

`" + "` (with surrounding whitespace) between terms means EVERY term must appear,
in any order -- one entry instead of the whole {level} x {separator} x {domain
word} matrix. It is a POSITIVE-side feature only: on the negative side an entry
is a veto, and "reject when both appear" is easier to write as two entries.
The separator requires the spaces on purpose, so the ordinary keyword "C++" is
not split into "c", which would match almost every title.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Callable, Optional, Sequence

Matcher = Callable[[str], bool]

WORD_PREFIX = "word:"
STEM_PREFIX = "stem:"

# Han, Hiragana, Katakana and Hangul are letters, but those scripts glue a Latin
# acronym straight onto the next word ("AI全栈工程师", "AIエンジニア", "AI엔지니어" --
# all "AI engineer"), so a CJK character next to a keyword is a word EDGE, not a
# continuation: "ai" must match them exactly as it matches "AI Engineer".
# Hangul Jamo (1100-11FF) is listed because fold_accents() runs NFD, which
# decomposes precomposed Hangul syllables into Jamo before any matching happens.
_CJK = (
    "⺀-⻿々〇〡-〩〸-〻㐀-䶿"
    "一-鿿豈-﫿"                              # Han
    "\U00020000-\U0002a6df\U0002a700-\U0002ebef\U0002f800-\U0002fa1f"
    "ぁ-ゖゝ-ゟ"                              # Hiragana
    "ァ-ヿㇰ-ㇿｦ-ﾝ"                 # Katakana
    "ᄀ-ᇿ㄰-㆏ꥠ-꥿"                 # Hangul Jamo
    "가-힣ힰ-퟿ﾠ-ￜ"                 # Hangul syllables
)

# One definition of "inside a word", used by both anchored branches. Explicit
# lookarounds rather than \b, because \b's meaning depends on the characters at
# the keyword's own edges: for `word:c++` a trailing \b would sit after "+" and
# assert the opposite of the intent.
#
# Python's \w is [letters, digits, underscore] and, unlike the original's
# \p{L}\p{M}\p{N}_, does NOT include combining marks. fold_accents() strips the
# Latin combining range (U+0300-U+036F) from both sides before matching, so the
# motivating case -- a decomposed "é" splitting a word -- is covered; a mark
# outside that range still would.
_WORD_CHAR = rf"(?:(?![{_CJK}])\w)"


def _anchored(body: str) -> Matcher:
    """Whole word: a boundary on both sides."""
    rx = re.compile(rf"(?<!{_WORD_CHAR}){body}(?!{_WORD_CHAR})")
    return lambda folded: rx.search(folded) is not None


def _stem(body: str) -> Matcher:
    """Same left boundary, no right one: the keyword must START a word, and the
    word may continue past it."""
    rx = re.compile(rf"(?<!{_WORD_CHAR}){body}")
    return lambda folded: rx.search(folded) is not None


def fold_accents(s: object) -> str:
    """Drop Latin diacritics so a keyword and a title compare equal without them.

    Boards publish titles in uppercase without accents ("TECNICO CONTROL DE
    PRODUCCION") while a keyword list is written with them; lowercasing alone
    never matches those, and every such posting is silently counted as filtered.
    Both sides are folded, so the comparison stays symmetric.
    """
    return "".join(c for c in unicodedata.normalize("NFD", str(s if s is not None else ""))
                   if not ("̀" <= c <= "ͯ"))


def fold_title(s: object) -> str:
    """Lowercase, fold accents, collapse whitespace runs.

    Boards ship titles with no-break and doubled spaces ("Product Manager"),
    and a multi-word keyword is a substring test, so without the collapse the
    keyword "product manager" simply misses.
    """
    return re.sub(r"\s+", " ", fold_accents(str(s if s is not None else "").lower()))


def _compile_prefixed(kw: str) -> Optional[Matcher]:
    """A matcher when `kw` carries `word:` or `stem:`, else None for the caller's
    own rule."""
    for prefix, build in ((WORD_PREFIX, _anchored), (STEM_PREFIX, _stem)):
        if kw.startswith(prefix):
            bare = kw[len(prefix):].strip()
            # A bare prefix is a config typo. Matching NOTHING is the safe
            # reading: as a positive it contributes no match, while an empty
            # pattern matching everything would veto an entire scan from one
            # stray colon -- a silent drop of one entry beats a silent flood.
            if not bare:
                return lambda folded: False
            return build(re.escape(bare))
    return None


def compile_keyword(kw: str) -> Matcher:
    """Compile one already-folded, trimmed keyword.

    A `word:`/`stem:` prefix wins. Otherwise a 2-3 letter all-letter keyword is
    anchored (an acronym inside a longer word is always an accident), and
    everything else -- multi-word phrases, and anything with a non-letter like
    ".NET", "sr ", "C++" -- keeps permissive substring matching.
    """
    prefixed = _compile_prefixed(kw)
    if prefixed is not None:
        return prefixed
    if re.fullmatch(r"[a-z]{2,3}", kw):
        return _anchored(re.escape(kw))
    return lambda folded: kw in folded


_AND_SEPARATOR = re.compile(r"\s+\+\s+")


def compile_positive_keyword(kw: str) -> Matcher:
    """compile_keyword, plus `" + "` AND-groups. Each term keeps compile_keyword's
    own rule, so a short term like "ml" is still anchored inside the group."""
    if not _AND_SEPARATOR.search(kw):
        return compile_keyword(kw)
    terms = [t for t in (t.strip() for t in _AND_SEPARATOR.split(kw)) if t]
    if not terms:
        return compile_keyword(kw)
    matchers = [compile_keyword(t) for t in terms]
    return lambda folded: all(m(folded) for m in matchers)


def compile_list(keywords: Sequence[object], positive: bool = False) -> tuple[tuple[str, Matcher], ...]:
    """Compile a keyword list into (raw entry, matcher) pairs.

    The raw entry is returned as written in the config -- prefix included -- so a
    rejection reason names something the user can find and edit.

    Normalized defensively: a non-string entry (a null or a number in the config)
    must not crash a scan, and an entry that folds to nothing is dropped, because
    an empty keyword would match every title via plain substring.
    """
    compile_one = compile_positive_keyword if positive else compile_keyword
    out: list[tuple[str, Matcher]] = []
    for raw in keywords or ():
        if not isinstance(raw, str):
            continue
        folded = fold_title(raw).strip()
        if not folded:
            continue
        out.append((raw, compile_one(folded)))
    return tuple(out)


def first_match(compiled: Sequence[tuple[str, Matcher]], title: object) -> Optional[str]:
    """The raw entry of the first keyword that matches, or None."""
    folded = fold_title(title)
    return next((raw for raw, match in compiled if match(folded)), None)


def any_match(compiled: Sequence[tuple[str, Matcher]], title: object) -> bool:
    folded = fold_title(title)
    return any(match(folded) for _, match in compiled)
