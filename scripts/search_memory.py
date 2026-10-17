"""Search one user's memories in the data store. Read-only.

Usage:
    uv run python scripts/search_memory.py "vegetarian food"
    uv run python scripts/search_memory.py "tea" --tenant acme --user alice
    uv run python scripts/search_memory.py --list          # list everything for the user
"""

import argparse
import sys
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
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--list", action="store_true", help="list all memories instead of searching")
    parser.add_argument("--tenant", default="demo-tenant")
    parser.add_argument("--user", default="demo-user")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    if not args.list and not args.query.strip():
        parser.error("give a query, or use --list")

    config = load_config()
    print(f"serving config: {config.serving_config_path}")
    try:
        store = DiscoveryEngineStore(config)
        if args.list:
            memories = store.list_all(args.tenant, args.user, args.limit)
        else:
            memories = store.search(args.tenant, args.user, args.query, args.limit)
    except DefaultCredentialsError:
        print("ERROR: no Google credentials found. Run: gcloud auth application-default login",
              file=sys.stderr)
        return 1
    except (StoreError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"{len(memories)} result(s) for tenant={args.tenant} user={args.user}")
    for memory in memories:
        print(f"  {memory.id}  [{memory.category}]  {memory.fact}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
