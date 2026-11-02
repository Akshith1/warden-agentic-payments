"""python -m catalogue keys     write the catalogue's own signing keys (once)
python -m catalogue ingest   read Warden's schemas into the catalogue, then tag and link them"""
import sys

from . import classify, config, keys, server, workflows


def ingest() -> None:
    from metadata.generated.schema.entity.services.connections.metadata.openMetadataConnection import (
        OpenMetadataConnection,
    )
    from metadata.generated.schema.security.client.openMetadataJWTClientConfig import OpenMetadataJWTClientConfig
    from metadata.ingestion.ometa.ometa_api import OpenMetadata
    from metadata.workflow.metadata import MetadataWorkflow

    server.wait_until_up(config.SERVER)
    admin = server.admin_token(config.SERVER, config.ADMIN_EMAIL, config.ADMIN_PASSWORD)
    bot = server.bot_token(config.SERVER, admin)
    for service in workflows.DATABASES:
        print(f"Reading {service} into the catalogue", flush=True)
        run = MetadataWorkflow.create(workflows.workflow(service, bot))
        run.execute()
        run.raise_from_status()
        run.print_status()
        run.stop()

    # Tags and lineage need the admin: creating a classification is outside the ingestion bot's role.
    client = OpenMetadata(OpenMetadataConnection(hostPort=f"{config.SERVER}/api", authProvider="openmetadata",
                                                 securityConfig=OpenMetadataJWTClientConfig(jwtToken=admin)))
    done = classify.apply(client)
    print(f"Tagged {done['column_tags']} personal-data columns and {done['table_tags']} tables guarded by "
          f"critical checks; added {done['lineage']} lineage links.")
    if done["missing"]:
        raise SystemExit("Not in the catalogue, so not tagged: " + ", ".join(done["missing"]))
    print(f"Done. Open {config.SERVER.replace('catalogue:8585', 'localhost:8585')} and search for warden.")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "keys":
        made = keys.ensure(config.KEYS_DIR)
        print("Created the catalogue's signing keys." if made else "Signing keys already present; kept them.")
    elif command == "ingest":
        try:
            ingest()
        except server.CatalogueError as e:
            raise SystemExit(str(e)) from None
    else:
        raise SystemExit(__doc__)
