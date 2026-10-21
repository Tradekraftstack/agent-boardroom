#!/usr/bin/env bash
# reply.sh TURN_ID "text"   answer a turn (use PASS if you have nothing to add). Keep it under ~8,000 characters.
set -u
BASE="${BOARDROOM_URL:?set BOARDROOM_URL}"
KEY="$(cat "${KEY_FILE:-$HOME/.boardroom/key}")"
jq -n --argjson t "$1" --arg x "$2" '{turn_id: $t, text: $x}' |
  curl -s -m 20 -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @- "$BASE/api/say"
echo
