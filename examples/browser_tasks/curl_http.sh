#!/usr/bin/env bash
# A bounded curl request with bearer headers passed through a pipe, not argv.
# JSON request body is stdin. Output intentionally strips returned credentials.
set +x
set -euo pipefail
: "${FLOWPILOT_API_KEY:?Read a key from secret input first}"
base="${FLOWPILOT_BASE_URL:-http://127.0.0.1:8080}"
method="${1:?GET/POST/DELETE}"
path="${2:?relative API path}"
[[ "$base" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ || "$base" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]] || { echo 'Use an HTTPS origin or loopback mock' >&2; exit 1; }
[[ "$FLOWPILOT_API_KEY" =~ ^[A-Za-z0-9._-]+$ ]] || { echo 'Invalid bearer credential format' >&2; exit 1; }
[[ "$path" =~ ^/api/[A-Za-z0-9/_-]+$ && "$method" =~ ^(GET|POST|DELETE)$ ]] || { echo 'Invalid method/path' >&2; exit 1; }
args=(--silent --show-error --fail --max-time 35 --max-redirs 0 --request "$method" --header 'Content-Type: application/json' --header 'Cache-Control: no-store')
if [[ "$method" == POST ]]; then args+=(--data-binary @-); fi
curl "${args[@]}" --config <(printf 'header = "Authorization: Bearer %s"\n' "$FLOWPILOT_API_KEY") "$base$path" |
  python3 -c '
import json,sys
def safe(v):
    if isinstance(v, dict): return {k:safe(x) for k,x in v.items() if k not in {"key","token","assistance_url","values"}}
    if isinstance(v, list): return [safe(x) for x in v]
    return v
data=sys.stdin.read()
if data: print(json.dumps(safe(json.loads(data)), indent=2))
'
