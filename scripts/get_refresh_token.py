#!/usr/bin/env python3
"""One-time interactive consent to seed the Graph refresh token.

Two commands, so it works in a shell with no stdin (Claude Code's `!` prefix,
a CI step, anything non-interactive). The original version prompted three
times with input() and getpass() and simply died with EOFError there.

    python scripts/get_refresh_token.py url
    python scripts/get_refresh_token.py exchange "<the full redirect URL>"

Step 1 prints a sign-in URL. Open it, sign in as alex@example.org.
The browser lands on a "can't reach this page" error at localhost:8400 - that
is expected, nothing is listening there. Copy the WHOLE address out of that
failed page's address bar and pass it to step 2.

TWO THINGS THIS FIXES BEYOND THE STDIN PROBLEM:

1. **Scopes come from shared/graph_client.py**, not a copy. This file used to
   carry its own SCOPES list, which drifted: the app was asking Entra for
   Calendars.ReadWrite while this script still requested Calendars.Read, so a
   freshly minted token would have been missing the very scope it was being
   re-issued to obtain - and nothing would have said so.

2. **The refresh token is written straight to Key Vault**, never printed. The
   old version printed it and told you to clear the terminal, which put a live
   mailbox credential in scrollback, in any transcript, and in the clipboard.
   The client secret is read from the vault too, so neither credential passes
   through a command line.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

REDIRECT_URI = "http://localhost:8400/callback"
VAULT = os.environ.get("CRC_VAULT", "<your-key-vault>")
SECRET = "graph-refresh-token"
EXPECT_USER = "alex@example.org"

# Your own Entra ID app registration. There is deliberately no default: these
# identify the application that will hold delegated access to a real mailbox.
TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "").strip()
CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "").strip()
if not TENANT_ID or not CLIENT_ID:
    raise SystemExit(
        "Set GRAPH_TENANT_ID and GRAPH_CLIENT_ID first - see README.md, "
        "'2. Register the application'."
    )


def scopes() -> list[str]:
    """The exact scopes the running app asks for - never a second copy."""
    os.environ.setdefault("GRAPH_CALENDAR_WRITE", "true")
    from shared.graph_client import _scopes

    return _scopes()


def az(*args: str) -> str:
    """Run the Azure CLI without depending on `python` being on PATH.

    az.bat shells out to bare `python`, which on this machine resolves to the
    Microsoft Store stub and dies with "Python was not found". Invoking the az
    entry-point script with THIS interpreter sidesteps that entirely.
    """
    entry = os.path.expandvars(r"%APPDATA%\Python\Python311\Scripts\az")
    command = [sys.executable, entry, *args] if os.path.exists(entry) else ["az", *args]
    out = subprocess.run(command, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"az failed:\n{out.stderr[:600]}")
    return out.stdout.strip()


def post_form(url: str, fields: dict[str, str]) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"\nToken exchange failed ({exc.code}):\n"
            f"{exc.read().decode(errors='replace')[:800]}"
        ) from None


def get_json(url: str, token: str) -> dict | None:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError:
        return None


def extract_code(pasted: str) -> str:
    """Accept a full redirect URL, a query string, or a bare code."""
    pasted = (pasted or "").strip().strip('"').strip("'")
    if not pasted:
        raise SystemExit("Nothing pasted.")

    if "=" in pasted:
        query = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
        params = urllib.parse.parse_qs(query)
        if "error" in params:
            raise SystemExit(
                f"Sign-in returned an error: {params['error'][0]} - "
                f"{params.get('error_description', [''])[0][:300]}"
            )
        if "code" in params:
            return params["code"][0]
        raise SystemExit("No authorization code in that URL.")

    if " " in pasted:
        raise SystemExit("Could not find an authorization code in what you pasted.")
    return pasted


def cmd_url() -> int:
    wanted = scopes()
    authorize = (
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize?"
        + urllib.parse.urlencode(
            {
                "client_id": CLIENT_ID,
                "response_type": "code",
                "redirect_uri": REDIRECT_URI,
                "response_mode": "query",
                "scope": " ".join(wanted),
                # NOT prompt=consent: where tenant user consent is disabled,
                # forcing a consent prompt diverts into the admin-approval
                # request flow instead of using the tenant-wide grant that
                # already exists.
                "prompt": "select_account",
            }
        )
    )
    print("Requesting these scopes:")
    for scope in wanted:
        print(f"   {scope.rsplit('/', 1)[-1]}")
    print("\n" + "=" * 72)
    print(f"STEP 1. Open this and sign in as {EXPECT_USER}:")
    print("=" * 72)
    print(authorize)
    print("=" * 72)
    print(
        "\nSTEP 2. The browser will fail to load localhost:8400. That is expected.\n"
        "Copy the ENTIRE address from that failed page, then run:\n\n"
        '   python scripts/get_refresh_token.py exchange "<paste the URL here>"\n'
    )
    return 0


def cmd_exchange(pasted: str) -> int:
    code = extract_code(pasted)
    wanted = scopes()

    print("Reading the client secret from Key Vault...")
    client_secret = az("keyvault", "secret", "show", "--vault-name", VAULT,
                       "--name", "graph-client-secret", "--query", "value", "-o", "tsv")

    print("Exchanging the authorization code...")
    payload = post_form(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        {
            "client_id": CLIENT_ID,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(wanted),
        },
    )

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise SystemExit(
            "No refresh token returned - offline_access was not granted. "
            "Check the app registration's API permissions."
        )

    granted = (payload.get("scope") or "").split()
    print("\nScopes actually granted:")
    for scope in sorted(granted):
        print(f"   {scope.rsplit('/', 1)[-1]}")

    if not any(s.endswith("Calendars.ReadWrite") for s in granted):
        print(
            "\nWARNING: Calendars.ReadWrite is NOT in the granted scopes.\n"
            "Admin consent has probably not been granted yet. The token below\n"
            "will still work for everything else, but blocking calendar time\n"
            "will keep failing. Grant consent, then run this again.",
            file=sys.stderr,
        )

    who = get_json("https://graph.microsoft.com/v1.0/me", payload["access_token"])
    if who:
        upn = who.get("userPrincipalName", "")
        print(f"\nSigned in as: {upn}")
        if upn.lower() != EXPECT_USER:
            raise SystemExit(
                f"\nThat is not {EXPECT_USER}. The token must belong to the "
                "mailbox being managed. Nothing was stored - start over."
            )
    else:
        print("\nWarning: could not verify the account via /me.")

    # Straight into the vault. Never printed, never on a command line.
    tmp = pathlib.Path(os.environ.get("TEMP", "/tmp")) / "crc-rt.txt"
    tmp.write_text(refresh_token, encoding="utf-8")
    try:
        az("keyvault", "secret", "set", "--vault-name", VAULT, "--name", SECRET,
           "--value", refresh_token, "--output", "none")
    finally:
        tmp.unlink(missing_ok=True)

    stored = az("keyvault", "secret", "show", "--vault-name", VAULT, "--name", SECRET,
                "--query", "length(value)", "-o", "tsv")
    if int(stored) != len(refresh_token):
        raise SystemExit(f"Stored {stored} chars but the token is {len(refresh_token)}.")

    print(f"\nStored in {VAULT} as '{SECRET}' ({stored} chars, verified).")
    print("The token was never printed.")
    print(
        "\nNext: the app also keeps a copy in its own table and prefers that one,\n"
        "so clear the stored token there (or let the bootstrap seed win) and set\n"
        "GRAPH_CALENDAR_WRITE=true, then restart the Function App."
    )
    return 0


def main(argv: list[str]) -> int:
    command = (argv[1] if len(argv) > 1 else "").lower()
    if command == "url":
        return cmd_url()
    if command == "exchange":
        if len(argv) < 3:
            raise SystemExit('Usage: get_refresh_token.py exchange "<redirect URL>"')
        return cmd_exchange(argv[2])

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
