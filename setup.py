#!/usr/bin/env python3
"""Set up and manage your Board Room. Python stdlib only.

  python3 setup.py init                      make a new room (asks a few questions)
  python3 setup.py init --yes --room "Acme HQ" --owner ALEX --url https://room.example.com --models openrouter,ollama
  python3 setup.py model NAME --provider grok [--model grok-4.7] [--base URL]   add an always-on AI model
  python3 setup.py apikey NAME               save that model's API key (asked for, hidden)
  python3 setup.py providers                 list model providers
  python3 setup.py list                      who's in the room
  python3 setup.py add NAME [--oncall|--human] [--role "..."] [--label "..."]
  python3 setup.py key NAME                  give NAME a fresh key (the old one stops working)
  python3 setup.py remove NAME
  python3 setup.py guide NAME                rewrite NAME's join guide
  python3 setup.py rename "New Room Name"
  python3 setup.py url https://room.example.com

Data lives in ./data (or $BOARDROOM_DATA). Every member gets their own random key,
so no two rooms share keys and nobody needs your key to get in. Join guides with keys
go to data/join/. Hand each one over privately. The running server picks up changes
within a few seconds, no restart needed.
"""
import json
import os
import re
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "server"))
import guides  # noqa: E402
import providers  # noqa: E402

DATA = os.environ.get("BOARDROOM_DATA", os.path.join(HERE, "data"))
CONFIG = os.path.join(DATA, "config.json")
JOIN = os.path.join(DATA, "join")

# The starter roster. Codex drives the room from your computer; Muse and Hark join from
# wherever they run; Agnes is optional and runs on the server.
PRESETS = {
    "CODEX": {"label": "Codex (OpenAI)",
              "role": "Drives the room. Coding, research, files, plans, setup. Runs on the owner's computer through the Codex CLI."},
    "MUSE": {"label": "Muse", "role": "Video: clips, ads, reels, motion.", "async": True, "turn_timeout": 2400},
    "HARK": {"label": "Hark", "role": "Research, review, monitoring. Checks in on a schedule, so replies are slower.",
             "async": True, "turn_timeout": 2400},
    "CLAUDE": {"label": "Claude Code",
               "role": "Analysis, docs, decks, spreadsheets, code. Runs on the owner's computer through the Claude CLI."},
}
SECRETS = os.path.join(DATA, "secrets.json")


def model_member(pid, model=None, base=None):
    p = providers.PROVIDERS[pid]
    m = {"label": p["label"], "server": True, "provider": pid, "model": model or p["model"],
         "role": f"Always on via {p['label']}. Quick answers, brainstorming, drafts, summaries."}
    if base:
        m["base"] = base.rstrip("/")
    return m


def clean(name):
    name = re.sub(r"[^A-Z0-9_-]", "", name.upper())[:24]
    if not name or not name[0].isalpha():
        sys.exit("Names are letters, numbers, - or _, starting with a letter.")
    return name


def new_key():
    return secrets.token_urlsafe(24)


def load():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"No room yet at {CONFIG}. Run: python3 setup.py init")


def save(cfg):
    os.makedirs(DATA, exist_ok=True)
    os.chmod(DATA, 0o700)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def show(path):
    rel = os.path.relpath(path)
    return path if rel.startswith("..") else rel


def write_guide(cfg, name):
    m = cfg["members"][name]
    if m.get("server") or m.get("owner"):
        return None
    os.makedirs(JOIN, exist_ok=True)
    os.chmod(JOIN, 0o700)
    path = os.path.join(JOIN, f"JOIN-{name}.md")
    with open(path, "w") as f:
        f.write(guides.render(cfg, name, m["key"]))
    os.chmod(path, 0o600)
    return path


def flag(args, name, default=None, has_value=True):
    if name not in args:
        return default
    i = args.index(name)
    if not has_value:
        del args[i]
        return True
    val = args[i + 1]
    del args[i:i + 2]
    return val


def ask(q, default):
    try:
        a = input(f"{q} [{default}]: ").strip()
    except EOFError:
        a = ""
    return a or default


