"""The catalogue job without a running OpenMetadata: the workflow configs are
validated by OpenMetadata's own models, the API calls by a stand-in server that
answers as the 2.0.7 source does, and the tags against Warden's real schema."""
import base64
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from catalogue import classify, config, keys, server, workflows

ROOT = Path(__file__).resolve().parents[2]


def test_keys_are_made_once_and_work_together(tmp_path):
    assert keys.ensure(str(tmp_path)) is True
    first = (tmp_path / keys.PRIVATE).read_bytes()
    assert keys.ensure(str(tmp_path)) is False and (tmp_path / keys.PRIVATE).read_bytes() == first   # kept
    private = serialization.load_der_private_key(first, password=None)            # PKCS#8 DER, as Java reads it
    public = serialization.load_der_public_key((tmp_path / keys.PUBLIC).read_bytes())  # X.509 SubjectPublicKeyInfo
    signature = private.sign(b"token", padding.PKCS1v15(), hashes.SHA256())       # RS256, as the server signs
    public.verify(signature, b"token", padding.PKCS1v15(), hashes.SHA256())
    assert private.key_size == 2048


def test_workflows_validate_against_openmetadata_and_hold_no_rows(monkeypatch):
    from metadata.generated.schema.metadataIngestion.workflow import OpenMetadataWorkflowConfig
    monkeypatch.setattr(config, "READER_PASSWORD", "pw")
    for service in workflows.DATABASES:
        raw = workflows.workflow(service, "a.b.c")
        parsed = OpenMetadataWorkflowConfig.model_validate(raw)
        source = parsed.source.sourceConfig.config
        assert type(source).__name__ == "DatabaseServiceMetadataPipeline"   # metadata only: no profiler, no samples
        assert parsed.source.serviceConnection.root.config.username == "warden_quality"
        assert parsed.workflowConfig.openMetadataServerConfig.securityConfig.jwtToken.get_secret_value() == "a.b.c"
    backend = OpenMetadataWorkflowConfig.model_validate(workflows.workflow("warden_backend", "t"))
    assert backend.source.sourceConfig.config.tableFilterPattern.includes == ["^invoice_inbox$"]


class FakeCatalogue(BaseHTTPRequestHandler):
    """Answers the three calls as OpenMetadata 2.0.7's UserResource and BotResource do."""
    password = "S3cret-pass"
    bot_id = str(uuid.uuid4())

    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path == "/api/v1/users/login":
            ok = (body["email"] == "admin@open-metadata.org"
                  and base64.b64decode(body["password"]).decode() == self.password)
            return self.reply(200, {"accessToken": "admin-jwt", "tokenType": "Bearer "}) if ok else \
                self.reply(401, {"code": 401, "message": "You have entered an invalid username or password."})
        self.reply(404, {})

    def do_GET(self):
        authorised = self.headers.get("Authorization") == "Bearer admin-jwt"
        if self.path == "/api/v1/bots/name/ingestion-bot?fields=botUser" and authorised:
            return self.reply(200, {"name": "ingestion-bot", "botUser": {"id": self.bot_id, "type": "user"}})
        if self.path == f"/api/v1/users/auth-mechanism/{self.bot_id}" and authorised:
            return self.reply(200, {"authType": "JWT", "config": {"JWTToken": "bot-jwt", "JWTTokenExpiry": "Unlimited"}})
        if self.path == "/api/v1/system/version":
            return self.reply(200, {"version": "2.0.7"})
        self.reply(403 if not authorised else 404, {})


@pytest.fixture()
def catalogue():
    httpd = HTTPServer(("127.0.0.1", 0), FakeCatalogue)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_signs_in_as_admin_then_ingests_as_the_bot(catalogue):
    server.wait_until_up(catalogue, seconds=5)
    admin = server.admin_token(catalogue, "admin@open-metadata.org", "S3cret-pass")
    assert admin == "admin-jwt" and server.bot_token(catalogue, admin) == "bot-jwt"


def test_a_wrong_or_missing_admin_password_says_what_to_do(catalogue):
    with pytest.raises(server.CatalogueError, match="refused the admin password"):
        server.admin_token(catalogue, "admin@open-metadata.org", "admin")   # the image's default
    with pytest.raises(server.CatalogueError, match="fill-secrets"):
        server.admin_token(catalogue, "admin@open-metadata.org", "")


