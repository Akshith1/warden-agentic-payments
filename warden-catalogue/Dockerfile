# The catalogue's one-shot jobs: its signing keys, and ingestion of Warden's schemas.
# The OpenMetadata ingestion package only (no Airflow): OpenMetadata's own ingestion image
# carries a full Airflow, which Warden doesn't need to run two jobs on demand.
FROM python:3.11-slim
WORKDIR /srv
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY catalogue ./catalogue
USER app
CMD ["python", "-m", "catalogue", "ingest"]
