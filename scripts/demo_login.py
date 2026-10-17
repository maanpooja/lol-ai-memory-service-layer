"""Sign a demo user in with Auth0 and cache their token for the demo agent.

The cached token is what demo_agent/memory_agent/auth.py sends to the server.
It is stored with owner-only permissions and never printed. The script checks
that you signed in as the expected demo user, so bob's token can never be
cached as alice's.

Usage:
    uv run --group agent python scripts/demo_login.py --user alice           # browser (device) sign-in
    uv run --group agent python scripts/demo_login.py --user bob --password  # password grant
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# demo_agent first: agent/ (Part 4) also has a package named memory_agent.
sys.path[:0] = [str(ROOT / "demo_agent"), str(ROOT / "agent")]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / "demo_agent" / ".env")

import jwt  # noqa: E402

from auth0_login import login_response  # noqa: E402
from memory_agent.auth import DemoIdentity, save_token  # noqa: E402


def main() -> int:
    env_identity = DemoIdentity.from_env()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", default=env_identity.user_id, help="demo user id, e.g. alice or bob")
    parser.add_argument("--email", help="expected Auth0 email (default: <user>@demo.test)")
    parser.add_argument("--password", action="store_true", help="use the password grant instead of the browser")
    args = parser.parse_args()

    identity = DemoIdentity(tenant_id=env_identity.tenant_id, user_id=args.user)
    expected = (args.email or f"{args.user}@demo.test").lower()
    print(f"Signing in demo user {identity.user_id!r} (tenant {identity.tenant_id}) as {expected}")

    response = login_response(expected if args.password else None)
    claims = jwt.decode(response.get("id_token", ""), options={"verify_signature": False}) if response.get("id_token") else {}
    email = str(claims.get("email", "")).lower()
    if email != expected:
        print(f"ERROR: signed in as {email or 'an unknown user'}, expected {expected}. Nothing cached.", file=sys.stderr)
        print("Sign out of Auth0 in the browser (or use a private window) and try again.", file=sys.stderr)
        return 1

    path = save_token(identity, response["access_token"], int(response.get("expires_in", 3600)), email)
    hours = int(response.get("expires_in", 3600)) / 3600
    print(f"Cached token for {identity.user_id!r} at {path} (valid for {hours:.1f}h).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