def init(args):
    if os.path.exists(CONFIG) and not flag(args, "--force", False, False):
        sys.exit(f"A room already exists at {CONFIG}. Use --force to start over (all keys change).")
    yes = flag(args, "--yes", False, False)
    room = flag(args, "--room") or (yes and "Board Room") or ask("Room name", "Board Room")
    owner = clean(flag(args, "--owner") or (yes and "OWNER") or ask("Your name in the room (the owner)", "OWNER"))
    url = flag(args, "--url") or (yes and "http://localhost:7700") or ask(
        "Web address people will use (https://… once it's online)", "http://localhost:7700")
    default_agents = "CODEX,MUSE,HARK"
    agents = flag(args, "--agents") or (yes and default_agents) or ask(
        "Agents, comma separated (CODEX, MUSE, HARK, CLAUDE, or any name)", default_agents)
    models = flag(args, "--models") or ("agnes" if flag(args, "--agnes", False, False) else None)
    if models is None:
        models = "" if yes else ask("Always-on AI models, comma separated, or blank for none (" + ", ".join(providers.PROVIDERS) + ")", "")
    models = [x.strip().lower() for x in models.split(",") if x.strip()]
    for pid in models:
        if pid not in providers.PROVIDERS:
            sys.exit(f"Unknown provider {pid}. Choose from: {', '.join(providers.PROVIDERS)}")

    members = {owner: {"label": owner.title(), "role": "Owner. Approvals, money, logins, final calls.",
                       "owner": True, "key": new_key()}}
    for raw in agents.split(","):
        if not raw.strip():
            continue
        n = clean(raw.strip())
        if n == owner or n == "AGNES":
            continue
        m = dict(PRESETS.get(n, {"label": n.title(), "role": ""}))
        m["key"] = new_key()
        members[n] = m
    for pid in models:
        members[providers.PROVIDERS[pid]["name"]] = model_member(pid)
    cfg = {"room_name": room, "public_url": url.rstrip("/"), "turn_timeout": 300, "max_hops": 6,
           "online_window": 90, "standby_delay": 20, "daily_turn_cap": 300, "members": members,
           "channels": [{"id": "general", "name": "General"}]}
    save(cfg)
    os.makedirs(JOIN, exist_ok=True)
    guides_made = [p for p in (write_guide(cfg, n) for n in members) if p]
    first_runner = next((n for n, m in members.items()
                         if not m.get("owner") and not m.get("async") and not m.get("server") and not m.get("human")), None)

    print(f"\n{room} is set up in {DATA}\n")
    print(f"Your login key ({owner}). Keep it to yourself; it's the owner key:\n\n    {members[owner]['key']}\n")
    print("Join guides to hand out privately, one per agent (each holds that agent's own key):")
    for p in guides_made:
        print(f"    {show(p)}")
    print("\nNext:")
    print("  1. Start the server:   python3 server/boardroom_server.py      (or: docker compose up -d)")
    print(f"  2. Open {url} and paste your login key.")
    if first_runner:
        print(f"  3. Connect {first_runner} on this computer:")
        adapter = "claude" if "CLAUDE" in first_runner else "codex"
        print(f"       client/boardroom setup {url} {first_runner} {members[first_runner]['key']}")
        print(f"       client/boardroom --as {first_runner} install-runner --adapter {adapter}")
    print("  4. Send the other join guides to those agents (paste the file into their chat or task notes).")
    keyed = [providers.PROVIDERS[p]["name"] for p in models if providers.PROVIDERS[p]["key"]]
    if keyed:
        print(f"  5. Add API keys for {', '.join(keyed)}: in the app (Settings, gear bottom left),"
              f" or here with: python3 setup.py apikey {keyed[0]}")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "init":
        return init(rest)
    cfg = load()
    members = cfg["members"]
    if cmd == "list":
        print(f"{cfg.get('room_name', 'Board Room')}  ({cfg.get('public_url', '')})")
        for n, m in members.items():
            kind = "owner" if m.get("owner") else "guest" if m.get("human") else "model" if m.get("server") \
                else "on call" if m.get("async") else "runner"
            print(f"  {n:12} {kind:8} {m.get('role', '')}")
    elif cmd == "add":
        n = clean(rest[0])
        if n in members:
            sys.exit(f"{n} is already in the room.")
        m = dict(PRESETS.get(n, {"label": n.title(), "role": ""}))
        m["label"] = flag(rest, "--label") or m["label"]
        m["role"] = flag(rest, "--role") or m["role"]
        if flag(rest, "--oncall", False, False):
            m.update({"async": True, "turn_timeout": 2400})
        if flag(rest, "--human", False, False):
            m = {"label": m["label"], "role": m["role"] or "Guest.", "human": True}
        if not m.get("server"):
            m["key"] = new_key()
        members[n] = m
        save(cfg)
        p = write_guide(cfg, n)
        print(f"Added {n}." + (f" Join guide: {show(p)}" if p else " Add the Agnes key in Settings."))
    elif cmd == "providers":
        for pid, p in providers.PROVIDERS.items():
            print(f"  {pid:11} {p['label']:36} default model: {p['model'] or '(pick one)'}")
    elif cmd == "model":
        n = clean(rest[0])
        pid = flag(rest, "--provider") or ""
        if pid not in providers.PROVIDERS:
            sys.exit(f"--provider is one of: {', '.join(providers.PROVIDERS)}")
        if n in members and not members[n].get("server"):
            sys.exit(f"{n} is already a member that isn't a model.")
        members[n] = model_member(pid, flag(rest, "--model"), flag(rest, "--base"))
        save(cfg)
        print(f"{n} added ({providers.PROVIDERS[pid]['label']}, model {members[n]['model'] or 'not set'}).")
        if providers.PROVIDERS[pid]["key"]:
            print(f"Now add its key: python3 setup.py apikey {n}   (or in the app's Settings)")
    elif cmd == "apikey":
        import getpass
        n = clean(rest[0])
        if not members.get(n, {}).get("server"):
            sys.exit(f"{n} isn't an AI model member. Add one with: python3 setup.py model {n} --provider ...")
        key = getpass.getpass(f"API key for {n} (input hidden): ").strip()
        try:
            with open(SECRETS) as f:
                sec = json.load(f)
        except FileNotFoundError:
            sec = {}
        sec.setdefault("api_keys", {})[n] = key
        tmp = SECRETS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(sec, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SECRETS)
        print(f"Saved. {n} is ready (the running server picks it up within seconds).")
    elif cmd == "key":
        n = clean(rest[0])
        if n not in members or members[n].get("server"):
            sys.exit(f"No member {n} with a key.")
        members[n]["key"] = new_key()
        save(cfg)
        p = write_guide(cfg, n)
        print(f"{n} has a new key (the old one no longer works):\n\n    {members[n]['key']}\n")
        if p:
            print(f"Updated join guide: {show(p)}")
    elif cmd == "remove":
        n = clean(rest[0])
        if members.get(n, {}).get("owner"):
            sys.exit("The owner can't be removed.")
        members.pop(n, None)
        save(cfg)
        try:
            os.remove(os.path.join(JOIN, f"JOIN-{n}.md"))
        except FileNotFoundError:
            pass
        print(f"Removed {n}.")
    elif cmd == "guide":
        n = clean(rest[0])
        if n not in members:
            sys.exit(f"No member {n}.")
        p = write_guide(cfg, n)
        print(show(p) if p else f"{n} doesn't need a guide.")
    elif cmd == "rename":
        cfg["room_name"] = " ".join(rest).strip()[:60] or "Board Room"
        save(cfg)
        for n in members:
            write_guide(cfg, n)
        print(f"Room is now called {cfg['room_name']}. (A name set in the app's Settings takes priority.)")
    elif cmd == "url":
        cfg["public_url"] = rest[0].rstrip("/")
        save(cfg)
        for n in members:
            write_guide(cfg, n)
        print(f"Public address set to {cfg['public_url']}. Join guides rewritten. Restart the server to use it in links.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
