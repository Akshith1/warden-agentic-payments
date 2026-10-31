FROM python:3.13-slim
RUN useradd --create-home --uid 10001 app \
 && pip install --no-cache-dir "mlflow==3.17.2" "psycopg[binary]==3.3.6" \
 && mkdir -p /mlartifacts && chown app:app /mlartifacts
USER app
ENV MLFLOW_DISABLE_AGENT_HINT=1
EXPOSE 5000
HEALTHCHECK --start-period=60s --interval=15s --timeout=5s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')"
