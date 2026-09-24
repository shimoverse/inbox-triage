"""Local-only Gmail OAuth onboarding; token never leaves the device."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .gmail.client import SCOPE, write_private

CONFIG_DIR = Path.home() / ".config/inbox-triage"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Connect a Gmail account locally (repeat for each account)")
    p.add_argument("--credentials", type=Path, default=CONFIG_DIR / "client_secret.json",
                   help="Desktop OAuth client JSON from your own Google Cloud project "
                        "(default: ~/.config/inbox-triage/client_secret.json)")
    p.add_argument("--output", type=Path,
                   help="Token path (default: ~/.config/inbox-triage/tokens/<email>.json, which "
                        "`inbox-triage --account <email>` and `--all` find automatically)")
    p.add_argument("--config-dir", type=Path, default=CONFIG_DIR)
    p.add_argument("--force", action="store_true", help="Replace an existing token (re-consent)")
    p.add_argument("--no-browser", action="store_true",
                   help="Print the consent URL instead of opening a browser (e.g. over SSH with port forwarding)")
    p.add_argument("--port", type=int, default=0, help="Local redirect port (default: any free port)")
    args = p.parse_args(argv)
    credentials_file = args.credentials.expanduser()
    if not credentials_file.exists():
        p.error(f"OAuth client JSON not found at {credentials_file}; see the README 'Google Cloud setup' section")
    if args.output and args.output.expanduser().exists() and not args.force:
        p.error("Token file already exists; pass --force to replace it")
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), [SCOPE])
    credentials = flow.run_local_server(port=args.port, open_browser=not args.no_browser)
    profile = build("gmail", "v1", credentials=credentials, cache_discovery=False).users().getProfile(userId="me").execute()
    email = str(profile["emailAddress"])
    output = (args.output or args.config_dir / "tokens" / f"{email.casefold()}.json").expanduser()
    if output.exists() and not args.force:
        p.error(f"A token for {email} already exists at {output}; pass --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    write_private(output, credentials.to_json())
    print(f"Connected: {email} — token saved locally at {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
