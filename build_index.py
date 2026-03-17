#!/usr/bin/env python3
"""
Parse all HTML metadata from library into a single JSON index file.

Sources:
1. Master Index pages (mindex1-5.html) - base tune data
2. Per-collection TOC pages - composer, gaelic title, alternate titles
3. Alternate titles page (alttitles.html) - cross-references
"""

import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import unquote
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(BASE_DIR, "library")
CONTENT_DIR = os.path.join(SITE_DIR, "content")

# URL prefixes to strip
URL_PREFIXES = [
    "https://library/",
    "https://www.library/",
    "http://library/",
    "http://www.library/",
]


def normalize_pdf_path(href):
    """Convert an href to a local relative path under library/."""
    if not href:
        return None
    href = href.strip()
    for prefix in URL_PREFIXES:
        if href.lower().startswith(prefix.lower()):
            href = href[len(prefix):]
            break
    href = unquote(href)
    if not href.lower().endswith(".pdf"):
        return None
    # Normalize path separators
    href = href.replace("\\", "/")
    return href


def clean_text(text):
    """Clean up extracted text from HTML."""
    if not text:
        return None
    # Replace non-breaking spaces (various forms)
    text = text.replace("\xa0", " ")
    text = text.replace("&nbsp;", " ")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text


def extract_book_from_path(pdf_path):
    """Extract the book identifier from a PDF path."""
    if not pdf_path:
        return None
    parts = pdf_path.replace("\\", "/").split("/")
    for part in parts:
        if re.match(r"^Book\d+$", part, re.IGNORECASE):
            return part
    return None


def extract_collection_dir_from_path(pdf_path):
    """Extract the collection directory from a PDF path like content/DGlen/..."""
    if not pdf_path:
        return None
    parts = pdf_path.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] == "content":
        return parts[1]
    return None


def read_file(filepath, try_encodings=None):
    """Read a file trying multiple encodings."""
    if try_encodings is None:
        try_encodings = ["utf-8", "windows-1252", "latin-1"]

    # Check if file is UTF-16LE
    try:
        with open(filepath, "rb") as f:
            bom = f.read(2)
            if bom == b"\xff\xfe":  # UTF-16LE BOM
                f.seek(0)
                content = f.read().decode("utf-16-le")
                # Remove null bytes that might appear in spacing
                return content
    except Exception:
        pass

    for enc in try_encodings:
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"Could not decode {filepath}")


class TableParser(HTMLParser):
    """Parse HTML tables and extract rows with cell contents and links.

    Captures tables at ALL nesting depths. Each table (even nested ones)
    gets its own entry in self.tables. Rows/cells belong to the innermost
    table currently open. Cell state is tracked per table level using stacks.
    """

    def __init__(self):
        super().__init__()
        self.tables = []  # final list of completed tables
        # Per-table-level stacks:
        self.table_stack = []  # stack of in-progress table row-lists
        self.row_stack = []  # stack of in-progress rows (or None)
        self.cell_state_stack = []  # stack of {in_td, cell_text, cell_href}
        self.in_a = False
        self.a_href = None

    @property
    def _cs(self):
        """Current cell state (top of stack)."""
        return self.cell_state_stack[-1] if self.cell_state_stack else None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        tag = tag.lower()

        if tag == "table":
            self.table_stack.append([])
            self.row_stack.append(None)
            self.cell_state_stack.append({"in_td": False, "cell_text": "", "cell_href": None})

        elif tag == "tr" and self.table_stack:
            self.row_stack[-1] = []

        elif (tag == "td" or tag == "th") and self.table_stack:
            if self.row_stack[-1] is not None:
                cs = self._cs
                cs["in_td"] = True
                cs["cell_text"] = ""
                cs["cell_href"] = None

        elif tag == "a":
            cs = self._cs
            if cs and cs["in_td"]:
                self.in_a = True
                self.a_href = attrs_dict.get("href", "")

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "table":
            if self.table_stack:
                completed = self.table_stack.pop()
                self.row_stack.pop()
                self.cell_state_stack.pop()
                self.tables.append(completed)

        elif tag == "tr" and self.table_stack:
            if self.row_stack[-1] is not None:
                self.table_stack[-1].append(self.row_stack[-1])
            self.row_stack[-1] = None

        elif (tag == "td" or tag == "th") and self.table_stack:
            cs = self._cs
            if cs and cs["in_td"]:
                if self.row_stack[-1] is not None:
                    self.row_stack[-1].append({
                        "text": clean_text(cs["cell_text"]) or "",
                        "href": cs["cell_href"],
                    })
                cs["in_td"] = False

        elif tag == "a" and self.in_a:
            cs = self._cs
            if cs and cs["in_td"] and cs["cell_href"] is None:
                cs["cell_href"] = self.a_href
            self.in_a = False
            self.a_href = None

    def handle_data(self, data):
        cs = self._cs
        if cs and cs["in_td"]:
            cs["cell_text"] += data

    def handle_entityref(self, name):
        cs = self._cs
        if cs and cs["in_td"]:
            if name == "nbsp":
                cs["cell_text"] += " "
            elif name == "amp":
                cs["cell_text"] += "&"
            else:
                cs["cell_text"] += f"&{name};"

    def handle_charref(self, name):
        cs = self._cs
        if cs and cs["in_td"]:
            try:
                if name.startswith("x"):
                    char = chr(int(name[1:], 16))
                else:
                    char = chr(int(name))
                cs["cell_text"] += char
            except ValueError:
                pass


