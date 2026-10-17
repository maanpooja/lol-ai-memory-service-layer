"""Add one memory to the data store and read it back. The memory is NOT deleted.

Usage:
    uv run python scripts/add_memory.py
    uv run python scripts/add_memory.py --tenant acme --user alice --fact "Likes green tea"
"""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

# Make src/ importable without relying on the editable install's .pth file,
# which Python 3.12 skips when macOS marks it hidden (e.g. under ~/Desktop).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google.auth.exceptions import DefaultCredentialsError

from memory_mcp.config import load_config
from memory_mcp.store.base import StoreError
from memory_mcp.store.discovery_engine import DiscoveryEngineStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", default="demo-tenant")
    parser.add_argument("--user", default="demo-user")
    parser.add_argument("--fact", default="Synthetic: prefers vegetarian food")
    parser.add_argument("--category", default="preference")
    parser.add_argument("--confidence", type=float, default=0.9)
    parser.add_argument("--source-app", default="add-memory-script")
    args = parser.parse_args()

    config = load_config()
    print(f"project={config.project_id} data_store={config.data_store_id}")
    try:
        store = DiscoveryEngineStore(config)
        created = store.create(
            args.tenant, args.user, args.fact, args.category, args.confidence, args.source_app
        )
        fetched = store.get(args.tenant, args.user, created.id)
    except DefaultCredentialsError:
        print("ERROR: no Google credentials found. Run: gcloud auth application-default login",
              file=sys.stderr)
        return 1
    except (StoreError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if fetched != created:
        print("FAIL: the memory read back does not match the one written", file=sys.stderr)
        return 1
    print("PASS: memory stored and read back")
    for field, value in asdict(fetched).items():
        print(f"  {field:<11} {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
