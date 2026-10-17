"""Get an Auth0 access token for the memory-mcp API.

Device flow (default): prints a URL and code, you sign in in the browser.
Password grant (--username): for synthetic demo users only; needs the Password
grant enabled on the Auth0 application and a default directory set.

Tokens are printed only with --print, never logged.

Usage:
    uv run --group agent python agent/auth0_login.py --print
    uv run --group agent python agent/auth0_login.py --username alice@demo.test --print
"""

import argparse
import getpass
import os
import sys
import time

import httpx
from dotenv import load_dotenv

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _settings() -> tuple[str, str, str]:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    try:
        return (
            os.environ["AUTH0_DOMAIN"].strip(),
            os.environ["AUTH0_CLIENT_ID"].strip(),
            os.environ["AUTH0_AUDIENCE"].strip(),
        )
    except KeyError as missing:
        sys.exit(f"ERROR: set {missing.args[0]} in agent/.env")


def _token_or_exit(response: httpx.Response) -> dict:
    """The whole token response (access_token, expires_in, id_token, ...)."""
    body = response.json()
    if response.status_code != 200 or "access_token" not in body:
        sys.exit(f"ERROR: Auth0 refused: {body.get('error', response.status_code)}: {body.get('error_description', '')}")
    return body


def device_login() -> dict:
    domain, client_id, audience = _settings()
    start = httpx.post(
        f"https://{domain}/oauth/device/code",
        data={"client_id": client_id, "audience": audience, "scope": "openid email"},
        timeout=15,
    )
    if start.status_code != 200:
        sys.exit(f"ERROR: Auth0 device code request failed: {start.text}")
    code = start.json()
    print(f"Open {code['verification_uri_complete']} and confirm code {code['user_code']}", file=sys.stderr)
    interval = int(code.get("interval", 5))
    deadline = time.monotonic() + int(code.get("expires_in", 600))
    while time.monotonic() < deadline:
        time.sleep(interval)
        response = httpx.post(
            f"https://{domain}/oauth/token",
            data={"grant_type": DEVICE_GRANT, "device_code": code["device_code"], "client_id": client_id},
            timeout=15,
        )
        error = response.json().get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        return _token_or_exit(response)
    sys.exit("ERROR: sign-in timed out")


def password_login(username: str, password: str) -> dict:
    domain, client_id, audience = _settings()
    response = httpx.post(
        f"https://{domain}/oauth/token",
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
            "client_id": client_id,
            "audience": audience,
            "scope": "openid email",
        },
        timeout=15,
    )
    return _token_or_exit(response)


def login_response(username: str | None = None) -> dict:
    """Sign in and return Auth0's full token response."""
    if username:
        password = os.environ.get("DEMO_PASSWORD") or getpass.getpass(f"Password for {username}: ")
        return password_login(username, password)
    return device_login()


def login(username: str | None = None) -> str:
    """Sign in and return just the access token."""
    return login_response(username)["access_token"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username", help="use the password grant for this synthetic demo user")
    parser.add_argument("--print", action="store_true", help="print the access token to stdout")
    args = parser.parse_args()
    token = login(args.username)
    print("signed in", file=sys.stderr)
    if args.print:
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