def parse_tables(html_content):
    """Parse HTML and return list of tables (each is a list of rows)."""
    parser = TableParser()
    try:
        parser.feed(html_content)
    except Exception as e:
        print(f"  Warning: HTML parse error: {e}", file=sys.stderr)
    return parser.tables


# ─── Build the collection name mapping from library.html ───

def build_collection_map():
    """Parse library.html to map collection directory names to full/short names."""
    filepath = os.path.join(SITE_DIR, "library.html")
    html = read_file(filepath)
    tables = parse_tables(html)

    # The mapping table has "Collection Name" and "Short Title" columns
    dir_to_full = {}
    dir_to_short = {}

    for table in tables:
        if len(table) < 2:
            continue
        # Check if header row has Collection Name / Short Title
        header = table[0]
        if len(header) < 2:
            continue
        h0 = header[0]["text"].lower() if header[0]["text"] else ""
        h1 = header[1]["text"].lower() if header[1]["text"] else ""
        if "collection" not in h0 or "short" not in h1:
            continue

        for row in table[1:]:
            if len(row) < 2:
                continue
            full_name = row[0]["text"]
            short_name = row[1]["text"]
            href = row[0].get("href") or row[1].get("href")
            if href:
                # Extract directory name from TOC URL
                for prefix in URL_PREFIXES:
                    if href.lower().startswith(prefix.lower()):
                        href = href[len(prefix):]
                        break
                # href like "content/DGlen/Dglen_TOC.html"
                parts = href.replace("\\", "/").split("/")
                if len(parts) >= 2 and parts[0] == "content":
                    dir_name = parts[1]
                    dir_to_full[dir_name.lower()] = clean_text(full_name)
                    dir_to_short[dir_name.lower()] = clean_text(short_name)

    return dir_to_full, dir_to_short


# ─── Parse master index pages ───

def parse_master_index():
    """Parse mindex1-5.html and return list of tune entries."""
    entries = []

    for i in range(1, 6):
        filepath = os.path.join(CONTENT_DIR, f"mindex{i}.html")
        if not os.path.exists(filepath):
            print(f"Warning: {filepath} not found", file=sys.stderr)
            continue

        html = read_file(filepath)
        tables = parse_tables(html)

        for table in tables:
            for row in table:
                if len(row) < 4:
                    continue
                number_cell = row[0]
                name_cell = row[1]
                type_cell = row[2]
                collection_cell = row[3]

                # Skip header rows
                num_text = number_cell["text"].lower().strip()
                if "number" in num_text or not num_text:
                    continue

                pdf_href = name_cell.get("href")
                pdf_path = normalize_pdf_path(pdf_href)
                if not pdf_path:
                    continue

                tune_name = clean_text(name_cell["text"])
                tune_type = clean_text(type_cell["text"])
                collection_name = clean_text(collection_cell["text"])

                # Get collection TOC href for directory mapping
                coll_href = collection_cell.get("href")
                coll_dir = None
                if coll_href:
                    for prefix in URL_PREFIXES:
                        if coll_href.lower().startswith(prefix.lower()):
                            coll_href = coll_href[len(prefix):]
                            break
                    parts = coll_href.replace("\\", "/").split("/")
                    if len(parts) >= 2 and parts[0] == "content":
                        coll_dir = parts[1]

                entries.append({
                    "pdf": pdf_path,
                    "name": tune_name,
                    "type": tune_type,
                    "collection_short_from_mindex": collection_name,
                    "collection_dir": coll_dir,
                })

    print(f"Master index: parsed {len(entries)} entries from mindex1-5.html")
    return entries


