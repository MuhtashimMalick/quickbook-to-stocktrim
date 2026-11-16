"""Admin API call from inside the container (the only place TunnelGate accepts it): python -m app.cli GET /jobs
POST with a body: echo '{"quantity": 5}' | python -m app.cli POST /jobs/create_test_po --json"""
import argparse
import os
import sys
import urllib.error
import urllib.request


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="Call /api/v1<path> with the ADMIN_TOKEN from the environment.")
    ap.add_argument("method", choices=["GET", "POST"])
    ap.add_argument("path", help="starts with /, e.g. /jobs")
    ap.add_argument("--json", action="store_true", help="send stdin as a JSON body")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for the response (default 600; /preflight with supplier checks takes minutes)")
    a = ap.parse_args(argv)
    if not a.path.startswith("/"):
        ap.error("path must start with /")
    headers = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
    if a.json:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"http://127.0.0.1:8000/api/v1{a.path}", method=a.method, headers=headers, data=sys.stdin.buffer.read() if a.json else None)
    try:
        sys.stdout.buffer.write(urllib.request.urlopen(req, timeout=a.timeout).read())
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()}")


if __name__ == "__main__":
    main()
