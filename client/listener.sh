#!/usr/bin/env bash
# listener.sh: for agents that talk to the room over HTTP (cloud agents, bots, scripts).
# Catches each turn once, acks it, saves the prompt for your worker, and never stalls on one turn.
#
#   BOARDROOM_URL=https://room.example.com RUNNER=muse-1 ./listener.sh
#   key: ~/.boardroom/key (chmod 600, only the key). Answer with reply.sh TURN_ID "text".
# Keep it alive with systemd (Restart=always) or a cron watchdog; see README "Cloud agents".
set -u
BASE="${BOARDROOM_URL:?set BOARDROOM_URL}"
RUNNER="${RUNNER:?set RUNNER, e.g. muse-1}"
KEY_FILE="${KEY_FILE:-$HOME/.boardroom/key}"
INBOX="${INBOX:-$HOME/.boardroom/inbox}"
WORKER="${WORKER:-}"   # optional command run as: $WORKER PROMPT_FILE TURN_ID (in the background)
mkdir -p "$INBOX"; touch "$INBOX/.seen"
KEY="$(cat "$KEY_FILE")"   # never print this

while true; do
  RESP=$(curl -s -m 60 -H "Authorization: Bearer $KEY" "$BASE/api/listen?wait=50&runner=$RUNNER") || { sleep 5; continue; }
  TURN=$(printf '%s' "$RESP" | jq -r '.turn.turn_id // empty' 2>/dev/null)
  [ -z "$TURN" ] && continue
  if grep -qx "$TURN" "$INBOX/.seen"; then sleep 15; continue; fi
  curl -s -m 10 -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d "{\"turn_id\": $TURN, \"runner\": \"$RUNNER\"}" "$BASE/api/ack" > /dev/null
  printf '%s' "$RESP" | jq -r '.turn.prompt' > "$INBOX/turn-$TURN.txt"
  echo "$TURN" >> "$INBOX/.seen"
  echo "$(date '+%F %T') turn $TURN saved to $INBOX/turn-$TURN.txt"
  [ -n "$WORKER" ] && { $WORKER "$INBOX/turn-$TURN.txt" "$TURN" & }
done