# ─── Parse TOC pages for supplemental data ───

def detect_toc_columns(header_row):
    """Detect which columns contain what data in a TOC table."""
    cols = {}
    for i, cell in enumerate(header_row):
        text = (cell["text"] or "").lower().strip()
        # Check specific patterns first to avoid false matches with generic "title"
        if "gaelic" in text:
            cols["gaelic"] = i
        elif "alternate" in text or "additional" in text:
            cols["alt_title"] = i
        elif "composer" in text or "arranger" in text:
            cols["composer"] = i
        elif "tune type" in text or text == "type":
            cols["type"] = i
        elif any(w in text for w in ["tune name", "tune title", "title", "tune (", "piobaireachd"]):
            cols["name"] = i
    return cols


def parse_toc_pages():
    """Parse all TOC pages and return supplemental data keyed by (pdf_path, tune_name)."""
    # Returns: dict of (pdf_path_lower, tune_name_lower) -> {composer, gaelic, alt_titles_toc}
    # Also returns: dict of pdf_path_lower -> [list of info dicts] for fallback matching
    toc_data = {}  # (pdf_lower, name_lower) -> info
    toc_by_pdf = defaultdict(list)  # pdf_lower -> [info, ...]

    # Find all TOC files
    toc_files = []
    for direntry in os.listdir(CONTENT_DIR):
        dirpath = os.path.join(CONTENT_DIR, direntry)
        if not os.path.isdir(dirpath):
            continue
        for fname in os.listdir(dirpath):
            if fname.lower().endswith("_toc.html"):
                toc_files.append(os.path.join(dirpath, fname))

    toc_files.sort()
    print(f"Found {len(toc_files)} TOC files")

    for toc_path in toc_files:
        rel_dir = os.path.basename(os.path.dirname(toc_path))
        html = read_file(toc_path)
        tables = parse_tables(html)

        for table in tables:
            if len(table) < 2:
                continue

            # Try to detect columns from header
            header = table[0]
            cols = detect_toc_columns(header)

            # Must have at least a name column with links to PDFs
            name_col = cols.get("name")
            if name_col is None:
                # Try to find it: the column that has PDF links
                for i, cell in enumerate(header):
                    text = (cell["text"] or "").lower()
                    if text and "name" not in text and "title" not in text:
                        continue
                    name_col = i
                    break
                if name_col is None:
                    name_col = 0  # default to first column

            for row in table[1:]:
                if len(row) <= name_col:
                    continue

                name_cell = row[name_col]
                pdf_href = name_cell.get("href")
                pdf_path = normalize_pdf_path(pdf_href)
                if not pdf_path:
                    continue

                info = {}

                # Composer
                comp_col = cols.get("composer")
                if comp_col is not None and comp_col < len(row):
                    composer = clean_text(row[comp_col]["text"])
                    if composer:
                        info["composer"] = composer

                # Gaelic title
                gaelic_col = cols.get("gaelic")
                if gaelic_col is not None and gaelic_col < len(row):
                    gaelic = clean_text(row[gaelic_col]["text"])
                    if gaelic:
                        info["gaelic"] = gaelic

                # Alternate titles from TOC
                alt_col = cols.get("alt_title")
                if alt_col is not None and alt_col < len(row):
                    alt_text = clean_text(row[alt_col]["text"])
                    if alt_text:
                        info["alt_titles_toc"] = [t.strip() for t in re.split(r"[;/]", alt_text) if t.strip()]

                # Also capture the tune name from TOC for per-tune matching
                toc_tune_name = clean_text(name_cell["text"])
                if info:
                    info["toc_name"] = toc_tune_name
                    key = (pdf_path.lower(), (toc_tune_name or "").lower())
                    toc_data[key] = info
                    toc_by_pdf[pdf_path.lower()].append(info)

    gaelic_count = sum(1 for v in toc_data.values() if "gaelic" in v)
    unique_pdfs = len(toc_by_pdf)
    print(f"TOC pages: extracted supplemental data for {unique_pdfs} PDFs, {len(toc_data)} tune entries ({gaelic_count} with gaelic)")
    return toc_data, toc_by_pdf


