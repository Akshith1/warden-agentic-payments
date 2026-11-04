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

import tldextract
import tldextract.tldextract as _tld
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
                         # up to five words, one space apart: a name ends at a wider gap or the line's end
                         r"[A-Za-z][A-Za-z.']*(?: [A-Za-z][A-Za-z.']*){0,4}", 0.9)
_ACCOUNT = Pattern("bank account", r"(?i)(?<=\b(?:account|acct|a/c)(?:\s*(?:no|number))?\.?\s*[:#]?\s*)"
                                   r"\d[\d\- ]{6,22}\d", 0.9)
# Anything shaped like an email address. Presidio's own email check also requires the domain
# ending to be a real public one (.com, .my), so addresses on reserved or internal endings
# (.example, .internal, .local) slipped through to the models. For masking, the shape is enough.
_EMAIL = Pattern("email address", r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+\b", 0.9)
_NRIC = Pattern("Malaysian IC", r"\b\d{6}-\d{2}-\d{4}\b", 0.85)

# Phase 11a live run: on an invoice's item table spaCy's small model read "Copier Paper" and
# "Toner Cartridge" as people, "Subang Jaya" (a town) as a person, and Presidio read the amount
# 168.00 as a phone number. The extraction agent then lost that line. These keep the table and
# the business's address readable; names after a label ("Cashier:", "Attn:") are still masked.
_MONEY = re.compile(r"^\(?-?(?:RM|MYR|\$)?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\)?$|^\d+\.\d{2}$", re.I)
_ITEM_ROW = re.compile(r"\d+\.\d{2}\s*$")                       # a line ending in an amount
_POSTCODE = re.compile(r"\b\d{5}\b")                              # an address line (Malaysian and US postcodes)
_COMPANY = re.compile(r"\b(?:co|company|corp|corporation|inc|ltd|limited|llc|plc|bhd|berhad|sdn|enterprise|"
                      r"trading|holdings|group|gmbh)\.?$", re.I)


# Presidio's email check (replaced below) asks tldextract whether a domain is real. By default
# tldextract keeps a cache on disk (and takes a lock file even to read it) and may fetch the
# list of domains from the internet. This service has a read-only filesystem and no internet
# route, so if anything still calls it, it uses the copy of the list that ships inside
# tldextract: no cache, no lock, no network.
_tld.TLD_EXTRACTOR = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=())


def build_analyzer() -> AnalyzerEngine:
    nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    analyzer.registry.remove_recognizer("EmailRecognizer")
    analyzer.registry.add_recognizer(PatternRecognizer(supported_entity="EMAIL_ADDRESS", patterns=[_EMAIL],
                                                       name="AnyDomainEmailRecognizer"))
    analyzer.registry.add_recognizer(PatternRecognizer(supported_entity="PERSON", patterns=[_LABELLED_NAME],
                                                       name="LabelledNameRecognizer"))
    analyzer.registry.add_recognizer(PatternRecognizer(supported_entity="BANK_ACCOUNT", patterns=[_ACCOUNT],
                                                       name="BankAccountRecognizer"))
    analyzer.registry.add_recognizer(PatternRecognizer(supported_entity="MY_NRIC", patterns=[_NRIC],
                                                       name="MalaysianNricRecognizer"))
    return analyzer


def _line(text: str, r: RecognizerResult) -> str:
    start = text.rfind("\n", 0, r.start) + 1
    end = text.find("\n", r.end)
    return text[start:end if end != -1 else len(text)]


def _keep(r: RecognizerResult, text: str) -> bool:
    if r.entity_type not in MASKED or r.score < MIN_SCORE.get(r.entity_type, 0.5):
        return False
    value = text[r.start:r.end].strip()
    if r.entity_type == "PHONE_NUMBER":
        if _MONEY.match(value) or len(re.sub(r"\D", "", value)) < 7:
            return False
    if r.entity_type == "PERSON":
        words = re.findall(r"[A-Za-z]+", value)
        if not words or all(w.lower() in NOT_NAMES for w in words) or any(c.isdigit() for c in value):
            return False
        if _COMPANY.search(value):
            return False
        guessed = (r.recognition_metadata or {}).get("recognizer_name") == "SpacyRecognizer"
        line = _line(text, r)
        if guessed and (_ITEM_ROW.search(line) or _POSTCODE.search(line)):
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
