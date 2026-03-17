#!/usr/bin/env python3
"""
Build browse.json — a structured index of all PDFs organized by collection/book
for the browse feature of the Ceol Sean Pipe Music Library.

Walks ceolsean.net/content/ to find ALL PDFs (not just indexed ones),
groups them by collection directory and book directory, natural-sorts
filenames, and maps tune information from index.json.
"""

import json
import os
import re
import sys


def natural_sort_key(s):
    """Sort key that handles embedded numbers naturally.
    'Book01 2.pdf' < 'Book01 10.pdf'
    Also handles 'a'/'b' suffixes: '5a' comes after '5' but before '6'.
    """
    parts = re.split(r'(\d+)', s)
    result = []
    for part in parts:
        if part.isdigit():
            result.append((0, int(part), ''))
        else:
            result.append((1, 0, part.lower()))
    return result


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    content_dir = os.path.join(base_dir, 'ceolsean.net', 'content')
    index_path = os.path.join(base_dir, 'index.json')
    output_path = os.path.join(base_dir, 'browse.json')

    # Load index.json for tune info and collection name mapping
    with open(index_path, 'r') as f:
        index_data = json.load(f)

    # Build a lookup: pdf path -> list of tune names
    pdf_to_tunes = {}
    for entry in index_data:
        pdf = entry['pdf']  # e.g. "content/AGlen/Book01/Book01 14.pdf"
        tune_names = [t['name'] for t in entry['tunes']]
        pdf_to_tunes[pdf] = tune_names

    # Build a lookup: directory name (lowercase) -> primary collection name
    # For directories with multiple collections, pick the best match:
    # 1. Prefer non-"Broadside" collections
    # 2. Among remaining, pick the most frequent collection for that directory
    from collections import Counter
    dir_coll_counts = {}  # dir_lower -> Counter of (coll_name, coll_short)
    for entry in index_data:
        coll_dir = entry['pdf'].split('/')[1].lower()
        key = (entry['collection'], entry['collection_short'])
        if coll_dir not in dir_coll_counts:
            dir_coll_counts[coll_dir] = Counter()
        dir_coll_counts[coll_dir][key] += 1

    dir_to_collection_name = {}
    meta_short = {'Broadside'}  # "Broadside to Broadband" is a meta-collection
    for dir_lower, counts in dir_coll_counts.items():
        # Filter out Broadside if there are other options
        non_meta = {k: v for k, v in counts.items() if k[1] not in meta_short}
        if non_meta:
            best = max(non_meta, key=lambda k: non_meta[k])
        else:
            best = max(counts, key=lambda k: counts[k])
        dir_to_collection_name[dir_lower] = best

    # Walk the content directory
    collections = {}
    for coll_dir_name in sorted(os.listdir(content_dir)):
        coll_path = os.path.join(content_dir, coll_dir_name)
        if not os.path.isdir(coll_path):
            continue
        # Skip non-book directories
        if coll_dir_name == 'grafix':
            continue

        books = {}
        for book_dir_name in sorted(os.listdir(coll_path)):
            book_path = os.path.join(coll_path, book_dir_name)
            if not os.path.isdir(book_path):
                continue

            # Find all PDFs in this book directory
            pdfs = []
            for fname in os.listdir(book_path):
                if fname.lower().endswith('.pdf'):
                    # Build the relative path as used in index.json
                    rel_path = f"content/{coll_dir_name}/{book_dir_name}/{fname}"
                    tunes = pdf_to_tunes.get(rel_path, [])
                    pdfs.append({
                        'file': rel_path,
                        'tunes': tunes
                    })

            # Natural sort by filename
            pdfs.sort(key=lambda p: natural_sort_key(os.path.basename(p['file'])))

            if pdfs:
                books[book_dir_name] = pdfs

        if books:
            # Format book name nicely: "Book01" -> "Book 01", "book03" -> "Book 03"
            coll_info = dir_to_collection_name.get(coll_dir_name.lower(), (coll_dir_name, coll_dir_name))
            collections[coll_dir_name] = {
                'name': coll_info[0],
                'short_name': coll_info[1],
                'dir': coll_dir_name,
                'books': []
            }
            for book_dir_name in sorted(books.keys(), key=natural_sort_key):
                # Create a nice display name
                display_name = re.sub(
                    r'[Bb]ook\s*(\d+)',
                    lambda m: f"Book {int(m.group(1)):02d}",
                    book_dir_name
                )
                collections[coll_dir_name]['books'].append({
                    'name': display_name,
                    'dir': book_dir_name,
                    'pages': books[book_dir_name]
                })

    # Build final output sorted by collection short name
    output = {
        'collections': sorted(
            collections.values(),
            key=lambda c: c['short_name'].lower()
        )
    }

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    # Print summary
    total_books = sum(len(c['books']) for c in output['collections'])
    total_pages = sum(
        len(b['pages'])
        for c in output['collections']
        for b in c['books']
    )
    print(f"Generated {output_path}")
    print(f"  {len(output['collections'])} collections, {total_books} books, {total_pages} pages")


if __name__ == '__main__':
    main()
