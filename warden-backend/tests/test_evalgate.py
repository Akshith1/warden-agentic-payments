"""The gate's scoring rules, and the gate end to end against a real MLflow
server (set MLFLOW_TRACKING_URI to run that part; skipped otherwise)."""
import json
import os
import uuid

import pytest

from app import evalgate
from app.evalgate import date_match, score_fields, total_match, vendor_match
from app.schemas import InvoiceExtraction


def test_vendor_exact_and_case_insensitive():
    assert vendor_match("OJC Marketing Sdn Bhd", "OJC MARKETING SDN BHD")


def test_vendor_with_cashier_and_copy_stamp_is_a_miss():
    # The phase 1 live run: the model mixed the cashier's name and a COPY stamp into the vendor.
    assert not vendor_match("TAN CHAY YEE *** COPY *** OJC MARKETING SDN BHD", "OJC MARKETING SDN BHD")


def test_vendor_missing_is_a_miss():
    assert not vendor_match(None, "OJC MARKETING SDN BHD")
    assert not vendor_match("OJC MARKETING", "OJC MARKETING SDN BHD")  # 2 of 4 tokens


def test_dates_across_printed_formats():
    assert date_match("15/01/2019", "15/01/2019")
    assert date_match("2019-01-15", "15/01/2019")
    assert date_match("15 Jan 2019", "15-01-2019")
    assert date_match("15/01/2019 11:05:16 AM", "15/01/2019")
    assert not date_match("16/01/2019", "15/01/2019")
    assert not date_match(None, "15/01/2019")


def test_totals():
    assert total_match(193.0, "193.00")
    assert total_match("RM 1,193.00", "1193.00")
    assert not total_match(193.5, "193.00")
    assert not total_match(None, "193.00")


def test_only_labelled_fields_are_scored():
    checks = score_fields({"vendor_name": "A SDN BHD", "total_amount": 5.0}, {"company": "A SDN BHD", "total": "5.00"})
    assert checks == {"vendor": True, "total": True}


def test_high_average_with_a_bad_field_is_blocked():
    # The first live gate run: casual scored 0.85 overall with vendor at 0.60.
    casual = {"accuracy": 0.85, "per_field": {"vendor": 0.60, "date": 0.97, "total": 0.97}}
    structured = {"accuracy": 0.925, "per_field": {"vendor": 0.82, "date": 0.97, "total": 0.97}}
    assert evalgate.blocking_reasons(casual) == ["vendor 0.60 < floor 0.75"]
    assert evalgate.blocking_reasons(structured) == []


def test_low_average_is_blocked_even_with_every_field_above_the_floor():
    r = {"accuracy": 0.78, "per_field": {"vendor": 0.78, "date": 0.78, "total": 0.78}}
    assert evalgate.blocking_reasons(r) == ["accuracy 0.780 < 0.80"]


@pytest.mark.skipif(not os.environ.get("MLFLOW_TRACKING_URI"), reason="needs a running MLflow server")
def test_gate_promotes_the_passing_candidate_and_blocks_the_other(monkeypatch):
    receipts = [{"external_key": f"X{i}", "text": f"receipt {i}",
                 "labels": {"company": "OJC MARKETING SDN BHD", "date": "15/01/2019", "total": "193.00"}}
                for i in range(4)]
    good = InvoiceExtraction(vendor_name="OJC MARKETING SDN BHD", invoice_number=None, invoice_date="15/01/2019",
                             total_amount=193.0, remit_to_account=None, extraction_confidence=0.9)
    sloppy = good.model_copy(update={"vendor_name": "TAN CHAY YEE COPY OJC MARKETING SDN BHD",
                                     "invoice_date": None})
    texts = {path.read_text(): name for name, path in evalgate.CANDIDATES.items()}

    def fake_structured(agent, route, system, user, model):  # stands in for the LLM call only
        return good if texts[system] == "structured" else sloppy

    name = f"warden-extraction-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(evalgate.config, "EXTRACTION_PROMPT_NAME", name)
    monkeypatch.setattr(evalgate, "held_out_receipts", lambda n: receipts)
    monkeypatch.setattr(evalgate.llm, "structured", fake_structured)
    monkeypatch.setattr(evalgate, "PACE_SECONDS", 0)
    monkeypatch.setattr(evalgate.config, "MLFLOW_TRACKING_URI", os.environ["MLFLOW_TRACKING_URI"])

    assert evalgate.main() == 0

    import mlflow
    prod = mlflow.genai.load_prompt(f"prompts:/{name}@production")
    assert prod.template == evalgate.CANDIDATES["structured"].read_text()
    assert prod.tags["gate"] == "PROMOTED" and prod.tags["accuracy"] == "1.000"
    versions = {mlflow.genai.load_prompt(name, version=v).tags["candidate"]: mlflow.genai.load_prompt(name, version=v)
                for v in (1, 2)}
    assert versions["casual"].tags["gate"] == "BLOCKED"
    assert "vendor 0.00 < floor" in versions["casual"].tags["blocked_because"]
    assert json.loads(json.dumps(versions["casual"].tags))["accuracy"] == "0.333"  # total right; vendor, date missed


