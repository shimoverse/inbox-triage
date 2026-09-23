"""Local-only Gmail OAuth onboarding; token never leaves the device."""
from __future__ import annotations
import argparse
import os
from pathlib import Path

SCOPE = "https://www.googleapis.com/auth/gmail.modify"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Connect your Gmail account locally")
    p.add_argument("--credentials", type=Path, required=True,
                   help="Downloaded Desktop OAuth client JSON from your own Google Cloud project")
    p.add_argument("--output", type=Path, required=True, help="Private path for your OAuth token JSON")
    args = p.parse_args(argv)
    if args.output.exists():
        p.error("Token file already exists; choose a new path to avoid replacing an account")
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    flow = InstalledAppFlow.from_client_secrets_file(str(args.credentials), [SCOPE])
    credentials = flow.run_local_server(port=0)
    profile = build("gmail", "v1", credentials=credentials, cache_discovery=False).users().getProfile(userId="me").execute()
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.output.parent, 0o700)
    fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(credentials.to_json())
    print(f"Connected: {profile['emailAddress']} — token saved locally at {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
