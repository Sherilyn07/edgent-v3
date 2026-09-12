"""Cutting text into passages, without holding the document in memory.

Version 1 did `body.split()` on the whole document, which made every short word
a separate Python object. Measured, that cost sixteen to twenty-six times the
file's size — a 25 MB text file peaked at 406 MB — and put the ceiling for a
2 GB server at about 100 MB of plain text, with no error that pointed anywhere
useful when it was crossed.

This version streams. It reads a line at a time, emits chunks as they fill, and
never holds more than one chunk plus its overlap. Memory is flat regardless of
how large the document is.

The output shape is unchanged, so nothing downstream can tell the difference.
"""
import re
from collections.abc import Iterable, Iterator
from typing import Any

from .config import get_settings

settings = get_settings()

HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
MIN_WORDS = 20              # below this it is a page number or a stray heading


def _section_name(trail: list[str]) -> str:
    return " > ".join(trail) if trail else "(document)"


def iter_chunks(
    lines: Iterable[str],
    *,
    session_id: str,
    file_id: str,
    source: str,
    kind: str,
) -> Iterator[dict[str, Any]]:
    """Yield chunk records from a stream of lines.

    Headings reset the section; the words in between accumulate into windows of
    `chunk_words` that overlap by `chunk_overlap_words`, so an idea sitting on a
    boundary survives whole in one of them.
    """
    size = settings.chunk_words
    overlap = min(settings.chunk_overlap_words, size - 1)
    step = max(1, size - overlap)

    trail: list[str] = []
    window: list[str] = []          # words waiting to be emitted
    ordinal = 0

    def emit(words: list[str], section: str) -> dict[str, Any]:
        nonlocal ordinal
        record = {
            "chunk_key": f"{file_id}:{ordinal:04d}",
            "session_id": session_id,
            "file_id": file_id,
            "source": source,
            "kind": kind,
            "section": section,
            "ordinal": ordinal,
            "text": " ".join(words),
        }
        ordinal += 1
        return record

    section = _section_name(trail)

    for line in lines:
        match = HEADING.match(line)
        if match:
            # A heading closes the current section. Flush what is left of it,
            # then reset the window -- overlap should not cross a heading.
            if len(window) >= MIN_WORDS:
                yield emit(window, section)
            window = []

            depth = len(match.group(1))
            del trail[depth - 1 :]
            trail.append(match.group(2).strip())
            section = _section_name(trail)
            continue

        window.extend(line.split())

        # Emit every full window, keeping the overlap for the next one.
        while len(window) >= size:
            yield emit(window[:size], section)
            window = window[step:]

    if len(window) >= MIN_WORDS:
        yield emit(window, section)


def iter_file_chunks(path: str, **kwargs) -> Iterator[dict[str, Any]]:
    """Chunk a file on disk without reading it whole."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        yield from iter_chunks(handle, **kwargs)


def batched(items: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Group an iterator into lists, so the stream never becomes one list."""
    batch: list[dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
