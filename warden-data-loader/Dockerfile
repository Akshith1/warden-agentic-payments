FROM python:3.13-slim
WORKDIR /srv
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY loader ./loader
USER app
ENV HF_HOME=/home/app/.cache/huggingface
CMD ["python", "-m", "loader"]
