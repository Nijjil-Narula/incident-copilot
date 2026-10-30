"""
api/app/services/chunker.py

Pure text normalization and document chunking functions for Incident Copilot.

This module contains NO I/O operations (file, database, or network).
It provides:
  - normalize_text: Decodes bytes as UTF-8 (supporting BOM) and normalises newlines.
  - extract_pdf: Extracts text from PDF bytes with page and content validations.
  - chunk_document: Chunks markdown and plain text files with section tracking,
    atomic code block preservation, and selective prose overlap.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import List, Optional

TARGET_CHUNK_SIZE = 1200
TARGET_OVERLAP_SIZE = 150
MAX_PDF_PAGES = 300
MIN_PDF_CHAR_COUNT = 20


@dataclass
class DocumentChunk:
    """Represents a single chunk of an ingested document."""

    chunk_index: int
    section: Optional[str]
    text: str
    embed_text: str


def normalize_text(content_bytes: bytes) -> str:
    """
    Decode raw bytes as UTF-8 (handling BOM with utf-8-sig) and normalise newlines.

    Raises:
        ValueError: If content_bytes is not valid UTF-8 text.
    """
    try:
        text = content_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("File is not valid UTF-8 text") from exc

    # Convert \r\n and \r to standard Unix \n
    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_pdf(content_bytes: bytes) -> str:
    """
    Extracts text from PDF raw bytes.

    Validates:
      - Page count must be <= 300.
      - Total non-whitespace character count must be >= 20.

    Raises:
        ValueError: If PDF exceeds page limit or has no extractable text.
    """
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(content_bytes))
    except Exception as exc:
        raise ValueError(f"Failed to parse PDF document: {exc}") from exc

    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"PDF exceeds maximum page limit of {MAX_PDF_PAGES} pages")

    extracted_pages: List[str] = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        extracted_pages.append(page_text)

    full_text = "\n\n".join(extracted_pages).replace("\r\n", "\n").replace("\r", "\n")

    # Count non-whitespace characters to detect scanned / image-only PDFs
    non_whitespace_count = len("".join(full_text.split()))
    if non_whitespace_count < MIN_PDF_CHAR_COUNT:
        raise ValueError("No extractable text (is this a scanned PDF?)")

    return full_text


def _extract_overlap(text: str, max_overlap: int = TARGET_OVERLAP_SIZE) -> str:
    """
    Extracts up to max_overlap characters from the end of a prose text,
    snapping forward to the nearest word boundary so words are not cut in half.
    """
    if len(text) <= max_overlap:
        return text.strip()

    tail = text[-max_overlap:]
    space_idx = tail.find(" ")
    newline_idx = tail.find("\n")

    split_idx = -1
    if space_idx != -1 and newline_idx != -1:
        split_idx = min(space_idx, newline_idx)
    elif space_idx != -1:
        split_idx = space_idx
    elif newline_idx != -1:
        split_idx = newline_idx

    if split_idx != -1 and split_idx < len(tail) - 1:
        tail = tail[split_idx + 1 :]

    return tail.strip()


def _split_oversize_prose(text: str, max_size: int = TARGET_CHUNK_SIZE) -> List[str]:
    """
    Splits a single oversize prose block (e.g. > 1200 chars without paragraphs)
    into sub-chunks of up to max_size, respecting word boundaries and adding overlap.
    """
    if len(text) <= max_size:
        return [text]

    chunks: List[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + max_size, text_len)
        if end < text_len:
            # Find a break point near end (newline, sentence end, or space)
            search_window = text[max(start, end - 200) : end]
            last_break = max(
                search_window.rfind("\n"),
                search_window.rfind(". "),
                search_window.rfind(" "),
            )
            if last_break != -1:
                end = max(start, end - 200) + last_break
                if text[end : end + 2] == ". ":
                    end += 1  # Include the period

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        # Advance start by chunk length minus overlap
        advance = (end - start) - TARGET_OVERLAP_SIZE
        if advance <= 0:
            advance = max(1, end - start)
        start += advance

    return chunks


def _chunk_plain_text(filename: str, text: str) -> List[DocumentChunk]:
    """
    Chunks plain text files (.txt, .pdf) by paragraphs with ~1200 char limit
    and ~150 char overlap. Section is always None.
    """
    raw_paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not raw_paragraphs:
        return []

    # Flatten any oversize paragraphs
    paragraphs: List[str] = []
    for p in raw_paragraphs:
        if len(p) > TARGET_CHUNK_SIZE:
            paragraphs.extend(_split_oversize_prose(p))
        else:
            paragraphs.append(p)

    chunks: List[DocumentChunk] = []
    current_parts: List[str] = []
    current_len = 0
    pending_overlap = ""

    def flush_chunk():
        nonlocal current_parts, current_len, pending_overlap
        if not current_parts:
            return
        chunk_text = "\n\n".join(current_parts).strip()
        if chunk_text:
            idx = len(chunks)
            chunks.append(
                DocumentChunk(
                    chunk_index=idx,
                    section=None,
                    text=chunk_text,
                    embed_text=f"{filename}\n{chunk_text}",
                )
            )
            pending_overlap = _extract_overlap(chunk_text)
        current_parts = []
        current_len = 0

    for para in paragraphs:
        # If starting a new chunk and we have overlap, seed with overlap
        if not current_parts and pending_overlap:
            current_parts.append(pending_overlap)
            current_len = len(pending_overlap)
            pending_overlap = ""

        sep_len = 2 if current_parts else 0
        if current_len + sep_len + len(para) <= TARGET_CHUNK_SIZE or not current_parts:
            current_parts.append(para)
            current_len += sep_len + len(para)
        else:
            flush_chunk()
            if pending_overlap:
                current_parts.append(pending_overlap)
                current_len = len(pending_overlap)
                pending_overlap = ""
            sep_len = 2 if current_parts else 0
            current_parts.append(para)
            current_len += sep_len + len(para)

    flush_chunk()
    return chunks


def _chunk_markdown(filename: str, text: str) -> List[DocumentChunk]:
    """
    Chunks markdown text (.md, .markdown) preserving sections, atomic code blocks,
    and applying prose overlap.
    """
    lines = text.split("\n")

    # Step 1: Parse lines into segments tagged with (section, is_code, text)
    # Order: Check code fences FIRST. Lines starting with # inside code are strictly code.
    segments: List[dict] = []
    in_code = False
    fence_marker = ""
    code_lines: List[str] = []
    heading_stack: List[str] = []
    prose_lines: List[str] = []

    def get_current_section() -> Optional[str]:
        return " > ".join(heading_stack) if heading_stack else None

    def flush_prose():
        nonlocal prose_lines
        if prose_lines:
            prose_text = "\n".join(prose_lines)
            if prose_text.strip():
                segments.append(
                    {
                        "section": get_current_section(),
                        "is_code": False,
                        "text": prose_text,
                    }
                )
            prose_lines = []

    for line in lines:
        stripped = line.strip()

        if not in_code:
            # Check for code fence start (``` or ~~~)
            if stripped.startswith("```") or stripped.startswith("~~~"):
                flush_prose()
                in_code = True
                fence_marker = stripped[:3]
                code_lines = [line]
                continue

            # Check for markdown heading (# through ######)
            heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if heading_match:
                flush_prose()
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()

                if level <= len(heading_stack):
                    heading_stack = heading_stack[: level - 1]
                heading_stack.append(title)

                # Keep the heading line in the prose text so document content is preserved
                prose_lines.append(line)
                continue

            prose_lines.append(line)

        else:
            # Inside code fence
            code_lines.append(line)
            # Lines starting with # inside a code fence are strictly code, never headings
            if stripped.startswith(fence_marker):
                # Code fence closed
                in_code = False
                fence_marker = ""
                code_text = "\n".join(code_lines)
                segments.append(
                    {
                        "section": get_current_section(),
                        "is_code": True,
                        "text": code_text,
                    }
                )
                code_lines = []

    # EOF handling: an unclosed fence at end of file counts as code to the end
    if in_code and code_lines:
        segments.append(
            {
                "section": get_current_section(),
                "is_code": True,
                "text": "\n".join(code_lines),
            }
        )
    elif prose_lines:
        flush_prose()

    # Step 2: Pack segments into chunks per section
    # Fenced code blocks are ATOMIC (never split, oversize stays oversize).
    # Overlap of ~150 chars is allowed ONLY between prose chunks and NEVER contains code.
    chunks: List[DocumentChunk] = []

    # Group segments into discrete chunk items: either atomic code block or prose paragraphs
    items: List[dict] = []
    for seg in segments:
        sec = seg["section"]
        if seg["is_code"]:
            items.append({"section": sec, "is_code": True, "text": seg["text"]})
        else:
            # Break prose into paragraphs
            paras = [p.strip() for p in re.split(r"\n\s*\n", seg["text"]) if p.strip()]
            for p in paras:
                if len(p) > TARGET_CHUNK_SIZE:
                    for sub_p in _split_oversize_prose(p):
                        items.append({"section": sec, "is_code": False, "text": sub_p})
                else:
                    items.append({"section": sec, "is_code": False, "text": p})

    # Pack items greedily up to TARGET_CHUNK_SIZE
    current_items: List[dict] = []
    current_len = 0
    last_chunk_was_pure_prose = False
    pending_prose_overlap = ""

    def flush_items():
        nonlocal current_items, current_len, last_chunk_was_pure_prose, pending_prose_overlap
        if not current_items:
            return

        chunk_section = current_items[0]["section"]
        is_pure_prose = all(not it["is_code"] for it in current_items)
        chunk_text = "\n\n".join(it["text"] for it in current_items).strip()

        if chunk_text:
            idx = len(chunks)
            if chunk_section:
                embed_text = f"{filename} | {chunk_section}\n{chunk_text}"
            else:
                embed_text = f"{filename}\n{chunk_text}"

            chunks.append(
                DocumentChunk(
                    chunk_index=idx,
                    section=chunk_section,
                    text=chunk_text,
                    embed_text=embed_text,
                )
            )

            if is_pure_prose:
                last_chunk_was_pure_prose = True
                pending_prose_overlap = _extract_overlap(chunk_text)
            else:
                last_chunk_was_pure_prose = False
                pending_prose_overlap = ""

        current_items = []
        current_len = 0

    for item in items:
        # If section changed, flush immediately (clean section boundaries)
        if current_items and current_items[0]["section"] != item["section"]:
            flush_items()
            # Overlap does not carry over across different sections
            pending_prose_overlap = ""
            last_chunk_was_pure_prose = False

        # If item is atomic code block and larger than TARGET_CHUNK_SIZE
        if item["is_code"] and len(item["text"]) >= TARGET_CHUNK_SIZE:
            # Flush any pending items first
            flush_items()
            # Add oversize code block as single chunk
            current_items = [item]
            flush_items()
            continue

        # If starting fresh chunk with prose, and previous chunk was pure prose in same section:
        if (
            not current_items
            and not item["is_code"]
            and last_chunk_was_pure_prose
            and pending_prose_overlap
        ):
            current_items.append(
                {
                    "section": item["section"],
                    "is_code": False,
                    "text": pending_prose_overlap,
                }
            )
            current_len = len(pending_prose_overlap)
            pending_prose_overlap = ""

        sep_len = 2 if current_items else 0
        if (
            current_len + sep_len + len(item["text"]) <= TARGET_CHUNK_SIZE
            or not current_items
        ):
            current_items.append(item)
            current_len += sep_len + len(item["text"])
        else:
            flush_items()
            # If new chunk begins with prose and last chunk was pure prose:
            if not item["is_code"] and last_chunk_was_pure_prose and pending_prose_overlap:
                current_items.append(
                    {
                        "section": item["section"],
                        "is_code": False,
                        "text": pending_prose_overlap,
                    }
                )
                current_len = len(pending_prose_overlap)
                pending_prose_overlap = ""

            sep_len = 2 if current_items else 0
            current_items.append(item)
            current_len += sep_len + len(item["text"])

    flush_items()
    return chunks


def chunk_document(filename: str, text: str) -> List[DocumentChunk]:
    """
    Main entry point for chunking a normalized document.

    Dispatches to markdown chunker for .md/.markdown files, or plain text chunker
    for all other supported files (.txt, .pdf).

    Drops empty or whitespace-only chunks.
    """
    clean_text = text.strip()
    if not clean_text:
        return []

    lower_name = filename.lower()
    if lower_name.endswith((".md", ".markdown")):
        raw_chunks = _chunk_markdown(filename, text)
    else:
        raw_chunks = _chunk_plain_text(filename, text)

    # Filter out empty or whitespace-only chunks and re-index sequentially
    valid_chunks: List[DocumentChunk] = []
    for chunk in raw_chunks:
        stripped_text = chunk.text.strip()
        if stripped_text:
            valid_chunks.append(
                DocumentChunk(
                    chunk_index=len(valid_chunks),
                    section=chunk.section,
                    text=stripped_text,
                    embed_text=chunk.embed_text,
                )
            )

    return valid_chunks


__all__ = [
    "DocumentChunk",
    "normalize_text",
    "extract_pdf",
    "chunk_document",
    "TARGET_CHUNK_SIZE",
    "TARGET_OVERLAP_SIZE",
]
