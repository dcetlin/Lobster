#!/usr/bin/env bash
# refresh-gh-token.sh — GitHub App installation token refresh
#
# Generates a JWT from the GitHub App private key, exchanges it for an
# installation access token via the GitHub API, writes the token to
# ~/lobster-config/github-app-token, and updates GH_TOKEN in config.env.
#
# Designed to run every 50 minutes via cron (tokens expire after 60 minutes).
# Logs with timestamps to ~/lobster-workspace/logs/gh-token-refresh.log.
# Exits 0 on success, 1 on failure.

set -euo pipefail

# --- Configuration ---
APP_ID="3506667"
INSTALLATION_ID="127157527"
PEM_PATH="${HOME}/lobster-config/github-app.pem"
TOKEN_FILE="${HOME}/lobster-config/github-app-token"
CONFIG_FILE="${HOME}/lobster-config/config.env"
LOG_FILE="${HOME}/lobster-workspace/logs/gh-token-refresh.log"

# --- Logging ---
log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

# --- Preflight checks ---
[ -f "$PEM_PATH" ] || die "Private key not found: $PEM_PATH"
[ -f "$CONFIG_FILE" ] || die "Config file not found: $CONFIG_FILE"

# --- Generate JWT and acquire installation token via Python ---
log "Generating JWT and requesting installation token..."

NEW_TOKEN=$(uv run python3 - <<PYEOF
import base64, json, time, subprocess, tempfile, os, sys
import urllib.request, urllib.error

APP_ID = "$APP_ID"
INSTALLATION_ID = "$INSTALLATION_ID"
PEM_PATH = "$PEM_PATH"

now = int(time.time())
header = {"alg": "RS256", "typ": "JWT"}
payload = {"iat": now - 60, "exp": now + 600, "iss": APP_ID}

def b64url(data):
    if isinstance(data, str):
        data = data.encode()
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

h = b64url(json.dumps(header, separators=(',', ':')))
p = b64url(json.dumps(payload, separators=(',', ':')))
signing_input = f"{h}.{p}"

with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
    f.write(signing_input)
    tmp_input = f.name

try:
    result = subprocess.run(
        ['openssl', 'dgst', '-sha256', '-sign', PEM_PATH, tmp_input],
        capture_output=True
    )
    if result.returncode != 0:
        print(f"ERROR: openssl failed: {result.stderr.decode()}", file=sys.stderr)
        sys.exit(1)
    sig = b64url(result.stdout)
    jwt_token = f"{signing_input}.{sig}"
finally:
    os.unlink(tmp_input)

url = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
req = urllib.request.Request(
    url,
    method='POST',
    headers={
        'Authorization': f'Bearer {jwt_token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Length': '0',
    }
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
        token = data.get('token', '')
        expires_at = data.get('expires_at', '')
        if not token:
            print("ERROR: No token in response", file=sys.stderr)
            sys.exit(1)
        print(f"{token}|{expires_at}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
) || die "Failed to acquire installation token"

# Parse token and expiry from output (format: "token|expires_at")
NEW_GH_TOKEN="${NEW_TOKEN%%|*}"
EXPIRES_AT="${NEW_TOKEN##*|}"

[ -n "$NEW_GH_TOKEN" ] || die "Empty token returned from Python script"

# --- Write token file ---
printf '%s' "$NEW_GH_TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"
log "Token written to $TOKEN_FILE (expires: $EXPIRES_AT)"

# --- Update GH_TOKEN line in config.env ---
# The config.env file has a shell-script sourcing pattern at the end:
#   GH_TOKEN_FILE="$HOME/lobster-config/github-app-token"
#   [ -f "$GH_TOKEN_FILE" ] && export GH_TOKEN="$(cat $GH_TOKEN_FILE)"
# Since it reads from the token file at source time, writing the file above
# is sufficient. However, if a static GH_TOKEN=... line exists, update it.
if grep -q '^GH_TOKEN=' "$CONFIG_FILE" 2>/dev/null; then
    # Update the static GH_TOKEN= line in-place
    sed -i "s|^GH_TOKEN=.*|GH_TOKEN=${NEW_GH_TOKEN}|" "$CONFIG_FILE"
    log "Updated GH_TOKEN line in $CONFIG_FILE"
else
    log "No static GH_TOKEN= line in config.env (token loaded via token file at source time)"
fi

log "SUCCESS: GitHub App token refreshed (expires: $EXPIRES_AT)"
exit 0
