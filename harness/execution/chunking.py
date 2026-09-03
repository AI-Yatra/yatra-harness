"""Cutting a repository into pieces worth ranking.

A chunk is the unit retrieval returns, so what a chunk *is* decides what
retrieval can find. Fixed line windows were the cheap answer and they are
wrong in a specific way: a forty-line window starting at line 60 is a
fragment of two functions and the whole of neither, and nothing in it says
which functions those were. Rank it first and the model still has to open the
file to find out what it is looking at.

So a Python file is cut at its own boundaries instead, using `ast` from the
standard library. Each function and method becomes one chunk that knows its
own name. Everything else -- other languages, unparseable files, module-level
code between definitions -- falls back to line windows, because a worse chunk
is much better than no chunk.

The other half of the idea is `search_text`. A chunk is indexed as its path
and symbol *followed by* its source, so `harness/models/providers.py` and
`_retry_after` are terms a query can match. Asking "where is the retry backoff
decided" previously ranked the documentation that contains the phrase above
the code that does it, because the code says `backoff_seconds` and never says
"retry backoff" in prose. Naming the symbol in the indexed text is what closes
that gap without an embedding model.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

#: A definition longer than this is split into windows rather than returned
#: whole. Some functions are hundreds of lines, and a chunk that large drowns
#: its own signal in a ranking and blows a context budget when it is read.
MAX_CHUNK_LINES = 120

#: Below this, a span is folded into its neighbour rather than being ranked as
#: a chunk on its own. A three-line chunk matches a term with almost no
#: competition from its own length and floats to the top of every query.
MIN_CHUNK_LINES = 3


@dataclass(frozen=True, slots=True)
class Chunk:
    """One rankable piece of one file."""

    path: str
    start_line: int
    end_line: int
    text: str
    #: The definition this came from, `Class.method` where it is one. Empty
    #: for module-level code and for files with no structure we can read.
    symbol: str = ""

    @property
    def search_text(self) -> str:
        """What is indexed, as opposed to what is shown.

        The path and symbol lead, so a query naming either reaches the chunk.
        The path is split on separators as well as kept whole, so `providers`
        matches `harness/models/providers.py`.
        """
        head = self.path.replace("/", " ").replace(".", " ")
        if self.symbol:
            head = f"{head} {self.symbol.replace('.', ' ')} {self.symbol}"
        return f"{head}\n{self.text}"

    @property
    def label(self) -> str:
        """How the chunk names itself on screen."""
        where = f"{self.path}:{self.start_line}-{self.end_line}"
        return f"{where} ({self.symbol})" if self.symbol else where


def chunk_text(
    text: str, relative: str, *, lines_per_chunk: int
) -> list[Chunk]:
    """Cut one file's contents, at its own boundaries where we can read them."""
    if relative.endswith(".py"):
        structured = python_chunks(text, relative)
        if structured:
            return structured
    return window_chunks(text, relative, lines_per_chunk=lines_per_chunk)


def window_chunks(
    text: str, relative: str, *, lines_per_chunk: int, symbol: str = "", offset: int = 0
) -> list[Chunk]:
    """Fixed windows: the fallback, and the only option for most languages."""
    lines = text.splitlines()
    chunks = []
    for start in range(0, len(lines), lines_per_chunk):
        window = lines[start : start + lines_per_chunk]
        if not "\n".join(window).strip():
            continue
        chunks.append(
            Chunk(
                path=relative,
                start_line=offset + start + 1,
                end_line=offset + start + len(window),
                text="\n".join(window),
                symbol=symbol,
            )
        )
    return chunks


def python_chunks(text: str, relative: str) -> list[Chunk]:
    """Every definition in a Python file, plus whatever sits between them.

    Returns an empty list when the file cannot be parsed, which is not an
    error: a file mid-edit is exactly when an agent most wants to search, and
    falling back to windows keeps it findable.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    lines = text.splitlines()
    if not lines:
        return []

    spans = _definition_spans(tree, "")
    spans.sort()
    covered = _fill_gaps(spans, len(lines))
    chunks: list[Chunk] = []
    for start, end, symbol in covered:
        # Trailing blank lines belong to nothing. Left on, a chunk's line
        # range claims more of the file than the chunk actually shows, and
        # `read_file` on those coordinates returns padding.
        while end > start and not lines[end - 1].strip():
            end -= 1
        body = "\n".join(lines[start - 1 : end])
        if not body.strip():
            continue
        if end - start + 1 > MAX_CHUNK_LINES:
            # Split, but every piece keeps the name of what it came from.
            chunks.extend(
                window_chunks(
                    body, relative, lines_per_chunk=MAX_CHUNK_LINES,
                    symbol=symbol, offset=start - 1,
                )
            )
            continue
        chunks.append(
            Chunk(path=relative, start_line=start, end_line=end, text=body, symbol=symbol)
        )
    return chunks


def _definition_spans(node: ast.AST, prefix: str) -> list[tuple[int, int, str]]:
    """Line spans of the definitions in one scope, and their qualified names.

    A class contributes its header -- decorators, signature, docstring, class
    attributes -- as one span and each of its methods as another. Keeping the
    whole class together would produce a single enormous chunk for any real
    class; splitting it entirely would lose the attributes and the docstring,
    which is often where the design is written down.
    """
    spans: list[tuple[int, int, str]] = []
    for child in getattr(node, "body", []):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            spans.append((_start(child), child.end_lineno or child.lineno, prefix + child.name))
        elif isinstance(child, ast.ClassDef):
            name = prefix + child.name
            inner = [
                grandchild
                for grandchild in child.body
                if isinstance(grandchild, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            ]
            head_end = (_start(inner[0]) - 1) if inner else (child.end_lineno or child.lineno)
            if head_end >= _start(child):
                spans.append((_start(child), head_end, name))
            spans.extend(_definition_spans(child, name + "."))
    return spans


def _start(node: ast.AST) -> int:
    """Where a definition begins, decorators included.

    `lineno` points at the `def`, so a decorated function indexed from there
    loses its decorators to the previous chunk, and `@property` is often the
    most informative line about what the function is.
    """
    decorators = getattr(node, "decorator_list", [])
    own = getattr(node, "lineno", 1)
    return min([own, *(getattr(d, "lineno", own) for d in decorators)])


def _fill_gaps(
    spans: list[tuple[int, int, str]], total: int
) -> list[tuple[int, int, str]]:
    """Add the module-level code that no definition covers.

    Imports, constants and the module docstring are not inside any function
    and would otherwise be unsearchable. Gaps shorter than `MIN_CHUNK_LINES`
    are dropped rather than ranked: a two-line chunk wins on length
    normalisation alone and pushes real matches down.
    """
    filled: list[tuple[int, int, str]] = []
    cursor = 1
    for start, end, symbol in spans:
        if start > cursor and start - cursor >= MIN_CHUNK_LINES:
            filled.append((cursor, start - 1, ""))
        filled.append((start, end, symbol))
        cursor = max(cursor, end + 1)
    if total >= cursor and total - cursor + 1 >= MIN_CHUNK_LINES:
        filled.append((cursor, total, ""))
    return filled
