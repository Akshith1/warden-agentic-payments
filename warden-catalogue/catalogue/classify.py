"""What the catalogue says about Warden's tables beyond their columns.

Descriptions come from Postgres comments written by the code that owns each
table, so they change with the schema. This adds what a schema can't say:

* personal data, tagged with OpenMetadata's PII classification, matching
  docs/data-map.md;
* which tables a critical data-quality check guards (a failure there stops
  payments), with a Warden classification;
* lineage: where a table's rows come from.

The job fails if a table or column named here isn't in the catalogue, so a
schema change can't leave the tags quietly pointing at nothing."""
from dataclasses import dataclass

LEDGER, BACKEND = "warden_ledger", "warden_backend"


@dataclass(frozen=True)
class ColumnTag:
    service: str
    table: str
    column: str
    tag: str
    why: str


@dataclass(frozen=True)
class Edge:
    service: str
    source: str
    target: str
    description: str


CLASSIFICATION = ("Warden", "Warden's own labels for its data.")
CRITICAL_TAG = ("CriticalCheck", "A critical Great Expectations check guards this table: if it fails, the kill "
                                 "switch stops all payments (phase 7b).")

COLUMN_TAGS = [
    ColumnTag(LEDGER, "historical_payments", "remit_account", "PII.Sensitive",
              "The account a payment went to; for a sole trader it is a person's bank account."),
    ColumnTag(BACKEND, "invoice_inbox", "text", "PII.Sensitive",
              "Receipt text as read: cashiers' names, phone numbers, emails. Masked before any model sees it."),
    ColumnTag(LEDGER, "vendor_aliases", "confirmed_by", "PII.NonSensitive",
              "The username of the person who confirmed the alias."),
]

# The tables warden-quality's critical checks read (see warden-quality/app/checks.py; a test keeps
# this list and those checks in step).
CRITICAL_TABLES = [(LEDGER, "postings"), (LEDGER, "transactions"), (LEDGER, "accounts"),
                   (LEDGER, "historical_payments")]

LINEAGE = [
    Edge(LEDGER, "postings", "accounts", "Each account's stored balance is the running sum of its postings."),
    Edge(LEDGER, "transactions", "historical_payments",
         "Each settled payment adds a vendor-history row; its external_ref is the transaction's idempotency_key."),
]


def fqn(service: str, table: str) -> str:
    # One database per service, named like the service (see infra/postgres-init).
    return f"{service}.{service}.public.{table}"


def apply(metadata) -> dict:
    """Applies everything above through an OpenMetadata client. Returns what it did."""
    from metadata.generated.schema.api.classification.createClassification import CreateClassificationRequest
    from metadata.generated.schema.api.classification.createTag import CreateTagRequest
    from metadata.generated.schema.api.lineage.addLineage import AddLineageRequest
    from metadata.generated.schema.entity.data.table import Table
    from metadata.generated.schema.type.entityLineage import EntitiesEdge, LineageDetails
    from metadata.generated.schema.type.entityReference import EntityReference
    from metadata.generated.schema.type.tagLabel import LabelType, State, TagLabel, TagSource
    from metadata.ingestion.models.table_metadata import ColumnTag as SdkColumnTag

    def label(tag_fqn: str) -> TagLabel:
        return TagLabel(tagFQN=tag_fqn, source=TagSource.Classification, labelType=LabelType.Manual,
                        state=State.Confirmed)

    tables: dict[str, object] = {}
    missing: list[str] = []

    def table(service: str, name: str):
        key = fqn(service, name)
        if key not in tables:
            tables[key] = metadata.get_by_name(entity=Table, fqn=key, fields=["columns", "tags"])
            if tables[key] is None:
                missing.append(key)
        return tables[key]

    done = {"column_tags": 0, "table_tags": 0, "lineage": 0}
    metadata.create_or_update(CreateClassificationRequest(name=CLASSIFICATION[0], description=CLASSIFICATION[1],
                                                          mutuallyExclusive=False))
    metadata.create_or_update(CreateTagRequest(classification=CLASSIFICATION[0], name=CRITICAL_TAG[0],
                                               description=CRITICAL_TAG[1]))

    for t in COLUMN_TAGS:
        entity = table(t.service, t.table)
        if entity is None:
            continue
        if t.column not in {c.name.root for c in entity.columns}:
            missing.append(f"{fqn(t.service, t.table)}.{t.column}")
            continue
        metadata.patch_column_tags(entity, [SdkColumnTag(column_fqn=f"{fqn(t.service, t.table)}.{t.column}",
                                                         tag_label=label(t.tag))])
        done["column_tags"] += 1

    for service, name in CRITICAL_TABLES:
        entity = table(service, name)
        if entity is not None:
            metadata.patch_tags(entity=Table, source=entity,
                                tag_labels=[label(f"{CLASSIFICATION[0]}.{CRITICAL_TAG[0]}")])
            done["table_tags"] += 1

    for e in LINEAGE:
        source, target = table(e.service, e.source), table(e.service, e.target)
        if source is None or target is None:
            continue
        metadata.add_lineage(AddLineageRequest(edge=EntitiesEdge(
            fromEntity=EntityReference(id=source.id, type="table"),
            toEntity=EntityReference(id=target.id, type="table"),
            lineageDetails=LineageDetails(description=e.description))))
        done["lineage"] += 1

    return done | {"missing": missing}
