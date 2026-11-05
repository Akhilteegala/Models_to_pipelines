# Structure-Aware PDF Loader for RAG

A hands-on learning project exploring how PDF extraction, document layout, and content classification affect the quality of text prepared for retrieval-augmented generation (RAG).

> **Status:** Experimental, document-specific baseline. This project currently prepares structured JSONL records; it does **not** yet implement chunking, embeddings, a vector database, or answer generation.

## Why this project?

My initial plan was to load a PDF using a LangChain PDF loader and pass the resulting text to a chunker. LangChain's PDF loaders are useful abstractions, but I wanted to inspect the underlying extraction process and control what information was retained. I therefore experimented directly with **PyMuPDF**.

The test document is the **NASA Systems Engineering Handbook (Rev. 2)**. Its mix of a table of contents, headings, repeated footers, footnotes, tables, diagrams, and cross-page text continuations makes it a useful case study. The initial experiment processes **25 PDF pages**, not the entire handbook.

## Pipeline

```text
PDF
  |
  v
PyMuPDF text blocks + bounding boxes
  |
  v
Text normalization + known repeated-footer removal
  |
  v
Verified paragraph / list continuation merges
  |
  v
Font metadata (blocks -> lines -> spans) + table regions
  |
  v
Rule-based content classification
  |
  +--> Full structured JSONL
  |
  +--> Conservative text-only retrieval JSONL
```

## What I learned, step by step

### 1. Extracting text blocks

I started with `page.get_text("blocks", sort=True)` to inspect whether PyMuPDF could identify text regions. Each extracted text block includes its bounding box `(x0, y0, x1, y1)`, text, and block type. I retained text blocks and their page references.

**Learning:** A block is a spatial grouping, not a guaranteed semantic paragraph. `sort=True` provides an approximate reading order, which may still be imperfect for complex layouts.

### 2. Cleaning extracted text

I normalized whitespace, replaced nonbreaking spaces, removed null characters, and filtered the handbook's known repeated footer pattern. I kept source coordinates so that cleaned text remained traceable to the PDF.

**Learning:** Cleaning makes text more consistent, but indiscriminate whitespace normalization can erase meaningful list or table formatting. Regular expressions can recognize known patterns; they do not inherently understand document structure.

### 3. Reconstructing continuations

I discovered that some sentences or list items continued across blocks and even across pages. For this experiment, I **manually verified four merge rules**: three cross-page continuations and one same-page list continuation. The script applies these explicit rules when `--nasa-merges` is enabled and retains the original source-block references.

**Learning:** A text block boundary is not necessarily a semantic boundary. This is **not** a generalized automatic continuation detector; the merge rules are specific to the tested handbook pages.

### 4. First classifier: text patterns and length

The first version used regular expressions, bullet markers, and a character-length threshold (for example, more than 150 characters as a paragraph candidate).

This misclassified a long footnote as a paragraph. It also treated some table-of-contents entries as headings.

**Learning:** Length and text patterns are useful clues, not reliable standalone content labels.

### 5. Inspecting typography using spans

I then used `page.get_text("dict")`, which exposes a richer hierarchy:

```text
Page -> Blocks -> Lines -> Spans
```

Spans provide text, font name, font size, and bounding boxes. On the examined handbook page, I observed:

| Content | Observed font | Additional signal |
| --- | --- | --- |
| Section heading | 14 pt Arial Bold | Heading-like text |
| Body text | 12 pt Times New Roman | Main text region |
| Footnote | 10 pt Times New Roman | Near bottom of page |
| Footnote markers | 6.5 pt | Small reference numbers |
| Superscript references | 8 pt | Embedded in body lines |
| Repeated footer | 12 pt Times New Roman | Repeated text near page bottom |

**Learning:** Typography improves classification, but the footer and body can share the same font size. The script combines text, font, and location signals; the observed font thresholds are **document-specific heuristics**, not universal PDF rules.

### 6. Discovering false footnotes in tables

The layout-aware classifier initially labeled several 10 pt table cells near the bottom of pages 20 and 22 as footnotes. I tested `page.find_tables()` and found one detected table on each of those pages, while the inspected footnote page had none.

The next version classifies text overlapping detected table regions **before** applying footnote heuristics. The overlap threshold is currently 50% of the text block's area.

**Learning:** Context matters. A 10 pt text block near the page bottom may be a footnote, a table cell, or a diagram label. Table detection helps, but imperfect table bounding boxes and continuations remain limitations.

### 7. Distinguishing TOC entries from headings

Numbered entries with dotted leaders were initially mistaken for headings. A dedicated TOC pattern correctly matched **22 of 22 inspected changed records**. This was a targeted validation, not a comprehensive accuracy measurement.

**Learning:** An entry *referring to* a section is not the same as the section heading in the document body.

### 8. Preserving context for retrieval

