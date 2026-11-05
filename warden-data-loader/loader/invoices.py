"""Loads real receipts/invoices from SROIE and CORD into invoice_inbox as
OCR text plus their ground-truth labels. The text is what the Extraction
agent reads; the labels are the eval set for phase 2's promotion gate.
Images are not stored in phase 1 (the sandboxed OCR path arrives in phase 5)."""
import json
import logging
import os

from datasets import Image, load_dataset
from psycopg.types.json import Jsonb

from . import db

log = logging.getLogger("warden.loader")

LIMITS = {
    ("sroie", "train"): int(os.environ.get("SROIE_TRAIN_LIMIT", "200")),
    ("sroie", "test"): int(os.environ.get("SROIE_TEST_LIMIT", "60")),
    ("cord", "train"): int(os.environ.get("CORD_TRAIN_LIMIT", "100")),
    ("cord", "test"): int(os.environ.get("CORD_TEST_LIMIT", "40")),
}


def _text_only(name: str, split: str, columns: list[str]):
    # Phase 1 reads the OCR text and labels only. Phase 11c: asking the Parquet reader for
    # just those columns means the images' bytes are never read at all. With only
    # decode=False they still were, a few hundred images at a time, and on Azure the loader
    # ran out of its 2 GB and was killed (exit 137). If a dataset won't take the column
    # list, fall back to decode=False (remove_columns alone still decodes first).
    try:
        return load_dataset(name, split=split, streaming=True, columns=columns)
    except (TypeError, ValueError) as exc:
        log.warning("%s: can't read only %s (%s); reading every column", name, columns, exc)
    ds = load_dataset(name, split=split, streaming=True)
    if ds.column_names and "image" in ds.column_names:
        ds = ds.cast_column("image", Image(decode=False))
    return ds


def sroie_rows(split: str, limit: int):
    for i, rec in enumerate(_text_only("jsdnrs/ICDAR2019-SROIE", split, ["key", "words", "entities"])):
        if i >= limit:
            return
        words = rec.get("words") or []
        if not words:
            continue
        yield rec.get("key") or f"sroie-{split}-{i}", " ".join(words), {"entities": rec.get("entities") or {}}


def cord_rows(split: str, limit: int):
    for i, rec in enumerate(_text_only("naver-clova-ix/cord-v2", split, ["ground_truth"])):
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
