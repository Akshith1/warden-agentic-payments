# Phase 11: the policy service with the Cedar policies inside the image. Locally they are a
# read-only mount of ./policies; Container Apps has no such mount, and baking them in means
# a policy change is a new image, reviewed and tagged like code. Built with policies/ as the
# context, on top of the image built from warden-policy/Dockerfile.
ARG BASE
FROM ${BASE}
COPY --chown=root:root . /policies
ENV POLICY_DIR=/policies
