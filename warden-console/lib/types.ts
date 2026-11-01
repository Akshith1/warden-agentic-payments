export type RunRow = {
  run_id: string;
  status: string;
  status_reason: string | null;
  vendor_name: string | null;
  total_amount: string | number | null;
  source: string;
  created_at: string;
  updated_at: string;
};

export type TrailEntry = { step: string; at: string; [k: string]: unknown };

export type Extraction = {
  vendor_name: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  currency: string | null;
  currency_assumed?: boolean;
  total_amount: number | null;
  line_items: { description: string; quantity: number | null; amount: number }[];
  remit_to_account: string | null;
  extraction_confidence: number;
};

export type RunDetail = {
  run: RunRow | null;
  state: {
    run_id: string;
    invoice_text: string;
    status: string;
    status_reason?: string;
    extraction?: Extraction;
    vendor?: { known: boolean; payment_count: number; average_amount: string | null; last_remit_account: string | null };
    risk?: { risk_level: string; risk_score: number; signals: string[]; rationale: string };
    approval?: { decision: string; confidence: number; reasons: string[] };
    critic?: { agrees: boolean; concerns: string[] };
    escalation_reasons?: string[];
    human?: { decision: string; approver: string; note?: string };
    payment?: { transaction_id?: number; amount?: string; idempotency_key?: string };
    audit_trail: TrailEntry[];
  };
  next: string[];
  awaiting_human: unknown[];
};

export type InboxRow = {
  external_key: string;
  preview: string;
  labelled_vendor: string | null;
  labelled_total: string | null;
};
