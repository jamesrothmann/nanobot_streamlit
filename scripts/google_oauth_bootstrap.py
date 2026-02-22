#!/usr/bin/env python3
"""
Run Google OAuth desktop flow once and print secrets values for persistent use.

Usage:
  python3 scripts/google_oauth_bootstrap.py --credentials /path/to/credentials.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap persistent Google OAuth refresh token.")
    parser.add_argument("--credentials", required=True, help="Path to OAuth desktop client credentials.json")
    parser.add_argument("--port", type=int, default=8765, help="Local callback port for OAuth flow")
    parser.add_argument("--user-email", default="", help="Optional user email hint to print in output")
    args = parser.parse_args()

    creds_path = Path(args.credentials).expanduser()
    if not creds_path.exists():
        raise SystemExit(f"Credentials file not found: {creds_path}")

    client_config = json.loads(creds_path.read_text(encoding="utf-8"))
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(
        port=args.port,
        prompt="consent",
        access_type="offline",
    )

    if not creds.refresh_token:
        raise SystemExit(
            "No refresh_token returned. Revoke the app in your Google account and rerun with prompt=consent."
        )

    user_email = args.user_email.strip()
    print("\nAdd this to your Streamlit secrets:\n")
    print("[google_oauth]")
    print("enabled = true")
    print(f'client_id = "{creds.client_id}"')
    print(f'client_secret = "{creds.client_secret}"')
    print(f'refresh_token = "{creds.refresh_token}"')
    print(f'token_uri = "{creds.token_uri or "https://oauth2.googleapis.com/token"}"')
    if user_email:
        print(f'user_email = "{user_email}"')
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
