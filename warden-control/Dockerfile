FROM python:3.13-slim
WORKDIR /srv
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
USER app
# /data exists in the image owned by app, so a new named volume there starts owned by app too.
VOLUME /data
EXPOSE 8000
HEALTHCHECK --start-period=20s --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
# Phase 9b: opentelemetry-instrument adds traces and request metrics; Compose sets where they go.
CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
