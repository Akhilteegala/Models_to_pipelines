"""Extract and classify PDF text for a reproducible, text-only RAG baseline.

Install: pip install pymupdf
Run: python pdf_loader.py /path/to/handbook.pdf --max-pages 25 --output-dir output

The default continuation rules were manually verified on the NASA Systems
Engineering Handbook. For other PDFs, omit --nasa-merges or provide custom rules.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re

import pymupdf

NASA_MERGES = [
    {"left_page": 16, "right_page": 17, "left_ends": "The systems engineer oversees the", "right_starts": "project’s systems engineering activities", "type": "paragraph"},
    {"left_page": 19, "right_page": 20, "left_ends": "those products that satisfy their", "right_starts": "design solutions and meet stakeholder expectations", "type": "list_item"},
    {"left_page": 23, "right_page": 24, "left_ends": "requirements are conveying; i.e., it", "right_starts": "may become more difficult to visualize", "type": "paragraph"},
    {"left_page": 24, "right_page": 24, "left_starts": "\uf0b7 Verification of a product", "right_starts": "meet each “shall” statement", "type": "list_item"},
]

INCLUDE_TYPES = {"heading_candidate", "paragraph_candidate", "paragraph", "list_candidate", "list_item"}


def clean_text(text: str) -> str:
    return " ".join(text.replace("\x00", "").replace("\xa0", " ").split()).strip()


def is_repeated_footer(text: str) -> bool:
    return bool(re.fullmatch(r"NASA Systems Engineering Handbook\s+Rev\s+2\s+[ivxlcdm\d]+", text, re.I))


def is_toc_entry(text: str) -> bool:
    return bool(re.match(r"^\d+(?:\.\d+)+\s+.+\.{5,}\s*\d*\s*$", text))


def extract_blocks(doc, max_pages: int) -> list[dict]:
    blocks = []
    for index in range(min(max_pages, len(doc))):
        for item in doc[index].get_text("blocks", sort=True):
            x0, y0, x1, y1, text, _block_no, block_type = item[:7]
            if block_type != 0 or not text.strip():
                continue
            text = clean_text(text)
            if not text or is_repeated_footer(text):
                continue
            blocks.append({"page": index + 1, "bbox": (x0, y0, x1, y1), "text": text})
    return blocks


def build_records(blocks: list[dict], merge_rules: list[dict], strict: bool = False) -> list[dict]:
    records = [
        {"pages": [b["page"]], "text": b["text"],
         "source_blocks": [{"page": b["page"], "bbox": b["bbox"]}],
         "type": "unclassified"}
        for b in blocks
    ]
    for rule_index, rule in enumerate(merge_rules):
        if rule_index == 3:
            for index, record in enumerate(records, start=1):
                record["record_id"] = f"nasa_{index:04d}"
        left_candidates = [r for r in records if r["pages"] == [rule["left_page"]]]
        right_candidates = [r for r in records if r["pages"] == [rule["right_page"]]]
        if "left_ends" in rule:
            left_candidates = [r for r in left_candidates if r["text"].endswith(rule["left_ends"])]
        if "left_starts" in rule:
            left_candidates = [r for r in left_candidates if r["text"].startswith(rule["left_starts"])]
        right_candidates = [r for r in right_candidates if r["text"].startswith(rule["right_starts"])]
        if len(left_candidates) != 1 or len(right_candidates) != 1 or left_candidates[0] is right_candidates[0]:
            message = f"Could not uniquely match merge rule: {rule}"
            if strict:
                raise ValueError(message)
            print("WARNING:", message)
            continue
        left, right = left_candidates[0], right_candidates[0]
        li, ri = records.index(left), records.index(right)
        if li >= ri:
            raise ValueError(f"Merge sources out of order: {rule}")
        merged = {
            **({"record_id": left["record_id"]} if "record_id" in left else {}),
            "pages": sorted(set(left["pages"] + right["pages"])),
            "text": left["text"].rstrip() + " " + right["text"].lstrip(),
            "source_blocks": left["source_blocks"] + right["source_blocks"],
            "type": rule["type"],
        }
        records[li] = merged
        records.pop(ri)
    for index, record in enumerate(records, start=1):
        record.setdefault("record_id", f"nasa_{index:04d}")
    return records


def get_font_info(page, bbox, page_dict=None):
    """Dominant span properties, weighted by visible character count."""
    page_dict = page_dict if page_dict is not None else page.get_text("dict")
    sizes, fonts = Counter(), Counter()
    target = pymupdf.Rect(bbox)
    for block in page_dict["blocks"]:
        if block["type"] != 0 or not pymupdf.Rect(block["bbox"]).intersects(target):
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if not pymupdf.Rect(span["bbox"]).intersects(target):
                    continue
                count = len(span["text"].strip())
                if count:
                    sizes[round(span["size"], 1)] += count
                    fonts[span["font"]] += count
    return {"font_size": sizes.most_common(1)[0][0] if sizes else None,
            "font": fonts.most_common(1)[0][0] if fonts else None}


def in_table(bbox, table_bboxes, threshold=0.5):
    rect = pymupdf.Rect(bbox)
    area = rect.get_area()
    if area <= 0:
        return False
    return any((rect & pymupdf.Rect(t)).get_area() / area >= threshold for t in table_bboxes)


def classify(record, doc, page_metadata):
    if record["type"] in {"paragraph", "list_item"}:
        return record["type"]
    text = record["text"].strip()
    source = record["source_blocks"][0]
    page_number, bbox = source["page"], source["bbox"]
    page = doc[page_number - 1]
    meta = page_metadata[page_number]
    if in_table(bbox, meta["table_bboxes"]):
        return "table_content"
    if is_toc_entry(text):
        return "toc_entry"
    font_info = get_font_info(page, bbox, meta["page_dict"])
    font_size = font_info["font_size"]
    font_name = (font_info["font"] or "").lower()
    if font_size is None:
        return "unknown"
    if font_size <= 10 and bbox[1] > page.rect.height * 0.75:
        return "footnote_candidate"
    if font_size >= 14 and "bold" in font_name and len(text) < 150:
        return "heading_candidate"
    if re.match(r"^Figure\s+\d+(?:\.\d+)*-\d+", text, re.I):
        return "figure_reference_candidate"
    if re.match(r"^[•\uf0b7]", text):
        return "list_candidate"
    if abs(font_size - 12) < 0.1 and len(text) > 150:
        return "paragraph_candidate"
    return "other"


def export_jsonl(records, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(pdf_path, output_dir="output", max_pages=25, nasa_merges=False, strict_merges=False):
    with pymupdf.open(pdf_path) as doc:
        blocks = extract_blocks(doc, max_pages)
        rules = NASA_MERGES if nasa_merges else []
        records = build_records(blocks, rules, strict=strict_merges)
        page_metadata = {}
        for page_number in range(1, min(max_pages, len(doc)) + 1):
            page = doc[page_number - 1]
            page_metadata[page_number] = {
                "page_dict": page.get_text("dict"),
                "table_bboxes": [tuple(table.bbox) for table in page.find_tables().tables],
            }
        for record in records:
            record["v4_type"] = classify(record, doc, page_metadata)
    retrieval = [
        {"record_id": r["record_id"], "pages": r["pages"], "text": r["text"],
         "content_type": r["v4_type"], "source_blocks": deepcopy(r["source_blocks"])}
        for r in records if r["v4_type"] in INCLUDE_TYPES
    ]
    output_dir = Path(output_dir)
    export_jsonl(records, output_dir / "nasa_structured_full.jsonl")
    export_jsonl(retrieval, output_dir / "nasa_retrieval_baseline.jsonl")
    print("Extracted text blocks:", len(blocks))
    print("Full dataset:", len(records))
    print("Retrieval dataset:", len(retrieval))
    print("Classification:", dict(Counter(r["v4_type"] for r in records)))
    return records, retrieval


def main():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "pdf_path",
        type=Path,
        nargs="?",
        default=Path(
            "/Users/akhilteegala/Projects/Models_to_pipelines/RAG/Data/NASA_HANDBOOK.pdf"
        ),
        help="Path to PDF file"
    )

    parser.add_argument("--max-pages", type=int, default=25)
    parser.add_argument("--output-dir", default="output")

    parser.add_argument(
        "--nasa-merges",
        action="store_true",
        help="Apply four manually verified NASA continuation merges"
    )

    parser.add_argument(
        "--strict-merges",
        action="store_true",
        help="Fail if a merge rule cannot be matched"
    )

    args = parser.parse_args()

    run(
        args.pdf_path,
        args.output_dir,
        args.max_pages,
        args.nasa_merges,
        args.strict_merges
    )


if __name__ == "__main__":
    main()
