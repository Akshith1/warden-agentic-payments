# Phase 11: the job that creates the service databases on Azure (see infra/azure/db-init.sh).
# Built with infra/azure as the context.
ARG POSTGRES_IMAGE=postgres:16-alpine
FROM ${POSTGRES_IMAGE}
COPY db-init.sh /usr/local/bin/warden-db-init
RUN chmod 0755 /usr/local/bin/warden-db-init
USER postgres
CMD ["warden-db-init"]
