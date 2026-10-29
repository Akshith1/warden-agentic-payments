FROM python:3.13-slim
WORKDIR /srv
RUN useradd --create-home --uid 10001 app \
 && mkdir -p /home/app/.cache/huggingface && chown -R app:app /home/app/.cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY loader ./loader
USER app
# The hf-cache volume mounts here; the directory must exist and belong to `app`
# in the image, or Docker creates the mount point owned by root.
ENV HF_HOME=/home/app/.cache/huggingface
CMD ["python", "-m", "loader"]
