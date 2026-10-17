"""Check that every infoType in redactor.INFO_TYPES exists for this project. Read-only.

DLP rejects a whole request if it names an unknown infoType, and the redactor
fails closed, so one bad name would block every add_memory call.

Usage:
    uv run python scripts/check_dlp_info_types.py
"""

import sys
from pathlib import Path

# Make src/ importable without relying on the editable install's .pth file,
# which Python 3.12 skips when macOS marks it hidden (e.g. under ~/Desktop).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google.api_core import exceptions as gexc
from google.api_core.client_options import ClientOptions
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import dlp_v2

from memory_mcp.config import load_config  # noqa: E402
from memory_mcp.governance.redactor import INFO_TYPES  # noqa: E402


def main() -> int:
    config = load_config()
    parent = f"projects/{config.project_id}/locations/{config.ai.dlp_location}"
    print(f"listing infoTypes for {parent}")
    try:
        client = dlp_v2.DlpServiceClient(client_options=ClientOptions(quota_project_id=config.project_id))
        response = client.list_info_types(request=dlp_v2.ListInfoTypesRequest(parent=parent))
    except DefaultCredentialsError:
        print("ERROR: no Google credentials found. Run: gcloud auth application-default login", file=sys.stderr)
        return 1
    except gexc.GoogleAPICallError as exc:
        print(f"ERROR: DLP call failed: {exc}", file=sys.stderr)
        return 1

    known = {info.name for info in response.info_types}
    missing = [name for name in INFO_TYPES if name not in known]
    for name in INFO_TYPES:
        print(f"  {'OK     ' if name in known else 'MISSING'} {name}")
    print(f"{len(INFO_TYPES) - len(missing)} of {len(INFO_TYPES)} found ({len(known)} available)")
    if missing:
        print(f"Remove these from INFO_TYPES in governance/redactor.py: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
