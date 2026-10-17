"""Create the Vertex AI Search data store, its schema and a search app. Safe to run repeatedly.

The search app is required: Discovery Engine does not index a data store's
documents until the data store is connected to an app.

Usage:
    uv run python scripts/setup_datastore.py
"""

import argparse
import sys
from pathlib import Path

# Make src/ importable without relying on the editable install's .pth file,
# which Python 3.12 skips when macOS marks it hidden (e.g. under ~/Desktop).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from typing import Any

from google.api_core import exceptions as gexc
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import discoveryengine_v1 as de

from memory_mcp.config import Config, load_config  # noqa: E402
from memory_mcp.store.discovery_engine import client_options  # noqa: E402

LRO_TIMEOUT_SECONDS = 900
DEFAULT_ENGINE_ID = "memories-app"

# "indexable" is the flag that makes a field usable in filters.
_FILTERABLE = {"indexable": True, "retrievable": True}

MEMORY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "tenant_id": {"type": "string", **_FILTERABLE},
        "user_id": {"type": "string", **_FILTERABLE},
        "category": {"type": "string", **_FILTERABLE},
        "fact": {"type": "string", "searchable": True, "retrievable": True},
        "confidence": {"type": "number", "retrievable": True},
        "source_app": {"type": "string", **_FILTERABLE},
        "created_at": {"type": "datetime", **_FILTERABLE},
        "updated_at": {"type": "datetime", **_FILTERABLE},
        "expires_at": {"type": "datetime", **_FILTERABLE},
    },
}


def ensure_data_store(config: Config) -> bool:
    """Create the data store with the memory schema. Returns True if it was created."""
    client = de.DataStoreServiceClient(client_options=client_options(config))
    try:
        client.get_data_store(name=config.data_store_path)
        print(f"data store '{config.data_store_id}': already exists")
        return False
    except gexc.NotFound:
        pass

    data_store = de.DataStore(
        display_name=config.data_store_id,
        industry_vertical=de.IndustryVertical.GENERIC,
        solution_types=[de.SolutionType.SOLUTION_TYPE_SEARCH],
        content_config=de.DataStore.ContentConfig.NO_CONTENT,
        starting_schema=de.Schema(struct_schema=MEMORY_SCHEMA),
    )
    operation = client.create_data_store(
        parent=config.collection_path,
        data_store=data_store,
        data_store_id=config.data_store_id,
    )
    print(f"data store '{config.data_store_id}': creating...")
    operation.result(timeout=LRO_TIMEOUT_SECONDS)
    print(f"data store '{config.data_store_id}': created with schema")
    return True


def apply_schema(config: Config) -> None:
    """Re-apply the schema to an existing data store so reruns converge."""
    client = de.SchemaServiceClient(client_options=client_options(config))
    schema = de.Schema(name=config.schema_path, struct_schema=MEMORY_SCHEMA)
    operation = client.update_schema(
        request=de.UpdateSchemaRequest(schema=schema, allow_missing=True)
    )
    print("schema: applying...")
    operation.result(timeout=LRO_TIMEOUT_SECONDS)
    print("schema: applied")


def ensure_engine(config: Config, engine_id: str) -> None:
    """Create a standard-tier search app on top of the data store, if missing."""
    client = de.EngineServiceClient(client_options=client_options(config))
    name = f"{config.collection_path}/engines/{engine_id}"
    try:
        client.get_engine(name=name)
        print(f"search app '{engine_id}': already exists")
        return
    except gexc.NotFound:
        pass

    engine = de.Engine(
        display_name=engine_id,
        solution_type=de.SolutionType.SOLUTION_TYPE_SEARCH,
        industry_vertical=de.IndustryVertical.GENERIC,
        data_store_ids=[config.data_store_id],
        search_engine_config=de.Engine.SearchEngineConfig(
            search_tier=de.SearchTier.SEARCH_TIER_STANDARD
        ),
    )
    operation = client.create_engine(
        parent=config.collection_path, engine=engine, engine_id=engine_id
    )
    print(f"search app '{engine_id}': creating...")
    operation.result(timeout=LRO_TIMEOUT_SECONDS)
    print(f"search app '{engine_id}': created. Set ENGINE_ID={engine_id} in .env to use it.")


def engine_serves_data_store(config: Config) -> bool:
    """True if the configured search app is missing or is attached to our data store."""
    client = de.EngineServiceClient(client_options=client_options(config))
    try:
        engine = client.get_engine(name=config.engine_path)
    except gexc.NotFound:
        return True
    attached = list(engine.data_store_ids)
    if config.data_store_id in attached:
        return True
    print(
        f"ERROR: search app '{config.engine_id}' is attached to data stores {attached}, "
        f"not '{config.data_store_id}'.\n"
        "Set DATA_STORE_ID in .env to one of those, or clear ENGINE_ID to use a new data store.",
        file=sys.stderr,
    )
    return False


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()

    config = load_config()
    print(f"project={config.project_id} location={config.location} collection={config.collection}")
    try:
        # Check an existing app first, so nothing is created if it cannot be used.
        if config.engine_id and not engine_serves_data_store(config):
            return 1
        created = ensure_data_store(config)
        if not created:
            apply_schema(config)
        # Required: documents are only indexed once the data store is connected to an app.
        ensure_engine(config, config.engine_id or DEFAULT_ENGINE_ID)
    except DefaultCredentialsError:
        print(
            "ERROR: no Google credentials found. Run:\n"
            "  gcloud auth application-default login\n"
            f"  gcloud auth application-default set-quota-project {config.project_id}",
            file=sys.stderr,
        )
        return 1
    except gexc.GoogleAPICallError as exc:
        print(f"ERROR: Discovery Engine call failed: {exc}", file=sys.stderr)
        return 1
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
