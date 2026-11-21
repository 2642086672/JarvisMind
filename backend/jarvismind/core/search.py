"""Full-text search helpers for SQLite FTS5.

The stock FTS5 ``unicode61`` tokenizer treats a run of CJK characters as a
single token, so Chinese substring queries return nothing. This module
implements a replaceable segmentation adapter: CJK runs are expanded into
overlapping bigrams (plus unigrams) and ASCII words are kept whole. The
resulting token string is indexed in a dedicated FTS5 table, which gives
usable 1-, 2-, and 3+ character Chinese search and normal English word
search from one index.

The adapter is pure Python and has no third-party dependency, so it can be
swapped for a real tokenizer later without touching the repository layer.
"""
from __future__ import annotations

import re
from typing import List

# CJK Unified Ideographs, extensions, compatibility forms, and the CJK
# punctuation that frequently appears inside Chinese text.
_CJK_RUN = re.compile(
    r"[㐀-䶿一-鿿豈-﫿"
    r"︰-﹏＀-￯\U00020000-\U0002a6df\U0002a700-\U0002ebef]+"
)
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+")

#: Words that carry no retrieval signal in a question. Removing them turns
#: "我喜欢喝什么咖啡" into "咖啡", which FTS5 can match with a strict AND.
_QUERY_FILLERS = (
    "请问", "帮我", "麻烦", "告诉我", "你知道", "你还记得", "还记得",
    "我想知道", "我想问", "什么", "哪些", "哪个", "哪里", "怎么", "怎样",
    "为什么", "多少", "多久", "是不是", "是否", "吗", "呢", "吧",
    "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "的", "是", "了", "也", "都", "在", "有", "和", "就", "而", "及", "或",
    "喜欢", "想", "要", "会", "能", "可以", "请",
)


def strip_query_fillers(query: str) -> str:
    """Remove question/pronoun filler so the remaining keywords can be ANDed."""
    result = query or ""
    for word in sorted(_QUERY_FILLERS, key=len, reverse=True):
        result = result.replace(word, "")
    return result.strip()


def segment_for_index(text: str) -> str:
    """Build the token string stored in the FTS index for one document.

    ASCII word runs stay whole and are lowercased. Each CJK run becomes its
    unigrams and overlapping bigrams, so both short and long Chinese queries
    can match.
    """
    if not text:
        return ""
    tokens: List[str] = []
    cursor = 0
    for match in _CJK_RUN.finditer(text):
        tokens.extend(word.lower() for word in _ASCII_WORD.findall(text[cursor:match.start()]))
        run = match.group(0)
        for index, char in enumerate(run):
            tokens.append(char)
            if index + 1 < len(run):
                tokens.append(run[index:index + 2])
        cursor = match.end()
    tokens.extend(word.lower() for word in _ASCII_WORD.findall(text[cursor:]))
    return " ".join(tokens)


def _quote(token: str) -> str:
    """Quote a token as an FTS5 string literal, neutralising operators."""
    return '"' + token.replace('"', '""') + '"'


def build_match_expression(query: str, *, operator: str = "AND") -> str:
    """Turn raw user input into a safe FTS5 MATCH expression.

    Every emitted term is a quoted string, so FTS5 operators in the input
    (``AND``, ``OR``, ``*``, ``(``, ``NEAR``) are treated as literal text and
    can never alter the query structure.

    CJK runs contribute their bigrams (or unigrams when shorter than two
    characters) and are joined with ``operator``. The default ``AND`` requires
    every bigram, which is right for precise lookups; ``OR`` is used as a
    fallback so a full question can still find a memory that shares its topic.
    Raises ValueError for input that yields no usable term.
    """
    if not query or not query.strip():
        raise ValueError("Search query must not be empty")

    terms: List[str] = []
    cursor = 0
    for match in _CJK_RUN.finditer(query):
        terms.extend(
            _quote(word.lower())
            for word in _ASCII_WORD.findall(query[cursor:match.start()])
        )
        run = match.group(0)
        bigrams = [run[i:i + 2] for i in range(len(run) - 1)]
        terms.extend(_quote(item) for item in (bigrams or list(run)))
        cursor = match.end()
    terms.extend(
        _quote(word.lower()) for word in _ASCII_WORD.findall(query[cursor:])
    )

    # A query made only of punctuation or symbols has nothing to match on.
    terms = [term for term in terms if term != '""']
    if not terms:
        raise ValueError("Search query contained no searchable terms")
    joiner = " OR " if operator.upper() == "OR" else " AND "
    return joiner.join(terms)
