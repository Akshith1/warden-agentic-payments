# Built from the pinned PyPI release rather than a moving image tag.
FROM python:3.13-slim
RUN useradd --create-home --uid 10001 app && pip install --no-cache-dir "litellm[proxy]==1.106.5"
USER app
COPY config.yaml /etc/litellm/config.yaml
EXPOSE 4000
HEALTHCHECK --interval=15s --timeout=5s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:4000/health/liveliness')"
CMD ["litellm", "--config", "/etc/litellm/config.yaml", "--host", "0.0.0.0", "--port", "4000"]
