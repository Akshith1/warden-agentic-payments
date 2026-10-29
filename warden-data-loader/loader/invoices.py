"""Loads real receipts/invoices from SROIE and CORD into invoice_inbox as
OCR text plus their ground-truth labels. The text is what the Extraction
agent reads; the labels are the eval set for phase 2's promotion gate.
Images are not stored in phase 1 (the sandboxed OCR path arrives in phase 5)."""
import json
import os

from datasets import Image, load_dataset
from psycopg.types.json import Jsonb

from . import db

LIMITS = {
    ("sroie", "train"): int(os.environ.get("SROIE_TRAIN_LIMIT", "200")),
    ("sroie", "test"): int(os.environ.get("SROIE_TEST_LIMIT", "60")),
    ("cord", "train"): int(os.environ.get("CORD_TRAIN_LIMIT", "100")),
    ("cord", "test"): int(os.environ.get("CORD_TEST_LIMIT", "40")),
}


def _text_only(name: str, split: str):
    # Phase 1 reads the OCR text and labels only. decode=False stops the library from
    # opening every image (remove_columns alone still decodes first). Images return
    # with the sandboxed OCR path in phase 5.
    ds = load_dataset(name, split=split, streaming=True)
    if ds.column_names and "image" in ds.column_names:
        ds = ds.cast_column("image", Image(decode=False))
    return ds


def sroie_rows(split: str, limit: int):
    for i, rec in enumerate(_text_only("jsdnrs/ICDAR2019-SROIE", split)):
        if i >= limit:
            return
        words = rec.get("words") or []
        if not words:
            continue
        yield rec.get("key") or f"sroie-{split}-{i}", " ".join(words), {"entities": rec.get("entities") or {}}


def cord_rows(split: str, limit: int):
    for i, rec in enumerate(_text_only("naver-clova-ix/cord-v2", split)):
        if i >= limit:
            return
        gt = json.loads(rec["ground_truth"])
        lines: dict[int, list[str]] = {}
        for line in gt.get("valid_line", []):
            for word in line.get("words", []):
                lines.setdefault(word.get("row_id", 0), []).append(word["text"])
        text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
        if text:
            yield f"cord-{split}-{i}", text, {"gt_parse": gt.get("gt_parse", {})}


def run() -> dict:
    counts = {}
    with db.backend_conn() as conn:
        conn.execute(db.INBOX_SCHEMA)
        for (source, split), limit in LIMITS.items():
            rows = sroie_rows(split, limit) if source == "sroie" else cord_rows(split, limit)
            n = 0
            for key, text, truth in rows:
                n += conn.execute(
                    """INSERT INTO invoice_inbox (source, external_key, split, text, ground_truth)
                       VALUES (%s,%s,%s,%s,%s) ON CONFLICT (external_key) DO NOTHING""",
                    (source, key, split, text, Jsonb(truth)),
                ).rowcount
            conn.commit()
            counts[f"{source}_{split}"] = n
            print(f"{source} {split}: {n} new documents")
    return counts