def schema_columns() -> dict[str, set[str]]:
    """Tables and columns exactly as Warden's code creates them."""
    sql = (ROOT / "warden-ledger/app/db.py").read_text() + (ROOT / "warden-data-loader/loader/db.py").read_text()
    tables = {}
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S):
        tables[name] = {line.split()[0] for line in body.strip().splitlines()
                        if line.strip() and not line.strip().startswith("--")}
    for table, column in re.findall(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)", sql):
        tables[table].add(column)
    return tables


def test_every_tag_points_at_a_real_column_and_every_table_is_described():
    tables = schema_columns()
    for t in classify.COLUMN_TAGS:
        assert t.column in tables[t.table], f"{t.table}.{t.column} is tagged but not in the schema"
    for _, table in classify.CRITICAL_TABLES + [(e.service, e.source) for e in classify.LINEAGE] + \
            [(e.service, e.target) for e in classify.LINEAGE]:
        assert table in tables
    sql = (ROOT / "warden-ledger/app/db.py").read_text() + (ROOT / "warden-data-loader/loader/db.py").read_text()
    described = set(re.findall(r"COMMENT ON TABLE (\w+) IS", sql))
    assert described == set(tables), f"no description for {set(tables) - described}"


def test_personal_data_tags_match_the_data_map():
    data_map = (ROOT / "docs/data-map.md").read_text()
    for t in classify.COLUMN_TAGS:
        assert f"`{t.table}.{t.column}`" in data_map, f"{t.table}.{t.column} is tagged but not in the data map"


def test_critical_tables_match_the_critical_checks():
    checks = (ROOT / "warden-quality/app/checks.py").read_text()
    critical = set(re.findall(r'Check\("[\w-]+", "\w+", "ledger", "(\w+)", CRITICAL', checks))
    assert critical == {table for _, table in classify.CRITICAL_TABLES}


class FakeClient:
    """Records what the job asks OpenMetadata to do."""

    def __init__(self, tables):
        self.tables, self.calls = tables, []

    def get_by_name(self, entity, fqn, fields=None):
        cols = self.tables.get(fqn)
        if cols is None:
            return None
        return SimpleNamespace(id=uuid.uuid5(uuid.NAMESPACE_URL, fqn), fullyQualifiedName=fqn,
                               columns=[SimpleNamespace(name=SimpleNamespace(root=c)) for c in cols])

    def create_or_update(self, request):
        self.calls.append(("create", type(request).__name__, request.name.root))

    def patch_column_tags(self, table, column_tags):
        self.calls += [("column", c.column_fqn, c.tag_label.tagFQN.root) for c in column_tags]

    def patch_tags(self, entity, source, tag_labels):
        self.calls += [("table", source.fullyQualifiedName, label.tagFQN.root) for label in tag_labels]

    def add_lineage(self, request):
        self.calls.append(("lineage", str(request.edge.fromEntity.id), str(request.edge.toEntity.id),
                           request.edge.lineageDetails.description))


def test_apply_tags_and_links_what_ingestion_found():
    found = {classify.fqn(service, table): cols for table, cols in schema_columns().items()
             for service in ("warden_ledger" if table != "invoice_inbox" else "warden_backend",)}
    client = FakeClient(found)
    done = classify.apply(client)
    assert done == {"column_tags": 3, "table_tags": 4, "lineage": 2, "missing": []}
    assert ("column", "warden_backend.warden_backend.public.invoice_inbox.text", "PII.Sensitive") in client.calls
    assert ("table", "warden_ledger.warden_ledger.public.historical_payments", "Warden.CriticalCheck") in client.calls
    assert client.calls[:2] == [("create", "CreateClassificationRequest", "Warden"),
                                ("create", "CreateTagRequest", "CriticalCheck")]


def test_a_table_missing_from_the_catalogue_is_reported():
    client = FakeClient({classify.fqn("warden_ledger", "accounts"): ["id", "balance"]})
    done = classify.apply(client)
    assert "warden_backend.warden_backend.public.invoice_inbox" in done["missing"]
    assert done["column_tags"] == 0 and done["lineage"] == 0