# ─── Parse alternate titles page ───

def parse_alt_titles():
    """Parse alttitles.html and return list of (pdf_path, listed_title, alt_title) tuples."""
    filepath = os.path.join(SITE_DIR, "alttitles.html")
    html = read_file(filepath)
    tables = parse_tables(html)

    alt_entries = []  # [(pdf_path, listed_title, alt_title), ...]

    for table in tables:
        for row in table:
            if len(row) < 3:
                continue

            alt_title_cell = row[0]
            listed_title_cell = row[2]

            alt_title_text = clean_text(alt_title_cell["text"])
            listed_title_text = clean_text(listed_title_cell["text"])
            listed_href = listed_title_cell.get("href")
            pdf_path = normalize_pdf_path(listed_href)

            if not alt_title_text or not pdf_path:
                continue

            # Skip header row
            if "alternate" in alt_title_text.lower() and "title" in alt_title_text.lower():
                continue

            alt_entries.append((pdf_path, listed_title_text, alt_title_text))

    print(f"Alternate titles: parsed {len(alt_entries)} cross-references")
    return alt_entries


# ─── Build the index ───

def build_index():
    # Load collection name mapping
    dir_to_full, dir_to_short = build_collection_map()
    print(f"Library: mapped {len(dir_to_full)} collections")

    # Parse all sources
    mindex_entries = parse_master_index()
    toc_data, toc_by_pdf = parse_toc_pages()
    alt_entries = parse_alt_titles()

    # toc_data is already keyed by (pdf_lower, name_lower)
    # toc_by_pdf is already keyed by pdf_lower

    # Group alt entries by PDF path (case-insensitive)
    alt_by_pdf = defaultdict(list)  # pdf_lower -> [(listed_title, alt_title), ...]
    for pdf_path, listed_title, alt_title in alt_entries:
        alt_by_pdf[pdf_path.lower()].append((listed_title, alt_title))

    # Group by PDF path
    pdf_map = defaultdict(lambda: {
        "tunes": [],
        "collection": None,
        "collection_short": None,
        "collection_dir": None,
        "book": None,
    })

    for entry in mindex_entries:
        pdf_path = entry["pdf"]
        rec = pdf_map[pdf_path]

        # Set collection info
        coll_dir = entry.get("collection_dir")
        if coll_dir:
            rec["collection_dir"] = coll_dir
            coll_key = coll_dir.lower()
            if coll_key in dir_to_full:
                rec["collection"] = dir_to_full[coll_key]
            else:
                rec["collection"] = entry.get("collection_short_from_mindex")
            if coll_key in dir_to_short:
                rec["collection_short"] = dir_to_short[coll_key]
            else:
                rec["collection_short"] = entry.get("collection_short_from_mindex")

        # Set book
        if not rec["book"]:
            rec["book"] = extract_book_from_path(pdf_path)

        # Look up TOC supplemental data by (pdf, tune_name) key
        tune_name_lower = (entry["name"] or "").lower()
        toc_key = (pdf_path.lower(), tune_name_lower)
        toc_info = toc_data.get(toc_key)
        if toc_info is None:
            # Fallback: try matching by PDF only (take first entry with data)
            pdf_entries = toc_by_pdf.get(pdf_path.lower(), [])
            if len(pdf_entries) == 1:
                toc_info = pdf_entries[0]
            else:
                toc_info = {}

        tune = {
            "name": entry["name"],
            "type": entry["type"],
            "composer": toc_info.get("composer"),
            "gaelic": toc_info.get("gaelic"),
            "alt_titles": [],
        }

        # Add alt titles from TOC
        if "alt_titles_toc" in toc_info:
            for at in toc_info["alt_titles_toc"]:
                if at not in tune["alt_titles"]:
                    tune["alt_titles"].append(at)

        rec["tunes"].append(tune)

    # Build case-insensitive map of pdf_map keys for merging
    pdf_map_ci = {}  # lower-case key -> actual key
    for k in pdf_map:
        pdf_map_ci[k.lower()] = k

    # Merge alt titles from alttitles.html
    for pdf_path, listed_title, alt_title in alt_entries:
        actual_key = pdf_map_ci.get(pdf_path.lower())
        if actual_key:
            rec = pdf_map[actual_key]
            # Try to match by listed title to the correct tune
            matched = False
            for tune in rec["tunes"]:
                if listed_title and tune["name"] and tune["name"].lower() == listed_title.lower():
                    if alt_title not in tune["alt_titles"]:
                        tune["alt_titles"].append(alt_title)
                    matched = True
                    break
            if not matched:
                # Fallback: if only one tune, add to it; otherwise add to all
                if len(rec["tunes"]) == 1:
                    if alt_title not in rec["tunes"][0]["alt_titles"]:
                        rec["tunes"][0]["alt_titles"].append(alt_title)
                else:
                    # Add to all tunes as we can't determine which one
                    for tune in rec["tunes"]:
                        if alt_title not in tune["alt_titles"]:
                            tune["alt_titles"].append(alt_title)
        else:
            print(f"  Warning: alt title PDF not in master index: {pdf_path}", file=sys.stderr)

    # Build final output
    result = []
    for pdf_path in sorted(pdf_map.keys()):
        rec = pdf_map[pdf_path]
        entry = {
            "pdf": pdf_path,
            "tunes": rec["tunes"],
            "collection": rec["collection"],
            "collection_short": rec["collection_short"],
            "book": rec["book"],
        }
        # Clean up: convert empty alt_titles lists to empty lists (already are)
        # Set null for None alt_titles that are empty
        for tune in entry["tunes"]:
            if not tune["alt_titles"]:
                tune["alt_titles"] = []
        result.append(entry)

    return result


