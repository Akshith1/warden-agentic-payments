import os

LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
# Phase 1 uses one gateway key; phase 8 replaces it with one budget-capped key per agent.
LITELLM_API_KEY = os.environ.get("LITELLM_API_KEY", "")

ROUTE_REASONING = "warden-reasoning"
ROUTE_ROUTINE = "warden-routine"

LEDGER_URL = os.environ.get("LEDGER_URL", "http://warden-ledger:8000")
LEDGER_WRITE_KEY = os.environ.get("LEDGER_WRITE_KEY", "")
VENDOR_LOOKUP_KEY = os.environ.get("VENDOR_LOOKUP_KEY", "")

ROUTINE_AMOUNT_THRESHOLD = float(os.environ.get("ROUTINE_AMOUNT_THRESHOLD", "5000"))
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
LINE_ITEM_TOLERANCE = float(os.environ.get("LINE_ITEM_TOLERANCE", "0.01"))
SETTLEMENT_HOLD_SECONDS = int(os.environ.get("SETTLEMENT_HOLD_SECONDS", "20"))
LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "3"))

CASH_ACCOUNT = "1000-operating-cash"
EXPENSE_ACCOUNT = "5000-vendor-expense"


def backend_dsn() -> str:
    return (
        f"postgresql://warden_backend:{os.environ['PGPASS_WARDEN_BACKEND']}"
        f"@{os.environ.get('POSTGRES_HOST', 'postgres')}:{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ.get('BACKEND_DB', 'warden_backend')}"
    )
