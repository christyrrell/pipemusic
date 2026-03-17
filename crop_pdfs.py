#!/usr/bin/env python3
"""
Crop headers and footers from ~4,700 scanned music PDFs under library/content/.

Header: top ~75 pts of the rendered/visible page (contains "Return to Index" link).
Footer: bottom ~55 pts of the rendered/visible page (contains descriptive text).

Uses PyMuPDF (fitz) to:
  - Remove link annotations
  - Redact text in header/footer zones
  - Set CropBox to exclude header/footer
"""

import os
import sys
import tempfile
import time
import shutil
import fitz  # PyMuPDF


HEADER_PTS = 75   # points to remove from the visual top
FOOTER_PTS = 55   # points to remove from the visual bottom
CONTENT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "library", "content")


def get_crop_and_redact_rects(page):
    """
    Return (cropbox, header_rect, footer_rect) in MediaBox coordinates,
    accounting for page rotation.

    PDF coordinate system: y=0 is at the bottom of the MediaBox.
    Rotation affects how MediaBox coords map to the visible page:
      rot=0:   visual top = high y in MediaBox
      rot=90:  visual top = right edge (high x) in MediaBox
      rot=180: visual top = low y in MediaBox
      rot=270: visual top = left edge (low x) in MediaBox
    """
    mb = page.mediabox  # fitz.Rect(x0, y0, x1, y1) -- but in fitz coords (y=0 top)
    rot = page.rotation % 360

    # fitz uses top-left origin internally, but CropBox is set in PDF coords
    # (bottom-left origin). page.mediabox in fitz is already normalised.
    # We'll work with the raw PDF MediaBox via page.mediabox.
    x0, y0, x1, y1 = mb

    if rot == 0:
        # Visual top = top of page in fitz coords = y0 side (fitz y grows downward)
        # Header region: top HEADER_PTS pts of visible page
        header_rect = fitz.Rect(x0, y0, x1, y0 + HEADER_PTS)
        # Footer region: bottom FOOTER_PTS pts of visible page
        footer_rect = fitz.Rect(x0, y1 - FOOTER_PTS, x1, y1)
        # CropBox: exclude header and footer
        cropbox = fitz.Rect(x0, y0 + HEADER_PTS, x1, y1 - FOOTER_PTS)

    elif rot == 90:
        # Rotation=90: the page is rotated 90 degrees clockwise.
        # Visual top maps to the right edge of the MediaBox (high x).
        # Visual bottom maps to the left edge (low x).
        header_rect = fitz.Rect(x1 - HEADER_PTS, y0, x1, y1)
        footer_rect = fitz.Rect(x0, y0, x0 + FOOTER_PTS, y1)
        cropbox = fitz.Rect(x0 + FOOTER_PTS, y0, x1 - HEADER_PTS, y1)

    elif rot == 180:
        # Visual top = bottom of MediaBox, visual bottom = top of MediaBox
        header_rect = fitz.Rect(x0, y1 - HEADER_PTS, x1, y1)
        footer_rect = fitz.Rect(x0, y0, x1, y0 + FOOTER_PTS)
        cropbox = fitz.Rect(x0, y0 + FOOTER_PTS, x1, y1 - HEADER_PTS)

    elif rot == 270:
        # Visual top maps to left edge (low x), visual bottom to right edge (high x)
        header_rect = fitz.Rect(x0, y0, x0 + HEADER_PTS, y1)
        footer_rect = fitz.Rect(x1 - FOOTER_PTS, y0, x1, y1)
        cropbox = fitz.Rect(x0 + HEADER_PTS, y0, x1 - FOOTER_PTS, y1)

    else:
        # Fallback: treat as rotation=0
        header_rect = fitz.Rect(x0, y0, x1, y0 + HEADER_PTS)
        footer_rect = fitz.Rect(x0, y1 - FOOTER_PTS, x1, y1)
        cropbox = fitz.Rect(x0, y0 + HEADER_PTS, x1, y1 - FOOTER_PTS)

    return cropbox, header_rect, footer_rect


def process_pdf(filepath):
    """Process a single PDF: remove links, redact header/footer, set CropBox."""
    doc = fitz.open(filepath)
    modified = False

    for page in doc:
        cropbox, header_rect, footer_rect = get_crop_and_redact_rects(page)

        # Remove all link annotations by removing Link-type annotations
        annots_to_remove = []
        for annot in page.annots():
            if annot.type[0] == fitz.PDF_ANNOT_LINK:
                annots_to_remove.append(annot)
        for annot in annots_to_remove:
            page.delete_annot(annot)
            modified = True

        # Also try removing links via the links API (catches widget-style links)
        links = list(page.get_links())
        for link in links:
            try:
                page.delete_link(link)
                modified = True
            except Exception:
                pass

        # Add redaction annotations for header and footer regions
        # fill uses 0-1 float RGB values; white = (1, 1, 1)
        page.add_redact_annot(header_rect, fill=[1, 1, 1])
        page.add_redact_annot(footer_rect, fill=[1, 1, 1])
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        modified = True

        # Set CropBox to exclude header/footer
        page.set_cropbox(cropbox)

    if modified:
        # Can't save non-incrementally to the same file; use a temp file
        dirpath = os.path.dirname(filepath)
        fd, tmppath = tempfile.mkstemp(suffix=".pdf", dir=dirpath)
        os.close(fd)
        try:
            doc.save(tmppath, incremental=False, deflate=True, garbage=3)
            doc.close()
            shutil.move(tmppath, filepath)
        except Exception:
            doc.close()
            if os.path.exists(tmppath):
                os.unlink(tmppath)
            raise
    else:
        doc.close()


def main():
    if not os.path.isdir(CONTENT_ROOT):
        print(f"ERROR: Content directory not found: {CONTENT_ROOT}")
        sys.exit(1)

    # Collect all PDF paths
    pdf_paths = []
    for dirpath, _dirnames, filenames in os.walk(CONTENT_ROOT):
        for fn in filenames:
            if fn.lower().endswith(".pdf"):
                pdf_paths.append(os.path.join(dirpath, fn))

    pdf_paths.sort()
    total = len(pdf_paths)
    print(f"Found {total} PDF files to process.")

    if total == 0:
        print("Nothing to do.")
        return

    errors = []
    t0 = time.time()

    for i, path in enumerate(pdf_paths, 1):
        try:
            process_pdf(path)
        except Exception as e:
            errors.append((path, str(e)))

        if i % 100 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(f"  [{i}/{total}] processed  ({elapsed:.1f}s elapsed, ~{eta:.0f}s remaining)")

    elapsed = time.time() - t0
    print(f"\nDone. Processed {total} files in {elapsed:.1f}s.")
    print(f"  Successful: {total - len(errors)}")
    print(f"  Errors:     {len(errors)}")

    if errors:
        print("\nFiles with errors:")
        for path, err in errors:
            print(f"  {path}: {err}")


if __name__ == "__main__":
    main()
