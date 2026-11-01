from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class Posting(BaseModel):
    account_id: str
    direction: Literal["debit", "credit"]
    amount: Decimal = Field(decimal_places=2)


class TransactionRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    description: str
    postings: list[Posting]


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str]
    resulting_balances: dict[str, Decimal]
    transaction_id: int | None = None


class RecordPaymentRequest(BaseModel):
    vendor_name: str
    amount: Decimal = Field(gt=0, decimal_places=2)
    paid_on: date
    external_ref: str
    remit_account: str | None = None


class VendorLookup(BaseModel):
    known: bool
    vendor_name: str
    vendor_id: int | None = None
    matched_alias: str | None = None  # set when the name was found through a confirmed alias
    first_seen: date | None = None
    payment_count: int = 0
    average_amount: Decimal | None = None
    recent_amounts: list[Decimal] = []
    last_remit_account: str | None = None


class VendorCandidate(BaseModel):
    vendor_id: int
    vendor_name: str
    payment_count: int
    last_remit_account: str | None = None
    aliases: list[str] = []


class AliasRequest(BaseModel):
    alias: str = Field(min_length=1, max_length=300)
    vendor_id: int
    confirmed_by: str = Field(min_length=1, max_length=200)
    run_ref: str | None = None
