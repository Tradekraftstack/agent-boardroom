#!/usr/bin/env python3
"""Set up and manage your Board Room. Python stdlib only.

  python3 setup.py init                      make a new room (asks a few questions)
  python3 setup.py init --yes --room "Acme HQ" --owner ALEX --url https://room.example.com --models openrouter,ollama
  python3 setup.py model NAME --provider grok [--model grok-4.7] [--base URL]   add an always-on AI model
  python3 setup.py apikey NAME               save that model's API key (asked for, hidden)
  python3 setup.py providers                 list model providers
  python3 setup.py list                      who's in the room
  python3 setup.py add NAME [--oncall|--human|--outside] [--role "..."] [--label "..."] [--channels general,x]
  python3 setup.py access NAME general,project-x   which channels a guest or outside agent can see
  python3 setup.py key NAME                  give NAME a fresh key + join guide (the old key stops working)
  python3 setup.py remove NAME
  python3 setup.py rename "New Room Name"
  python3 setup.py url https://room.example.com
  python3 setup.py check                     security check: disk encryption, HTTPS, who has access
  python3 setup.py backup [FILE]             password-locked backup of the whole room (AES-256)
  python3 setup.py restore FILE [--force]

Data lives in ./data (or $BOARDROOM_DATA). Every member gets their own random key,
so no two rooms share keys and nobody needs your key to get in. Keys are shown once,
in their join guide (data/join/); the room itself keeps only a scrambled fingerprint.
Hand each guide over privately. The running server picks up changes within a few
seconds, no restart needed.
"""
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

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


def set_key(m):
    """Give a member a fresh key. Only its fingerprint is stored; the key itself goes in the join guide."""
    key = new_key()
    m.pop("key", None)
    m["key_hash"] = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
    return key


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


def write_guide(cfg, name, key):
    m = cfg["members"][name]
    if m.get("server") or m.get("owner"):
        return None
    os.makedirs(JOIN, exist_ok=True)
    os.chmod(JOIN, 0o700)
    path = os.path.join(JOIN, f"JOIN-{name}.md")
    with open(path, "w") as f:
        f.write(guides.render(cfg, name, key))
    os.chmod(path, 0o600)
    return path


def update_guides(old, new):
    """Room renamed or moved: fix the text of existing guides (keys aren't stored, so guides aren't rebuilt)."""
    if not old or old == new or not os.path.isdir(JOIN):
        return
    for f in os.listdir(JOIN):
        path = os.path.join(JOIN, f)
        with open(path) as fh:
            text = fh.read()
        with open(path, "w") as fh:
            fh.write(text.replace(old, new))


def disk_encryption():
    """(ok, message) for the disk that holds the room's data."""
    if sys.platform == "darwin":
        out = subprocess.run(["fdesetup", "status"], capture_output=True, text=True).stdout.strip()
        if "On" in out:
            return True, "FileVault is on: the room's files are encrypted on disk."
        return False, ("FileVault is OFF. Turn it on (System Settings > Privacy & Security > FileVault) "
                       "so a lost or stolen Mac doesn't expose the room.")
    if sys.platform.startswith("linux"):
        out = subprocess.run(["lsblk", "-no", "TYPE"], capture_output=True, text=True).stdout
        if "crypt" in out:
            return True, "An encrypted (LUKS) volume is in use. Make sure the room's data folder is on it."
        return None, ("Couldn't find disk encryption. On a VPS, turn on your provider's volume encryption, "
                      "or put the data folder on a LUKS-encrypted volume.")
    return None, "Check that this computer's disk is encrypted (BitLocker on Windows)."


