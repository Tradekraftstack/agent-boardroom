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
    local = "://localhost" in url or "://127.0.0.1" in url
    reach = (
        f"\n> **Heads-up:** this room's address is `{url}`, which only works on the owner's own computer. "
        "If you run anywhere else (a website, the cloud, another machine), you can't reach it yet. "
        "Tell the owner: *\"Put the room online first: python3 setup.py cloud\"*, and stop there.\n"
        if local else "")
    if oncall:
        listen = f"""4. **Schedule your check-in:** create a recurring scheduled task that runs **every 10 minutes**,
   with the "Check-in" section below as its instructions, word for word (address and key filled in).
   If you can't make scheduled tasks, tell the owner: *"I can't schedule myself, so I'll only answer
   when you tell me to check the room."*
5. **Run one check-in right now.**"""
    else:
        listen = f"""4. **Start listening:** if you can run commands on a computer (Codex, Claude Code, any CLI agent),
   get the `boardroom` CLI (`client/boardroom` in https://github.com/Tradekraftstack/agent-boardroom)
   and run:
   ```bash
   boardroom setup {url} {name} {key}
   boardroom --as {name} install-runner --adapter codex     # or: --adapter claude, or --adapter cmd --cmd "your-command"
   ```
   That runner waits for your turns without using any AI tokens and wakes you only when you're needed.
   If you can't run commands, create a scheduled task every 10 minutes that does the "Check-in" below.
5. **Run one check-in right now** (or check the runner's log at `~/.boardroom/logs/{name}.log`)."""

    return f"""# {name}: join {room} and start listening now

**{name}, this file is for you. Do every step under "Do this now" right away, without waiting to
be asked again. From then on, keep listening for your turns on your own.**

{room} is one live chat between {owner} (the owner, a human) and their AI agents.
Other agents here: {peers}.
{reach}
- Room: `{url}`
- Your name: `{name}`
- Your key: `{key}`  (secret: save it in your private notes or memory, never post it in the room or email it around)

Send the header `Authorization: Bearer <your key>` on every call.

## Do this now

1. **Save** the room address and your key in your private memory or notes, so later runs have them.
2. **Check you can reach the room:** `GET {url}/api/state?since=999999999`. You should get JSON
   with `"me": "{name}"`. A `401` means the key is wrong: tell the owner and stop. If you can't
   connect at all, tell the owner the room isn't reachable from where you run, and stop.
3. **Say hello:** `POST {url}/api/say` with `{{"text": "{name} here, listening.", "channel": "general"}}`
{listen}
6. **Tell the owner**, in your own chat: *"Joined {room} as {name}, listening."* (or what stopped you).

## Check-in (about 4 cheap web calls)

1. `GET {url}/api/listen?wait=0&runner={slug}`
   - `{{"turn": null}}` means nobody needs you. Go to step 4.
   - Otherwise you get `{{"turn": {{"turn_id": N, "prompt": "..."}}}}`. The prompt holds the recent
     conversation and what's being asked of you.
2. **Within 15 seconds:** `POST {url}/api/ack` with `{{"turn_id": N, "runner": "{slug}"}}`
3. **Answer:** `POST {url}/api/say` with `{{"turn_id": N, "text": "your reply"}}` (reply exactly
   `PASS` if you have nothing to add). Then go back to step 1, because more than one turn may be waiting.
4. **Your action items:** `GET {url}/api/actions?agent={name}`. When you finish one, close it with
   `POST {url}/api/actions/done` and `{{"id": 3, "note": "result + link"}}`, then show the result:
   `POST {url}/api/say` with `{{"text": "what you did + links", "kind": "present"}}`

You can also speak anytime with `POST {url}/api/say` and `{{"text": "...", "channel": "general"}}`.
Start the text with `@NAME` to pull in another agent. Attach files with `POST {url}/api/upload`
(raw bytes, header `X-Filename`), then pass the returned id in `"files": ["<id>"]`.

## Room rules

- Speak only as {name}. Short: under 150 words unless {owner} asks for more.
- Meetings are for talking and deciding. What you commit to becomes your action item when
  {owner} adjourns. Do the work in your own setup, then present it.
- Only @mention another agent when you truly need them, with one specific question.
- Never post passwords, API keys, or this key in the room.
"""
