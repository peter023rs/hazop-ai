#!/usr/bin/env bash
#
# share.sh — expose the HAZOP-AI web apps through Cloudflare quick tunnels,
# behind one shared password.
#
#   ./share.sh                            # both apps, random password
#   ./share.sh sw                         # dashboard only (8780)
#   ./share.sh dim                        # P&ID validator only (8777)
#   HAZOP_WEB_PASSWORD=hunter2 ./share.sh # pick your own password
#
# Both URLs are ephemeral: they die with this process and new ones are
# issued next run.  Ctrl-C tears down the servers and the tunnels.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$ROOT/.venv/bin/python"
LOGDIR="${TMPDIR:-/tmp}/hazop-tunnels"
WHICH="${1:-both}"

mkdir -p "$LOGDIR"
export HAZOP_WEB_PASSWORD="${HAZOP_WEB_PASSWORD:-$(openssl rand -base64 12)}"

# The validator's review history still lives in the pre-consolidation tree;
# point at it so the tunnelled app shows the real extractions, not an empty list.
LEGACY_RUNS="$ROOT/../hazop_L1(Extraction)/runs"
if [ -z "${HAZOP_DIM_RUNS:-}" ] && [ -d "$LEGACY_RUNS" ]; then
  export HAZOP_DIM_RUNS="$LEGACY_RUNS"
fi

trap 'kill 0' EXIT INT TERM

# --- start one app + its tunnel; prints nothing, records URL in $LOGDIR ----
serve() {
  local name=$1 port=$2 module=$3
  echo "starting $name on 127.0.0.1:$port ..."
  "$PY" -m "$module" >"$LOGDIR/$name.app.log" 2>&1 &

  # The SW dashboard contracts the plant graph and indexes the KB on boot,
  # so first response can take a minute.  Poll rather than guess.
  local waited=0
  until curl -sf -o /dev/null -u ":$HAZOP_WEB_PASSWORD" \
              "http://127.0.0.1:$port/"; do
    sleep 2; waited=$((waited + 2))
    if [ "$waited" -ge 180 ]; then
      echo "!! $name never came up — see $LOGDIR/$name.app.log" >&2
      return 1
    fi
  done

  # --protocol http2 is not optional on this network: cloudflared's default
  # QUIC transport passes its own pre-checks, registers, then loses the
  # control stream every few seconds ("failed to run the datagram handler").
  # Cloudflare then deregisters the quick tunnel and its *.trycloudflare.com
  # hostname stops resolving, which looks exactly like "the tunnel is down".
  # HTTP/2 over TCP 443 is stable here.
  cloudflared tunnel --protocol http2 --url "http://127.0.0.1:$port" \
      >"$LOGDIR/$name.tunnel.log" 2>&1 &

  local url=""
  for _ in $(seq 30); do
    url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' \
           "$LOGDIR/$name.tunnel.log" | head -1 || true)"
    [ -n "$url" ] && break
    sleep 1
  done
  if [ -z "$url" ]; then
    echo "!! $name tunnel failed — see $LOGDIR/$name.tunnel.log" >&2
    return 1
  fi
  echo "$url" >"$LOGDIR/$name.url"
}

SW_URL="" DIM_URL=""
# dim first: its tunnel URL feeds the dashboard's "Stage 1 validator" link,
# so reviewers land on the public validator, not their own localhost.
if [ "$WHICH" = "both" ] || [ "$WHICH" = "dim" ]; then
  serve dim 8777 hazop.s1_dim.app
  DIM_URL="$(cat "$LOGDIR/dim.url")"
fi
if [ "$WHICH" = "both" ] || [ "$WHICH" = "sw" ]; then
  [ -n "$DIM_URL" ] && export HAZOP_DIM_URL="$DIM_URL"
  serve sw 8780 hazop.s5_sw.app
  SW_URL="$(cat "$LOGDIR/sw.url")"
fi

echo
echo "  ----------------------------------------------------------------"
[ -n "$SW_URL" ]  && echo "   dashboard (s5_sw)    $SW_URL"
[ -n "$DIM_URL" ] && echo "   P&ID validator (dim) $DIM_URL"
echo "   user                 (leave blank)"
echo "   password             $HAZOP_WEB_PASSWORD"
echo "  ----------------------------------------------------------------"
echo
echo "  Ctrl-C to shut down.  Logs: $LOGDIR"

wait
