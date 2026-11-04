# Phase 11: the control plane with the gateway's per-agent rules inside the image (locally a
# mount of infra/litellm/agents.yaml). Built with infra/litellm as the context, on top of the
# image built from warden-control/Dockerfile.
ARG BASE
FROM ${BASE}
COPY --chown=root:root agents.yaml /etc/warden/agents.yaml