A product-verification bullet was split across two text blocks. Merging the verified continuation kept the full statement together rather than leaving an isolated sentence fragment.

**Learning:** The goal is not merely readable text; it is text with enough surrounding meaning to support downstream retrieval.

## Experimental results

The notebook experiment produced the following counts after the verified merges and classification:

| Output | Records |
| --- | ---: |
| Full structured dataset | **373** |
| Text-only retrieval baseline | **69** |

The full dataset retained these labels:

| Label | Count |
| --- | ---: |
| `other` | 172 |
| `toc_entry` | 83 |
| `paragraph_candidate` | 44 |
| `table_content` | 44 |
| `heading_candidate` | 13 |
| `list_candidate` | 8 |
| `figure_reference_candidate` | 5 |
| `paragraph` | 2 |
| `list_item` | 2 |
| `footnote_candidate` | 1 |

The 69-record baseline selects `heading_candidate`, `paragraph_candidate`, `paragraph`, `list_candidate`, and `list_item`. This is intentionally conservative: it excludes TOC entries, tables, figure references, footnotes, and `other` records from the **retrieval baseline**, not from the full preserved dataset. Some excluded records contain meaningful text.

> **Reproducibility note:** These numbers are the observed notebook results, not guaranteed outputs for every script run. The standalone script uses character-weighted font statistics and applies its configured merges during record construction; validate its counts and merged content against the notebook before claiming exact parity.

## Project files

```text
project/
├── pdf_loader.py
├── README.md
├── Data/
│   └── NASA_HANDBOOK.pdf       # Supply your own PDF
└── output/                     # Generated when the script runs
    ├── nasa_structured_full.jsonl
    └── nasa_retrieval_baseline.jsonl
```

## Requirements

- Python 3.10+
- PyMuPDF

```bash
pip install pymupdf
```

## Usage

From the project directory:

```bash
python pdf_loader.py Data/NASA_HANDBOOK.pdf \
  --max-pages 25 \
  --nasa-merges \
  --strict-merges \
  --output-dir output
```

`pdf_path` is a positional argument in the supplied script version. If you modify `main()` to provide a default PDF path, it can be omitted. `--nasa-merges` enables the four handbook-specific verified continuations; `--strict-merges` fails rather than silently proceeding when a rule cannot be matched. Omit the NASA merge flag for unrelated PDFs.

## Output schema

A record in the retrieval JSONL has this shape (illustrative values):

```json
{
  "record_id": "nasa_0225",
  "pages": [16],
  "text": "2.0 Fundamentals of Systems Engineering",
  "content_type": "heading_candidate",
  "source_blocks": [
    {"page": 16, "bbox": [72.0, 84.4, 400.0, 101.0]}
  ]
}
```

The full JSONL uses `v4_type` for the current classifier output and also retains the original `type` field. Bounding-box values above are **illustrative**, not an exact transcription from the handbook.

Inspect the outputs in a notebook:

```python
import json
import pandas as pd

with open("output/nasa_retrieval_baseline.jsonl", encoding="utf-8") as f:
    records = [json.loads(line) for line in f]

df = pd.DataFrame(records)
display(df[["record_id", "pages", "content_type", "text"]])
```

## Limitations and known issues

1. **Not a universal classifier.** Font thresholds, footer patterns, and TOC regexes are tailored to the tested handbook.
2. **Candidate labels are not ground truth.** The counts above are distributions, not accuracy or precision scores.
3. **Tables are region-tagged, not reconstructed.** `find_tables()` may miss borderless tables or part of a continued table; cell structure is not yet exported.
4. **Figures and diagrams are not semantically extracted.** PyMuPDF can expose images and vector graphics, but this baseline does not caption, interpret, or embed them. Diagram labels may still be misclassified.
5. **Cross-page merging is manual.** Other split paragraphs may remain unresolved.
6. **Short meaningful text may be excluded.** A character-length threshold can miss valid short paragraphs or continuations.
7. **Reading order and metadata are imperfect.** Multi-column layouts and overlapping regions may require further processing.
8. **The experiment covers only 25 pages.** Results should not be generalized to the complete handbook without additional tests.

## Next experiments

- Compare fixed-size chunking with paragraph/heading-aware chunking.
- Infer dominant body font sizes dynamically instead of hardcoding 12/14/10 pt.
- Reconstruct table rows, columns, and multi-page continuations.
- Improve diagram and figure handling.
- Create manually labeled evaluation samples and measure classification quality.
- Generate embeddings and compare retrieval relevance across baseline variants.
- Add tests and a reusable configuration interface for other PDFs.

## Key takeaway

**Extracting text is not the same as preserving meaning.** For RAG, layout, typography, reading order, and continuation boundaries can materially affect what information is retrieved. This project documents an iterative, evidence-driven approach to building a better ingestion baseline, including the cases where the rules still fail.
