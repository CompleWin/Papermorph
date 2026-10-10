#!/usr/bin/env python3
"""Extract a book by its page map, or preview a page range before a map exists.

    uv run --with pymupdf split_pages.py book.pdf --sections sections.json --out book_pages
    uv run --with pymupdf split_pages.py book.pdf --list            # check the map only
    uv run --with pymupdf split_pages.py book.pdf --only ch05       # one section
    uv run --with pymupdf split_pages.py book.pdf --text-only        # text layer only, no images
    uv run --with pymupdf split_pages.py book.pdf --pages 1-12       # preview/, no sections.json needed

Each section folder also gets text.md: the PDF's own text layer, page by page. Read that
first; open a page image only for diagrams, layout or text the layer gets wrong (it is far
cheaper than an image). Scanned books have no text layer, so text.md comes out empty.
Rendered folders also get sheet_NN.png: four pages at a time, 2x2, labelled with their PDF
page numbers. Look at the sheets for an overview; open a page image for a detail.

Files are named page_NNNN.png by PDF page order (from 1), never by printed page numbers.
The script refuses a map with gaps or overlaps, and refuses to overwrite existing images
unless --overwrite is given. The pages are a private reference for the author; they are
never published with the site.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import pymupdf

# Larger images are scaled down before a model sees them, so a bigger sheet only costs more.
SHEET_LONG_SIDE, SHEET_PIXELS = 1568, 1_150_000
SHEET_LABEL_PX, SHEET_GAP_PX = 16, 6


def write_sheets(doc, folder, first, last):
    """Write sheet_NN.png for pages first..last, four per sheet; return the file names."""
    names = []
    for start in range(first, last + 1, 4):
        numbers = list(range(start, min(start + 4, last + 1)))
        cell_w = max(doc[n - 1].rect.width for n in numbers)
        cell_h = max(doc[n - 1].rect.height for n in numbers)
        # Size labels and gaps for the pages alone, then fit the whole 2x2 sheet into the limits.
        zoom = min(SHEET_LONG_SIDE / (2 * max(cell_w, cell_h)), (SHEET_PIXELS / (4 * cell_w * cell_h)) ** .5)
        label, gap = SHEET_LABEL_PX / zoom, SHEET_GAP_PX / zoom
        row_h = 1.5 * label + cell_h
        full_w, full_h = 2 * cell_w + 3 * gap, 2 * row_h + 3 * gap
        zoom = .995 * min(SHEET_LONG_SIDE / max(full_w, full_h), (SHEET_PIXELS / (full_w * full_h)) ** .5)
        cols, rows = min(2, len(numbers)), (len(numbers) + 1) // 2      # a short last sheet keeps the scale
        with pymupdf.open() as sheet:
            canvas = sheet.new_page(width=cols * cell_w + (cols + 1) * gap, height=rows * row_h + (rows + 1) * gap)
            canvas.draw_rect(canvas.rect, color=None, fill=(.82, .82, .82))
            for slot, number in enumerate(numbers):
                x = gap + (slot % 2) * (cell_w + gap)
                y = gap + (slot // 2) * (row_h + gap)
                canvas.insert_text((x, y + 1.1 * label), f"page {number}", fontsize=label, color=(.1, .1, .1))
                source = doc[number - 1].rect
                fit = min(cell_w / source.width, cell_h / source.height)
                width, height = source.width * fit, source.height * fit
                left, top = x + (cell_w - width) / 2, y + 1.5 * label + (cell_h - height) / 2
                spot = pymupdf.Rect(left, top, left + width, top + height)
                canvas.draw_rect(spot, color=None, fill=(1, 1, 1))     # pages without a background stay white
                canvas.show_pdf_page(spot, doc, number - 1)
            image = canvas.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)
        target = folder / f"sheet_{len(names) + 1:02d}.png"
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(image.tobytes("png"))
        tmp.replace(target)
        names.append(target.name)
    for stale in folder.glob("sheet_*.png"):          # left from an earlier, longer map
        if stale.name not in names:
            stale.unlink()
    return names


def main():
    # Captured output (a pipe) uses the system code page on Windows; section titles may be any language.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--sections", type=Path, default=Path("sections.json"))
    ap.add_argument("--out", type=Path, default=Path("book_pages"))
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--list", action="store_true", help="print the map and stop")
    ap.add_argument("--only", nargs="+", help="render only these section folders")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--text-only", action="store_true", help="write text.md files, skip the images")
    ap.add_argument("--pages", help="preview a page or inclusive range (e.g. 3 or 1-12), independently of the map")
    a = ap.parse_args()
    with pymupdf.open(a.pdf) as doc:
        if a.pages:
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", a.pages)
            if not match:
                ap.error("--pages expects a page or range, e.g. 3 or 1-12")
            start, end = int(match[1]), int(match[2] or match[1])
            if not 1 <= start <= end <= len(doc):
                ap.error(f"--pages must be within 1-{len(doc)} in ascending order")
            if a.only:
                ap.error("use --pages for a preview or --only for mapped sections")
            sections = [{"folder": "preview", "title": f"Pages {start}-{end}", "start": start, "end": end}]
        else:
            sections = json.loads(a.sections.read_text(encoding="utf-8-sig"))   # PowerShell 5.1 writes a BOM
            nxt, seen = 1, set()
            for s in sections:
                f, st, en = s["folder"], s["start"], s["end"]
                if not f or "/" in f or "\\" in f or f in (".", "..") or f in seen:
                    ap.error(f"bad or repeated folder name: {f!r}")
                if st != nxt or en < st or en > len(doc):
                    ap.error(f"pages must run on without gaps or overlaps: {f} ({st}-{en}), expected start {nxt}")
                seen.add(f)
                nxt = en + 1
            if nxt != len(doc) + 1:
                ap.error(f"the map ends at page {nxt - 1}; the PDF has {len(doc)} pages")
            if a.only:
                unknown = set(a.only) - seen
                if unknown:
                    ap.error(f"unknown section folders: {', '.join(sorted(unknown))}")
        print(f"{len(doc)} pages, {len(sections)} sections, {a.dpi} dpi")
        if a.list:
            for s in sections:
                print(f'{s["folder"]}: {s["start"]}-{s["end"]}  {s["title"]}')
            return
        todo = [s for s in sections if not a.only or s["folder"] in a.only]
        for s in todo:
            d = a.out / s["folder"]
            d.mkdir(parents=True, exist_ok=True)
            text = [f"## page {p}\n\n{doc[p - 1].get_text().strip()}\n" for p in range(s["start"], s["end"] + 1)]
            (d / "text.md").write_text(f"# {s['title']}\n\n" + "\n".join(text), encoding="utf-8")
            if a.text_only:
                continue
            for p in range(s["start"], s["end"] + 1):
                target = d / f"page_{p:04d}.png"
                if target.exists() and not a.overwrite:
                    continue
                tmp = target.with_suffix(".tmp")     # never leave half an image behind
                tmp.write_bytes(doc[p - 1].get_pixmap(dpi=a.dpi, colorspace=pymupdf.csRGB, alpha=False).tobytes("png"))
                tmp.replace(target)
            sheets = write_sheets(doc, d, s["start"], s["end"])
            listed = sheets[0] if len(sheets) == 1 else f"{sheets[0]} .. {sheets[-1]}"
            print(f'{s["folder"]}: {s["start"]}-{s["end"]} done; open {listed} first (four pages per sheet)', flush=True)
        manifest = {"source": a.pdf.name, "page_count": len(doc), "dpi": a.dpi,
                    "page_numbering": "PDF page order from 1, not printed page numbers",
                    "filename_pattern": "page_{pdf_page:04d}.png", "sections": sections}
        manifest_dir = a.out / "preview" if a.pages else a.out
        (manifest_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
