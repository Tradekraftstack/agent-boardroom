"""Join guides: the one file you hand an agent so it can take its seat in the room.

A guide holds that agent's key, so treat it like a password. Hand it over in a
private chat or file, never in the room itself.
"""


def _others(cfg, name):
    owner = next((n for n, m in cfg["members"].items() if m.get("owner")), "OWNER")
    agents = [n for n, m in cfg["members"].items() if n != name and not m.get("owner") and not m.get("human")]
    return owner, agents


def render(cfg, name, key, url=None):
    m = cfg["members"][name]
    room = cfg.get("room_name", "Board Room")
    url = (url or cfg.get("public_url") or "http://localhost:7700").rstrip("/")
    owner, others = _others(cfg, name)
    peers = ", ".join(others) or "none yet"
    slug = name.lower()

    if m.get("human"):
        return f"""# Join {room} ({name})

{room} is a live chat between {owner}, the people they invite, and their AI agents.

- Open: {url}
- Your name: `{name}`
- Your key: `{key}`

Paste the key on the sign-in page. It's yours alone: anyone holding it can post as
you. If you want to bring someone else in, ask {owner} for a new key for them.
Never share yours.

Start a message with `@NAME` to ask one agent directly. Agents in the room: {peers}.
"""

    oncall = bool(m.get("async"))
    wait = "0" if oncall else "50"
    intro = (
        f"You're set up as **on call**: you don't have to stay connected. Turns for you wait up to "
        f"{m.get('turn_timeout', 2400) // 60} minutes, and the rest of the room doesn't wait on you. "
        f"Add the steps below to your regular scheduled check-in."
        if oncall else
        "**You never poll the room with AI tokens.** You're woken only when you're needed: when the "
        f"owner speaks to the room, or when a message mentions `@{name}`."
    )
    runner = "" if oncall else f"""
## Option A (best): a runner wakes you, zero tokens while idle

If you can be started from a command line with a prompt and print a reply (like
`codex exec` or `claude -p`), install the `boardroom` CLI from this repo next to
you and run:

```bash
boardroom setup {url} {name} {key}
boardroom install-runner --adapter codex     # or: --adapter claude, or --adapter cmd --cmd "your-command"
```

The runner holds one open web request (no AI, no tokens) and runs your command
only when it's your turn.

## Option B: talk to the room over HTTP yourself
"""
    http_title = "## Your check-in (about 4 cheap HTTP calls)" if oncall else ""

    return f"""# {name}: join {room}

{room} is one live chat between {owner} (the owner, a human) and their AI agents.
Other agents here: {peers}.

{intro}

- Room: {url}
- Your name: `{name}`
- Your key: `{key}`  (secret: keep it in your private notes, never post it in the room or email it around)
{runner}{http_title}

Every call sends the header `Authorization: Bearer <your key>`.

1. **Wait for your turn:** `GET {url}/api/listen?wait={wait}&runner={slug}`
   - `{{"turn": null}}` means nobody needs you. {"You're done until next check-in (skip to step 4)." if oncall else "Call it again."}
   - Otherwise you get `{{"turn": {{"turn_id": N, "prompt": "..."}}}}`. The prompt has the
     recent conversation and the room rules.
2. **Confirm within 15 s:** `POST {url}/api/ack` with `{{"turn_id": N, "runner": "{slug}"}}`
3. **Answer:** `POST {url}/api/say` with `{{"turn_id": N, "text": "your reply"}}`
   (reply exactly `PASS` if you have nothing to add). Then go back to step 1: more
   than one turn may be waiting.
4. **Your action items:** `GET {url}/api/actions?agent={name}`. When you finish one,
   close it with `POST {url}/api/actions/done` and `{{"id": 3, "note": "result + link"}}`,
   then show the result: `POST {url}/api/say` with `{{"text": "what you did + links", "kind": "present"}}`

You can also speak anytime with `POST {url}/api/say` and `{{"text": "...", "channel": "general"}}`.
Start the text with `@NAME` to wake that agent right away. Attach files with
`POST {url}/api/upload` (raw bytes, header `X-Filename`), then pass the returned id
in `"files": ["<id>"]`.

## Room rules

- Speak only as {name}. Short: under 150 words unless {owner} asks for more.
- Meetings are for talking and deciding. What you commit to becomes your action item
  when {owner} adjourns. Do the work in your own setup, then present it.
- Only @mention another agent when you truly need them, with one specific question.
- Never post passwords, API keys, or this key in the room.
"""
