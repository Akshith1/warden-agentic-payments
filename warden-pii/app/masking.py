"""Finds personal data in invoice text and swaps it for placeholders.

Presidio does the finding: its built-in pattern recognizers (emails, phone
numbers, card numbers, IBANs, US SSNs, IP addresses), spaCy's named-entity
model for people's names, and recognizers added here for what receipts
carry: names after a label ("Cashier:", "Attn:"), bank account numbers after
"account", and Malaysian identity card numbers.

Only personal data is masked. Dates, amounts, addresses of businesses and
company names stay, because the agents need them and they identify a
business, not a person. Each value becomes a placeholder such as <PERSON_1>,
the same value always gets the same placeholder within one document, and the
mapping goes back to the caller, so it can put the real values back after
the model has answered. This service keeps nothing."""
import re

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider

MASKED = {"PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "IBAN_CODE", "US_SSN", "IP_ADDRESS",
          "BANK_ACCOUNT", "MY_NRIC"}
MIN_SCORE = {"PERSON": 0.85, "PHONE_NUMBER": 0.4}  # others: 0.5
# Words spaCy's small model mistakes for names on receipts.
NOT_NAMES = {"tel", "fax", "cashier", "total", "subtotal", "gst", "sst", "tax", "invoice", "receipt", "qty", "amount",
             "change", "cash", "rounding", "thank", "you", "no", "date", "time", "copy", "member", "points",
             "customer", "ic", "nric", "id", "phone", "email", "account", "card", "bank"}

_LABELLED_NAME = Pattern("labelled name",
                         r"(?im)(?<=\b(?:cashier|customer|attn|attention|salesperson|served by|bill to|name)\s*[:\-]\s*)"
                         r"[A-Za-z][A-Za-z.' ]{1,40}[A-Za-z]", 0.9)
_ACCOUNT = Pattern("bank account", r"(?i)(?<=\b(?:account|acct|a/c)(?:\s*(?:no|number))?\.?\s*[:#]?\s*)"
                                   r"\d[\d\- ]{6,22}\d", 0.9)
_NRIC = Pattern("Malaysian IC", r"\b\d{6}-\d{2}-\d{4}\b", 0.85)


def build_analyzer() -> AnalyzerEngine:
    nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    analyzer.registry.add_recognizer(PatternRecognizer(supported_entity="PERSON", patterns=[_LABELLED_NAME],
                                                       name="LabelledNameRecognizer"))
    analyzer.registry.add_recognizer(PatternRecognizer(supported_entity="BANK_ACCOUNT", patterns=[_ACCOUNT],
                                                       name="BankAccountRecognizer"))
    analyzer.registry.add_recognizer(PatternRecognizer(supported_entity="MY_NRIC", patterns=[_NRIC],
                                                       name="MalaysianNricRecognizer"))
    return analyzer


def _keep(r: RecognizerResult, text: str) -> bool:
    if r.entity_type not in MASKED or r.score < MIN_SCORE.get(r.entity_type, 0.5):
        return False
    value = text[r.start:r.end].strip()
    if r.entity_type == "PERSON":
        words = re.findall(r"[A-Za-z]+", value)
        if not words or all(w.lower() in NOT_NAMES for w in words) or any(c.isdigit() for c in value):
            return False
    return True


def _non_overlapping(results: list[RecognizerResult]) -> list[RecognizerResult]:
    chosen: list[RecognizerResult] = []
    for r in sorted(results, key=lambda r: (-(r.end - r.start), -r.score)):  # longest, then surest, wins
        if all(r.end <= c.start or r.start >= c.end for c in chosen):
            chosen.append(r)
    return sorted(chosen, key=lambda r: r.start)


def mask(analyzer: AnalyzerEngine, text: str) -> dict:
    found = [r for r in analyzer.analyze(text=text, language="en") if _keep(r, text)]
    tokens: dict[str, str] = {}      # original -> placeholder
    counts: dict[str, int] = {}
    pieces, last = [], 0
    for r in _non_overlapping(found):
        value = text[r.start:r.end]
        key = value.strip()
        if key not in tokens:
            counts[r.entity_type] = counts.get(r.entity_type, 0) + 1
            tokens[key] = f"<{r.entity_type}_{counts[r.entity_type]}>"
        pieces += [text[last:r.start], text[r.start:r.end].replace(key, tokens[key])]
        last = r.end
    pieces.append(text[last:])
    return {"masked": "".join(pieces), "mapping": {t: v for v, t in tokens.items()}, "counts": counts}