def main():
    print("Building library index...")
    print()

    index = build_index()

    output_path = os.path.join(BASE_DIR, "index.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)

    print()
    print(f"Output: {output_path}")
    print(f"Total PDF entries: {len(index)}")
    total_tunes = sum(len(e["tunes"]) for e in index)
    print(f"Total tune records: {total_tunes}")
    multi = [e for e in index if len(e["tunes"]) > 1]
    print(f"PDFs with multiple tunes: {len(multi)}")
    with_composer = sum(1 for e in index for t in e["tunes"] if t["composer"])
    print(f"Tunes with composer info: {with_composer}")
    with_gaelic = sum(1 for e in index for t in e["tunes"] if t["gaelic"])
    print(f"Tunes with Gaelic title: {with_gaelic}")
    with_alt = sum(1 for e in index for t in e["tunes"] if t["alt_titles"])
    print(f"Tunes with alternate titles: {with_alt}")

    # Print samples
    print()
    print("Sample entries (first 3):")
    for entry in index[:3]:
        print(json.dumps(entry, indent=2, ensure_ascii=False))

    # Show a sample with gaelic
    print()
    print("Sample entry with Gaelic title:")
    for entry in index:
        for t in entry["tunes"]:
            if t["gaelic"]:
                print(json.dumps(entry, indent=2, ensure_ascii=False))
                break
        else:
            continue
        break

    # Show a sample with alt titles
    print()
    print("Sample entry with alternate titles:")
    for entry in index:
        for t in entry["tunes"]:
            if t["alt_titles"]:
                print(json.dumps(entry, indent=2, ensure_ascii=False))
                break
        else:
            continue
        break

    # Check for unreferenced PDFs on disk
    print()
    print("Checking for unreferenced PDFs on disk...")
    indexed_pdfs = set()
    for entry in index:
        indexed_pdfs.add(entry["pdf"].lower())

    unreferenced = []
    for root, dirs, files in os.walk(CONTENT_DIR):
        # Skip grafix directory
        rel_root = os.path.relpath(root, SITE_DIR)
        if "grafix" in rel_root.lower():
            continue
        for fname in files:
            if fname.lower().endswith(".pdf"):
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, SITE_DIR).replace("\\", "/")
                # Files on disk may have %20 in names; decode for comparison
                rel_decoded = unquote(rel)
                if rel_decoded.lower() not in indexed_pdfs:
                    unreferenced.append(rel)

    # Also check site root for PDFs
    for fname in os.listdir(SITE_DIR):
        if fname.lower().endswith(".pdf"):
            rel = fname
            rel_decoded = unquote(rel)
            if rel_decoded.lower() not in indexed_pdfs:
                unreferenced.append(rel)

    if unreferenced:
        unreferenced.sort()
        print(f"Found {len(unreferenced)} unreferenced PDF files:")
        for p in unreferenced:
            print(f"  {p}")
    else:
        print("All PDFs on disk are referenced in the index.")


if __name__ == "__main__":
    main()
