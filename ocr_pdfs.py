#!/usr/bin/env python3
"""
OCR all PDFs under library/content/ to extract tune titles from scanned bagpipe sheet music.

Outputs:
  - ocr_results.json: raw OCR text and detected titles per PDF
  - index.json: enhanced with ocr_titles fields and new entries for unreferenced PDFs
"""

import json
import os
import re
import sys
import time
import subprocess
from pathlib import Path

# Force unbuffered output so progress is visible when piped
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# 0. Check / install dependencies
# ---------------------------------------------------------------------------

def ensure_tesseract():
    """Make sure the tesseract binary is available."""
    try:
        result = subprocess.run(["tesseract", "--version"],
                                capture_output=True, text=True)
        print(f"Tesseract found: {result.stdout.splitlines()[0]}")
    except FileNotFoundError:
        print("Tesseract not found — installing via brew …")
        subprocess.check_call(["brew", "install", "tesseract"])
        print("Tesseract installed.")

def ensure_python_packages():
    """pip-install the required Python packages if missing."""
    needed = {"pytesseract": "pytesseract", "PIL": "Pillow", "fitz": "PyMuPDF"}
    missing = []
    for imp, pkg in needed.items():
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Installing Python packages: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])

ensure_tesseract()
ensure_python_packages()

import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import io

# ---------------------------------------------------------------------------
# 1. Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONTENT_DIR = BASE_DIR / "library" / "content"
INDEX_PATH = BASE_DIR / "index.json"
OCR_RESULTS_PATH = BASE_DIR / "ocr_results.json"
DPI = 150
PROGRESS_EVERY = 100

# Tune-type keywords used to help identify title lines
TUNE_TYPES = [
    "march", "slow march", "quick march", "competition march",
    "strathspey", "reel", "jig", "slip jig", "hornpipe",
    "quickstep", "polka", "waltz", "slow air", "air",
    "lament", "salute", "gathering", "retreat",
    "pibroch", "piobaireachd", "ceol mor", "ceol meadhonach",
    "2/4 march", "3/4 march", "4/4 march", "6/8 march",
    "6/8", "2/4", "3/4", "4/4", "9/8",
]

# Pre-compile a regex that matches a standalone tune-type line
_type_pattern = "|".join(re.escape(t) for t in sorted(TUNE_TYPES, key=len, reverse=True))
TYPE_RE = re.compile(r"^\s*(?:\(?\s*(?:" + _type_pattern + r")\s*\)?)\s*$", re.IGNORECASE)

# Pattern for lines that are clearly NOT titles
NOT_TITLE_RE = re.compile(
    r"(?:^\s*$"                             # blank
    r"|^\s*\d+\s*$"                         # page number only
    r"|^\s*(?:page|book|vol|contents|index|preface|foreword|introduction)" # structural
    r"|^\s*(?:copyright|©|all rights|printed|published)"
    r"|^\s*(?:www\.|http)"
    r")",
    re.IGNORECASE,
)

# Lines that look like music notation artefacts from OCR
MUSIC_NOISE_RE = re.compile(
    r"^[\s\|\-\.\,\_\:\;\'\"\(\)\[\]\{\}0-9]+$"
)

# ---------------------------------------------------------------------------
# 2. Title-detection heuristics
# ---------------------------------------------------------------------------

def detect_titles(ocr_text: str) -> list[str]:
    """
    Given full OCR text of one PDF page, return a list of likely tune titles.

    Heuristics:
    - Titles tend to be at/near the top of the page.
    - They are often in UPPER CASE or Title Case, on short lines.
    - They are often followed by a tune-type designation line.
    - Multiple tunes on a page repeat the pattern.
    """
    lines = ocr_text.split("\n")
    titles: list[str] = []
    n = len(lines)

    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue

        # Skip obvious non-title lines
        if NOT_TITLE_RE.match(line):
            continue
        if MUSIC_NOISE_RE.match(line):
            continue
        if TYPE_RE.match(line):
            continue  # this is a type descriptor, not a title

        # Length heuristic: titles are usually short-ish (3–80 chars)
        if len(line) < 3 or len(line) > 80:
            continue

        # Check if this line looks like a title
        is_title_candidate = False

        # A) Line is mostly uppercase letters (>= 60% uppercase alpha)
        alpha_chars = [c for c in line if c.isalpha()]
        if alpha_chars:
            upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
            if upper_ratio >= 0.60 and len(alpha_chars) >= 3:
                is_title_candidate = True

        # B) Line is in Title Case (first letter of most words capitalised)
        words = [w for w in line.split() if w[0:1].isalpha()]
        if len(words) >= 2:
            cap_words = sum(1 for w in words if w[0].isupper())
            if cap_words / len(words) >= 0.6:
                is_title_candidate = True

        # C) Next non-blank line is a tune-type descriptor
        if is_title_candidate or True:  # always check lookahead
            for j in range(i + 1, min(i + 4, n)):
                next_line = lines[j].strip()
                if not next_line:
                    continue
                if TYPE_RE.match(next_line):
                    is_title_candidate = True
                break  # only check the first non-blank following line

        if not is_title_candidate:
            continue

        # Clean up the candidate
        title = line.strip(" .-—_*#")
        # Remove trailing tune-type if glued to the title
        # e.g. "BONNIE DUNDEE Reel" -> keep as-is, the type gives context
        if title and title not in titles:
            titles.append(title)

    return titles


