"""Turning an uploaded file into plain text on disk.

Unchanged in spirit from version 1: Docling for PDFs and Word documents, a
plain read for text. The difference is that the result is written to a *file*
rather than returned as a string, so the chunker can stream it and nothing ever
holds the whole document in memory.

Videos never reach here. They go to the speech-to-text service, which needs a
GPU.
"""
import logging
import os
import shutil

log = logging.getLogger(__name__)

KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_TEXT = "text"
KIND_VIDEO = "video"

EXTENSION_KINDS = {
    ".pdf": KIND_PDF,
    ".docx": KIND_DOCX,
    ".doc": KIND_DOCX,
    ".txt": KIND_TEXT,
    ".md": KIND_TEXT,
    ".mp4": KIND_VIDEO,
    ".mov": KIND_VIDEO,
    ".mkv": KIND_VIDEO,
    ".webm": KIND_VIDEO,
    ".m4a": KIND_VIDEO,
    ".mp3": KIND_VIDEO,
    ".wav": KIND_VIDEO,
}


def kind_for_filename(filename: str) -> str | None:
    """The kind for a filename, or None if we do not accept it."""
    return EXTENSION_KINDS.get(os.path.splitext(filename)[1].lower())


_converter = None


def get_converter():
    """Load Docling once per process and reuse it.

    The first call downloads its layout models, which is slow. Loading it lazily
    means a worker that only ever sees .txt files never pays for it.
    """
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter

        log.info("loading docling (first time is slow)")
        _converter = DocumentConverter()
    return _converter


def to_text_file(source_path: str, kind: str, target_path: str) -> int:
    """Extract text from `source_path` into `target_path`. Returns bytes written.

    Text files are copied rather than parsed — there is nothing to extract, and
    copying keeps memory flat for a file of any size.
    """
    if kind == KIND_TEXT:
        shutil.copyfile(source_path, target_path)
        return os.path.getsize(target_path)

    if kind in (KIND_PDF, KIND_DOCX):
        result = get_converter().convert(source_path)
        markdown = result.document.export_to_markdown()
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        return os.path.getsize(target_path)

    raise ValueError(f"cannot convert kind {kind!r} to text")
