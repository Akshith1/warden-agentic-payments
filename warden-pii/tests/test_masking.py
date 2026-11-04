"""Real Presidio and the real spaCy model on receipt text."""
import pytest
from fastapi.testclient import TestClient

from app import main

RECEIPT = """TAN CHAY YEE
*** COPY ***
OJC MARKETING SDN BHD
ROC NO: 538358-H
NO 2 & 4, JALAN BAYU 4, BANDAR SERI ALAM, 81750 MASAI, JOHOR
Tel: 07-388 2218  Fax: 07-388 8218  Email: ng@ojcgroup.com
TAX INVOICE  Invoice No: PEGIV-1030765  Date: 15/01/2019 11:05:16 AM
Cashier: NG CHUAN MIN
Customer IC: 850101-14-5678
Pay to Maybank account 5140-1234-5678, card 4111 1111 1111 1111
Total: 193.00"""


@pytest.fixture(scope="module")
def api():
    with TestClient(main.app) as client:
        yield client


def masked(api, text):
    return api.post("/mask", json={"text": text}).json()


def test_personal_data_is_masked(api):
    out = masked(api, RECEIPT)
    m, mapping = out["masked"], out["mapping"]
    for secret in ["NG CHUAN MIN", "ng@ojcgroup.com", "07-388 2218", "850101-14-5678", "5140-1234-5678",
                   "4111 1111 1111 1111"]:
        assert secret not in m, secret
        assert secret in mapping.values(), secret
    assert "Cashier: <PERSON_" in m and "account <BANK_ACCOUNT_1>" in m and "<MY_NRIC_1>" in m


def test_what_the_agents_need_is_kept(api):
    m = masked(api, RECEIPT)["masked"]
    for kept in ["OJC MARKETING SDN BHD", "PEGIV-1030765", "15/01/2019", "193.00", "JALAN BAYU 4", "Customer IC",
                 "Maybank"]:
        assert kept in m, kept


def test_the_same_value_gets_the_same_placeholder(api):
    out = masked(api, "Call 07-388 2218. Again: 07-388 2218. Other: 03-2222 1111")
    assert out["masked"].count("<PHONE_NUMBER_1>") == 2 and "<PHONE_NUMBER_2>" in out["masked"]
    assert out["counts"] == {"PHONE_NUMBER": 2}


def test_putting_the_values_back_restores_the_text(api):
    out = masked(api, RECEIPT)
    restored = out["masked"]
    for token, value in out["mapping"].items():
        restored = restored.replace(token, value)
    assert restored == RECEIPT


def test_nothing_personal_nothing_masked(api):
    text = "BRIGHTLINE OFFICE SUPPLIES SDN BHD\nInvoice BOS-10811\nStapler x 2 RM 38.00\nTotal: RM 92.00"
    assert masked(api, text) == {"masked": text, "mapping": {}, "counts": {}}


def test_emails_are_masked_with_no_writable_disk_and_no_internet(api, monkeypatch):
    # The container's filesystem is read-only and it has no route out. tldextract (used by
    # Presidio's email check) once tried to take a lock file on every call; that failed live.
    import socket

    import filelock

    def refuse(*a, **k):
        raise PermissionError("read-only filesystem")

    def no_network(*a, **k):
        raise OSError("no route to the internet")

    monkeypatch.setattr(filelock.FileLock, "acquire", refuse)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    out = masked(api, "Send the remittance advice to accounts@kestrel-freight.com.my today.")
    assert "kestrel-freight" not in out["masked"] and out["counts"] == {"EMAIL_ADDRESS": 1}


@pytest.mark.parametrize("address", ["siti.aminah@kestrelprint.example", "ops@vendor.internal",
                                     "Accounts.Team+inv@kestrel-freight.com.my"])
def test_any_email_is_masked_whatever_its_domain(api, address):
    # Live phase 7 run: Presidio's own check left siti.aminah@kestrelprint.example unmasked,
    # because .example is not a public domain ending.
    out = masked(api, f"Queries: {address}  Tel: 07-388 2218")
    assert address not in out["masked"] and out["counts"]["EMAIL_ADDRESS"] == 1
    assert out["mapping"]["<EMAIL_ADDRESS_1>"] == address


# Phase 11a live run: the item table and the business's address came out garbled.
INVOICE = """TAX INVOICE

Kestrel Office Supplies Sdn Bhd
No. 12, Jalan Perindustrian 3, 47500 Subang Jaya, Selangor
Tel: 03-5631 2200

Invoice No: K-2026-102
Date: 04/11/2026
Bill To: Warden Demo Co.
Attn: Siti Nurhaliza

Description                     Qty   Unit Price    Amount (RM)
A4 Copier Paper (box of 5)        4        42.00        168.00
Printer Toner Cartridge           2       185.00        370.00
Ballpoint Pens (box of 50)        3        28.00         84.00

TOTAL (RM)                                              622.00"""


def test_an_invoice_item_table_reaches_the_models_intact(api):
    out = masked(api, INVOICE)
    for kept in ("A4 Copier Paper (box of 5)", "Printer Toner Cartridge", "168.00", "370.00", "622.00",
                 "47500 Subang Jaya, Selangor", "Bill To: Warden Demo Co."):
        assert kept in out["masked"], kept
    # What is personal is still masked: the phone number and the named contact.
    assert set(out["mapping"].values()) == {"03-5631 2200", "Siti Nurhaliza"}


def test_a_name_on_a_receipt_line_with_a_label_is_still_masked(api):
    out = masked(api, "Cashier: Tan Ah Kow\nServed by: Lim Mei Ling   Total 12.50")
    assert {"Tan Ah Kow", "Lim Mei Ling"} <= set(out["mapping"].values())


def test_an_invoice_number_is_not_a_phone_number_and_windows_line_endings_change_nothing(api):
    """Phase 11b live run: the API's invoice came from PowerShell with CRLF line endings; the
    invoice number was masked as a phone number and "Bill" (of "Bill To:") as a name."""
    text = INVOICE.replace("K-2026-102", "K-2026-155258").replace("\n", "\r\n")
    out = masked(api, text)
    assert "Invoice No: K-2026-155258" in out["masked"] and "Bill To: Warden Demo Co." in out["masked"]
    assert set(out["mapping"].values()) == {"03-5631 2200", "Siti Nurhaliza"}
    assert masked(api, "Receipt #: 0123456789\nCall us: 012-345 6789")["mapping"] == {"<PHONE_NUMBER_1>": "012-345 6789"}
