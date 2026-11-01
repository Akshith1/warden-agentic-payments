import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    description: str
    quantity: float | None = None
    amount: float = Field(description="Line total as a plain number")


class InvoiceExtraction(BaseModel):
    """Typed output of the Extraction agent."""
    vendor_name: str | None = Field(description="Seller or merchant name exactly as printed, or null")
    invoice_number: str | None = Field(description="Invoice or receipt number, or null")
    invoice_date: str | None = Field(description="Date as printed on the document, or null")
    currency: str | None = Field(default=None, description="ISO 4217 code if the document shows one, else null")
    total_amount: float | None = Field(description="Final amount payable as a plain number, or null")
    line_items: list[LineItem] = Field(default_factory=list)
    remit_to_account: str | None = Field(description="Bank account or remittance details for payment, or null")
    extraction_confidence: float = Field(ge=0, le=1, description="How sure you are the fields are correct")


class RiskAssessment(BaseModel):
    """Typed output of the Risk Scoring agent."""
    risk_level: Literal["low", "medium", "high"]
    risk_score: float = Field(ge=0, le=1)
    signals: list[str] = Field(description="Specific observations that drove the score")
    rationale: str


class ApprovalDecision(BaseModel):
    """Typed output of the Approval agent. confidence is about the decision,
    not about the extracted facts underneath it."""
    decision: Literal["auto_approve", "escalate", "reject"]
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]


class CriticReview(BaseModel):
    """Typed output of the Critic, run on the other provider."""
    agrees: bool
    concerns: list[str]


Status = Literal[
    "submitted", "extracted", "scored", "pending_human",
    "approved", "settling", "rejected", "blocked", "settled",
]  # follows the A2A TaskState idea: one explicit lifecycle state per run.
# The runs table adds service states outside the graph: resuming, failed (a provider or
# infrastructure error; retryable) and retrying.


class RunState(TypedDict, total=False):
    run_id: str
    invoice_text: str
    source: str
    status: Status
    status_reason: str
    extraction: dict
    vendor: dict
    risk: dict
    approval: dict
    critic: dict
    escalation_reasons: list[str]
    human: dict
    payment: dict
    audit_trail: Annotated[list[dict], operator.add]
