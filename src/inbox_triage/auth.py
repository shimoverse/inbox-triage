"""Local-only Gmail OAuth onboarding; token never leaves the device."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .gmail import oauth as google_oauth
from .gmail.client import write_private

CONFIG_DIR = Path.home() / ".config/inbox-triage"


def loopback_consent(client: dict, port: int = 0, open_browser: bool = True):
    """Run Google consent in the browser and catch the redirect on 127.0.0.1 (one request)."""
    import secrets
    import urllib.parse
    import webbrowser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            result.update(query)
            ok = "code" in query
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Connected. You can close this tab." if ok else "Sign-in was cancelled.").encode())

        def log_message(self, *args):  # don't print the authorization code
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    state = secrets.token_urlsafe(24)
    url, verifier = google_oauth.authorization_url(client, redirect_uri, state)
    print("Opening your browser for Google sign-in. If it doesn't open, visit:\n" + url)
    if open_browser:
        webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if result.get("state") != state or "code" not in result:
        raise SystemExit("Google sign-in was cancelled or didn't match this request")
    return google_oauth.exchange(client, redirect_uri, result["code"], verifier)


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
    import json
    client = json.loads(credentials_file.read_text(encoding="utf-8"))
    credentials = loopback_consent(client, args.port, open_browser=not args.no_browser)
    email = google_oauth.profile_email(credentials)
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