def check(cfg=None):
    print("Security check")
    ok, msg = disk_encryption()
    print(f"  [{'ok' if ok else 'WARN' if ok is False else '??'}] {msg}")
    if cfg:
        url = cfg.get("public_url", "")
        local = re.match(r"https?://(localhost|127\.|\[::1\])", url)
        if url.startswith("https://") or local:
            print(f"  [ok] Address: {url}")
        else:
            print(f"  [WARN] {url} isn't HTTPS. From the internet the room refuses plain HTTP; put it behind HTTPS.")
        if cfg.get("allow_http"):
            print("  [WARN] allow_http is on in config.json: unencrypted connections from the internet are allowed.")
        if any(m.get("key") for m in cfg["members"].values()):
            print("  [..] Some keys are still stored in plain text. Start the server once and it scrambles them.")
        else:
            print("  [ok] Keys are stored only as fingerprints.")
        outside = {n: m for n, m in cfg["members"].items() if m.get("human") or m.get("outside")}
        for n, m in outside.items():
            print(f"  [..] {n} ({'guest' if m.get('human') else 'outside agent'}) sees: {', '.join(m.get('channels', ['general']))}")
        if not outside:
            print("  [ok] No guests or outside agents.")
        models = [(n, providers.PROVIDERS.get(m.get("provider", "agnes"), {}).get("where", "?"))
                  for n, m in cfg["members"].items() if m.get("server")]
        for n, where in models:
            print(f"  [..] {n} sends what it's asked to: {where}")
    perms = oct(os.stat(DATA).st_mode)[-3:] if os.path.isdir(DATA) else "---"
    print(f"  [{'ok' if perms == '700' else 'WARN'}] Data folder permissions: {perms} (only you can read it)")


def openssl():
    path = shutil.which("openssl")
    if not path:
        sys.exit("Backups need the openssl command (it comes with macOS and most Linux).")
    return path


def backup(args):
    out = args[0] if args else f"boardroom-backup-{time.strftime('%Y%m%d-%H%M')}.tar.gz.enc"
    pw = getpass.getpass("Backup password (you'll need it to restore): ")
    if len(pw) < 10:
        sys.exit("Use at least 10 characters.")
    if getpass.getpass("Again: ") != pw:
        sys.exit("Passwords didn't match.")
    with tempfile.TemporaryDirectory() as td:
        tar = os.path.join(td, "room.tar.gz")
        with tarfile.open(tar, "w:gz") as t:
            t.add(DATA, arcname="data")
        subprocess.run([openssl(), "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-salt",
                        "-in", tar, "-out", out, "-pass", "env:BR_BACKUP_PASS"],
                       check=True, env=dict(os.environ, BR_BACKUP_PASS=pw))
    os.chmod(out, 0o600)
    print(f"Backup saved: {show(os.path.abspath(out))} (encrypted with AES-256; keep the password somewhere safe)")


