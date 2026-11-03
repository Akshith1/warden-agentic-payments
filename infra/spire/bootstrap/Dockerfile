# The SPIRE images have no shell; this job needs one to read the server's answers and write files.
FROM ghcr.io/spiffe/spire-server:1.15.5 AS spire
FROM python:3.13-slim
COPY --from=spire /opt/spire/bin/spire-server /usr/local/bin/spire-server
COPY bootstrap.py /srv/bootstrap.py
CMD ["python", "/srv/bootstrap.py"]