@pytest.mark.skipif(not os.environ.get("MLFLOW_TRACKING_URI"), reason="needs a running MLflow server")
def test_gate_leaves_production_alone_when_nothing_passes(monkeypatch):
    receipts = [{"external_key": "X1", "text": "r", "labels": {"company": "A SDN BHD", "total": "9.00"}}]
    wrong = InvoiceExtraction(vendor_name="B", invoice_number=None, invoice_date=None, total_amount=1.0,
                              remit_to_account=None, extraction_confidence=0.9)
    name = f"warden-extraction-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(evalgate.config, "EXTRACTION_PROMPT_NAME", name)
    monkeypatch.setattr(evalgate, "held_out_receipts", lambda n: receipts)
    monkeypatch.setattr(evalgate.llm, "structured", lambda *a: wrong)
    monkeypatch.setattr(evalgate, "PACE_SECONDS", 0)
    monkeypatch.setattr(evalgate.config, "MLFLOW_TRACKING_URI", os.environ["MLFLOW_TRACKING_URI"])

    assert evalgate.main() == 1
    import mlflow
    with pytest.raises(Exception):
        mlflow.genai.load_prompt(f"prompts:/{name}@production")


def test_provider_refusal_stops_the_gate_instead_of_scoring_misses(monkeypatch):
    # The second live gate run: Groq's daily token limit (429) mid-run.
    import httpx
    import openai
    from instructor.core.exceptions import InstructorRetryException

    calls = []

    def rate_limited(agent, route, system, user, model):
        calls.append(user)
        if len(calls) < 3:
            return InvoiceExtraction(vendor_name="A SDN BHD", invoice_number=None, invoice_date=None,
                                     total_amount=9.0, remit_to_account=None, extraction_confidence=0.9)
        req = httpx.Request("POST", "http://litellm:4000/chat/completions")
        cause = openai.RateLimitError("Rate limit reached ... tokens per day (TPD)",
                                      response=httpx.Response(429, request=req), body=None)
        raise InstructorRetryException("Error code: 429 - rate limit", n_attempts=1, total_usage=0) from cause

    receipts = [{"external_key": f"X{i}", "text": f"r{i}", "labels": {"company": "A SDN BHD", "total": "9.00"}}
                for i in range(10)]
    registered = []
    monkeypatch.setattr(evalgate, "held_out_receipts", lambda n: receipts)
    monkeypatch.setattr(evalgate.llm, "structured", rate_limited)
    monkeypatch.setattr(evalgate, "PACE_SECONDS", 0)
    monkeypatch.setattr(evalgate, "register", lambda *a: registered.append(a))
    monkeypatch.setattr(evalgate.config, "MLFLOW_TRACKING_URI", "http://unused")

    assert evalgate.main() == 3
    assert len(calls) == 3 and registered == []  # stopped at the first refusal; nothing registered


def test_a_model_that_never_validates_is_a_real_miss():
    from instructor.core.exceptions import InstructorRetryException

    def invalid(*_a):
        raise InstructorRetryException("1 validation error for InvoiceExtraction", n_attempts=3, total_usage=0)

    original = evalgate.llm.structured
    evalgate.llm.structured = invalid
    try:
        predicted, error = evalgate.extract("prompt", "doc")
    finally:
        evalgate.llm.structured = original
    assert predicted == {} and "validation error" in error