def restore(args):
    if not args:
        sys.exit("usage: python3 setup.py restore FILE [--force]")
    force = flag(args, "--force", False, False)
    if os.path.exists(CONFIG) and not force:
        sys.exit(f"A room already exists at {DATA}. Add --force to replace it (move it aside first if unsure).")
    pw = getpass.getpass("Backup password: ")
    with tempfile.TemporaryDirectory() as td:
        tar = os.path.join(td, "room.tar.gz")
        r = subprocess.run([openssl(), "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
                            "-in", args[0], "-out", tar, "-pass", "env:BR_BACKUP_PASS"],
                           env=dict(os.environ, BR_BACKUP_PASS=pw), capture_output=True)
        if r.returncode != 0:
            sys.exit("Wrong password, or that isn't a Board Room backup.")
        with tarfile.open(tar) as t:
            names = t.getnames()
            if any(n.startswith("/") or ".." in n.split("/") or not (n == "data" or n.startswith("data/")) for n in names):
                sys.exit("That backup has unexpected paths; not restoring it.")
            t.extractall(td)
        if os.path.exists(DATA):
            shutil.rmtree(DATA)
        shutil.copytree(os.path.join(td, "data"), DATA)
    os.chmod(DATA, 0o700)
    print(f"Restored into {show(DATA)}. Restart the server.")


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

    members = {owner: {"label": owner.title(), "role": "Owner. Approvals, money, logins, final calls.", "owner": True}}
    plain = {owner: set_key(members[owner])}
    for raw in agents.split(","):
        if not raw.strip():
            continue
        n = clean(raw.strip())
        if n == owner or n == "AGNES":
            continue
        m = dict(PRESETS.get(n, {"label": n.title(), "role": ""}))
        plain[n] = set_key(m)
        members[n] = m
    for pid in models:
        members[providers.PROVIDERS[pid]["name"]] = model_member(pid)
    cfg = {"room_name": room, "public_url": url.rstrip("/"), "turn_timeout": 300, "max_hops": 6,
           "online_window": 90, "standby_delay": 20, "daily_turn_cap": 300, "members": members,
           "channels": [{"id": "general", "name": "General"}]}
    save(cfg)
    os.makedirs(JOIN, exist_ok=True)
    guides_made = [p for p in (write_guide(cfg, n, plain[n]) for n in plain) if p]
    first_runner = next((n for n, m in members.items()
                         if not m.get("owner") and not m.get("async") and not m.get("server") and not m.get("human")), None)

    print(f"\n{room} is set up in {DATA}\n")
    print(f"Your login key ({owner}). Keep it to yourself; it's the owner key. It's shown only this once:\n\n    {plain[owner]}\n")
    print("Join guides to hand out privately, one per agent (each holds that agent's own key):")
    for p in guides_made:
        print(f"    {show(p)}")
    print("\nNext:")
    print("  1. Start the server:   python3 server/boardroom_server.py      (or: docker compose up -d)")
    print(f"  2. Open {url} and paste your login key.")
    if first_runner:
        print(f"  3. Connect {first_runner} on this computer:")
        adapter = "claude" if "CLAUDE" in first_runner else "codex"
        print(f"       client/boardroom setup {url} {first_runner} {plain[first_runner]}")
        print(f"       client/boardroom --as {first_runner} install-runner --adapter {adapter}")
    print("  4. Send the other join guides to those agents (paste the file into their chat or task notes).")
    keyed = [providers.PROVIDERS[p]["name"] for p in models if providers.PROVIDERS[p]["key"]]
    if keyed:
        print(f"  5. Add API keys for {', '.join(keyed)}: in the app (Settings, gear bottom left),"
              f" or here with: python3 setup.py apikey {keyed[0]}")
    print()
    check(cfg)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = args[0], args[1:]
    if cmd == "init":
        return init(rest)
    if cmd == "restore":
        return restore(rest)
    cfg = load()
    members = cfg["members"]
    if cmd == "list":
        print(f"{cfg.get('room_name', 'Board Room')}  ({cfg.get('public_url', '')})")
        for n, m in members.items():
            kind = "owner" if m.get("owner") else "guest" if m.get("human") else "outside" if m.get("outside") \
                else "model" if m.get("server") \
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
        chans = (flag(rest, "--channels") or "general").split(",")
        if flag(rest, "--human", False, False):
            m = {"label": m["label"], "role": m["role"] or "Guest.", "human": True, "channels": chans}
        if flag(rest, "--outside", False, False):  # someone else's agent: chosen channels, can't trigger tool use
            m.update({"outside": True, "async": True, "turn_timeout": 2400, "channels": chans})
        key = None if m.get("server") else set_key(m)
        members[n] = m
        save(cfg)
        p = write_guide(cfg, n, key) if key else None
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
        key = set_key(members[n])
        save(cfg)
        p = write_guide(cfg, n, key)
        print(f"{n} has a new key (the old one no longer works). Shown only this once:\n\n    {key}\n")
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
        sys.exit("Keys are shown only once. For a fresh key and a new guide: python3 setup.py key NAME")
    elif cmd == "access":
        n = clean(rest[0])
        m = members.get(n)
        if not m or not (m.get("human") or m.get("outside")):
            sys.exit(f"{n} isn't a guest or outside agent (your own agents see every channel).")
        m["channels"] = [c.strip() for c in (rest[1] if len(rest) > 1 else "").split(",") if c.strip()]
        save(cfg)
        print(f"{n} can now see: {', '.join(m['channels']) or 'nothing'}")
    elif cmd == "check":
        check(cfg)
    elif cmd == "backup":
        backup(rest)
    elif cmd == "rename":
        old = cfg.get("room_name")
        cfg["room_name"] = " ".join(rest).strip()[:60] or "Board Room"
        save(cfg)
        update_guides(old, cfg["room_name"])
        print(f"Room is now called {cfg['room_name']}. (A name set in the app's Settings takes priority.)")
    elif cmd == "url":
        old = cfg.get("public_url")
        cfg["public_url"] = rest[0].rstrip("/")
        save(cfg)
        update_guides(old, cfg["public_url"])
        print(f"Public address set to {cfg['public_url']}. Join guides updated. Restart the server to use it in links.")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