# ---------------------------------------------------------------------------
# 3. OCR a single PDF
# ---------------------------------------------------------------------------

def ocr_pdf(pdf_path: Path) -> dict:
    """
    Render each page of *pdf_path* at DPI resolution, OCR it, and return
    a dict with the combined OCR text and detected titles.
    """
    full_text_parts: list[str] = []
    all_titles: list[str] = []

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        return {
            "pdf": str(pdf_path.relative_to(CONTENT_DIR.parent)),
            "ocr_text": f"[ERROR opening PDF: {e}]",
            "detected_titles": [],
        }

    for page_num in range(len(doc)):
        page = doc[page_num]
        # Render at target DPI
        zoom = DPI / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))

        try:
            text = pytesseract.image_to_string(img)
        except Exception as e:
            text = f"[OCR ERROR: {e}]"

        full_text_parts.append(text)
        titles = detect_titles(text)
        for t in titles:
            if t not in all_titles:
                all_titles.append(t)

    doc.close()

    combined_text = "\n--- PAGE BREAK ---\n".join(full_text_parts)
    return {
        "pdf": str(pdf_path.relative_to(CONTENT_DIR.parent)),
        "ocr_text": combined_text,
        "detected_titles": all_titles,
    }


# ---------------------------------------------------------------------------
# 4. Discover all PDFs
# ---------------------------------------------------------------------------

def find_all_pdfs() -> list[Path]:
    pdfs = sorted(CONTENT_DIR.rglob("*.pdf"))
    # Also catch .PDF
    pdfs += sorted(p for p in CONTENT_DIR.rglob("*.PDF") if p not in pdfs)
    return pdfs


# ---------------------------------------------------------------------------
# 5. Main OCR loop
# ---------------------------------------------------------------------------

def run_ocr():
    pdfs = find_all_pdfs()
    total = len(pdfs)
    print(f"Found {total} PDFs to process under {CONTENT_DIR}")

    results: list[dict] = []
    t0 = time.time()

    for idx, pdf_path in enumerate(pdfs, 1):
        result = ocr_pdf(pdf_path)
        results.append(result)

        if idx % PROGRESS_EVERY == 0 or idx == total:
            elapsed = time.time() - t0
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (total - idx) / rate if rate > 0 else 0
            print(f"  [{idx}/{total}] {elapsed:.0f}s elapsed, "
                  f"{rate:.1f} PDFs/s, ETA {eta:.0f}s — "
                  f"last: {result['pdf']} → {len(result['detected_titles'])} titles")

    # Write OCR results
    with open(OCR_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nOCR results written to {OCR_RESULTS_PATH}")

    return results


# ---------------------------------------------------------------------------
# 6. Merge OCR results into index.json
# ---------------------------------------------------------------------------

def merge_into_index(ocr_results: list[dict]):
    """
    - For PDFs already in index.json, add an 'ocr_titles' field.
    - For PDFs NOT in the index, create new entries.
    """
    # Load existing index
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        index = json.load(f)

    # Build a lookup by pdf path
    index_by_pdf: dict[str, dict] = {}
    for entry in index:
        index_by_pdf[entry["pdf"]] = entry

    # Build OCR lookup
    ocr_by_pdf: dict[str, dict] = {}
    for r in ocr_results:
        ocr_by_pdf[r["pdf"]] = r

    # Merge existing entries
    for entry in index:
        pdf_key = entry["pdf"]
        if pdf_key in ocr_by_pdf:
            entry["ocr_titles"] = ocr_by_pdf[pdf_key]["detected_titles"]

    # Add new entries for unreferenced PDFs
    new_count = 0
    for r in ocr_results:
        pdf_key = r["pdf"]
        if pdf_key not in index_by_pdf:
            # Infer collection/book from path
            parts = Path(pdf_key).parts  # e.g. ('content', 'AGlen', 'Book01', 'Book01 1.pdf')
            collection_short = parts[1] if len(parts) > 1 else "Unknown"
            book = parts[2] if len(parts) > 2 else "Unknown"

            new_entry = {
                "pdf": pdf_key,
                "tunes": [],
                "collection": None,
                "collection_short": collection_short,
                "book": book,
                "ocr_titles": r["detected_titles"],
                "source": "ocr_only",
            }
            index.append(new_entry)
            new_count += 1

    # Save enhanced index
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    print(f"Index updated: {len(index)} total entries ({new_count} new from OCR)")


# ---------------------------------------------------------------------------
# 7. Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("Bagpipe Sheet Music OCR")
    print("=" * 60)
    ocr_results = run_ocr()
    print("\n" + "=" * 60)
    print("Merging OCR results into index.json …")
    print("=" * 60)
    merge_into_index(ocr_results)
    print("\nDone.")
