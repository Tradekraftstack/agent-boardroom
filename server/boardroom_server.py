#!/usr/bin/env python3
"""Board Room: one live chat for you and all of your AI agents, split into project channels.
Python stdlib only. Name it whatever you like (room_name in config, or Settings in the app).

Agents never poll with AI tokens. A runner (or any agent) holds a cheap HTTP
long-poll on /api/listen; the server answers it only when that agent is needed
(an @mention, a round from the owner, or an adjourn). Then the agent speaks once.

Server-side members (Agnes) are called straight from here through their API.
Agnes can also route: when the owner talks to everyone, she picks who's needed.
"""
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

import guides

DATA = os.environ.get("BOARDROOM_DATA", "./data")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BOARDROOM_PORT", "7700"))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj, private=False):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


CONFIG_PATH = os.path.join(DATA, "config.json")
CFG = load_json(CONFIG_PATH, None)
if not CFG:
    raise SystemExit(f"missing {CONFIG_PATH}. Run: python3 setup.py init")
MEMBERS, OWNER, AGENTS = {}, None, []
_cfg_seen = {"mtime": 0.0, "checked": 0.0}


def apply_members():
    """name -> {label, role, key, owner?, human?, async?, server?, turn_timeout?}"""
    global MEMBERS, OWNER, AGENTS
    MEMBERS = CFG["members"]
    OWNER = next(n for n, m in MEMBERS.items() if m.get("owner"))
    AGENTS = [n for n, m in MEMBERS.items() if n != OWNER and not m.get("human")]


def reload_config():
    """Pick up keys added with setup.py without a restart (checked at most every 2 s)."""
    global CFG
    if time.time() - _cfg_seen["checked"] < 2:
        return
    _cfg_seen["checked"] = time.time()
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if mtime == _cfg_seen["mtime"]:
        return
    fresh = load_json(CONFIG_PATH, None)
    if fresh and any(m.get("owner") for m in fresh.get("members", {}).values()):
        CFG = fresh
        apply_members()
    _cfg_seen["mtime"] = mtime


def save_config():
    save_json(CONFIG_PATH, CFG, private=True)
    _cfg_seen["mtime"] = os.path.getmtime(CONFIG_PATH)


apply_members()
_cfg_seen["mtime"] = os.path.getmtime(CONFIG_PATH)
PUBLIC_URL = os.environ.get("BOARDROOM_PUBLIC_URL", CFG.get("public_url", "")).rstrip("/")
TURN_TIMEOUT = CFG.get("turn_timeout", 300)
MAX_HOPS = CFG.get("max_hops", 6)
ONLINE_WINDOW = CFG.get("online_window", 90)
STANDBY_DELAY = CFG.get("standby_delay", 20)
DAILY_CAP = CFG.get("daily_turn_cap", 300)
MAX_FILE = CFG.get("max_file_mb", 25) * 1024 * 1024
TEXT_INLINE = 60_000  # bytes of a text attachment shown to agents
LOG_PATH = os.path.join(DATA, "messages.jsonl")
STATE_PATH = os.path.join(DATA, "state.json")
ACTIONS_PATH = os.path.join(DATA, "actions.json")
SECRETS_PATH = os.path.join(DATA, "secrets.json")
SETTINGS_PATH = os.path.join(DATA, "settings.json")
MINUTES_DIR = os.path.join(DATA, "minutes")
FILES_DIR = os.path.join(DATA, "files")
os.makedirs(MINUTES_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)
DEFAULT_CHANNELS = CFG.get("channels", [{"id": "general", "name": "General"}])
AGNES_BASE = CFG.get("agnes_base", "https://apihub.agnes-ai.com/v1")
TEXT_EXT = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".html", ".py", ".js", ".ts", ".tsv", ".xml", ".log"}

COND = threading.Condition()
MSGS = []
TURNS = {}  # id -> turn
PRESENCE = {}  # agent -> {"t": ts, "runner": id}
STATE = load_json(STATE_PATH, {})
STATE.setdefault("next_turn", 1)
STATE.setdefault("next_action", 1)
STATE.setdefault("turn_counts", {})
STATE.setdefault("channels", DEFAULT_CHANNELS)
STATE.setdefault("bindings", {})  # agent -> channel -> {harness, session, host, open, ts}
STATE.setdefault("meetings", {})  # channel -> {"last_adjourn": id, "epoch": n, "adjourning": {...}|None}
if "last_adjourn" in STATE:  # migrate the single-room state
    STATE["meetings"]["general"] = {"last_adjourn": STATE.pop("last_adjourn"), "epoch": STATE.pop("epoch", 1),
                                    "adjourning": None}
    STATE.pop("adjourning", None)
ACTIONS = load_json(ACTIONS_PATH, [])
SECRETS = load_json(SECRETS_PATH, {})
SETTINGS = load_json(SETTINGS_PATH, {"route": True})


def room():
    return SETTINGS.get("room_name") or CFG.get("room_name") or "Board Room"


if os.path.exists(LOG_PATH):
    with open(LOG_PATH) as f:
        MSGS = [json.loads(line) for line in f if line.strip()]
for _m in MSGS:
    _m.setdefault("channel", "general")
for _a in ACTIONS:
    _a.setdefault("channel", "general")

MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_-]*)")


def now():
    return time.time()


def hhmm(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "channel"


def channel_name(cid):
    return next((c["name"] for c in STATE["channels"] if c["id"] == cid), cid)


def valid_channel(cid):
    return any(c["id"] == cid for c in STATE["channels"])


def meeting(cid):
    return STATE["meetings"].setdefault(cid, {"last_adjourn": 0, "epoch": 1, "adjourning": None})


def who(key):
    reload_config()
    if not key:
        return None
    for name, m in MEMBERS.items():
        if m.get("key") and hmac.compare_digest(str(m["key"]), key):
            return name
    return None


def is_async(agent):
    return bool(MEMBERS.get(agent, {}).get("async"))


def is_server(agent):
    return bool(MEMBERS.get(agent, {}).get("server"))


def agnes_ready():
    return bool(SECRETS.get("agnes_key")) and "AGNES" in MEMBERS


def timeout_for(agent):
    return MEMBERS.get(agent, {}).get("turn_timeout", TURN_TIMEOUT)


def online(agent):
    if is_server(agent):
        return agnes_ready()
    p = PRESENCE.get(agent)
    return bool(p and now() - p["t"] < ONLINE_WINDOW)


def reachable(agent):
    return online(agent) or is_async(agent)


def save_state():
    save_json(STATE_PATH, STATE)


# ---------- files

def file_path(fid):
    d = os.path.join(FILES_DIR, fid)
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,40}", fid) or not os.path.isdir(d):
        return None, None
    meta = load_json(os.path.join(d, "meta.json"), None)
    return (os.path.join(d, "blob"), meta) if meta else (None, None)


def file_url(meta):
    return f"{PUBLIC_URL}/files/{meta['id']}/{quote(meta['name'])}"


def files_for(ids):
    out = []
    for fid in ids or []:
        _, meta = file_path(str(fid))
        if meta:
            out.append(meta)
    return out[:10]


def attachment_text(meta):
    """What an agent sees for an attachment: text files inline, others as a link."""
    head = f"[attached: {meta['name']} ({meta['size'] // 1024 or 1} KB) {file_url(meta)}]"
    ext = os.path.splitext(meta["name"])[1].lower()
    if ext in TEXT_EXT or meta.get("type", "").startswith("text/"):
        blob, _ = file_path(meta["id"])
        try:
            with open(blob, "rb") as f:
                raw = f.read(TEXT_INLINE + 1)
            body = raw[:TEXT_INLINE].decode("utf-8", "replace")
            more = "\n…(truncated)" if len(raw) > TEXT_INLINE else ""
            return f"{head}\n----- {meta['name']} -----\n{body}{more}\n----- end of {meta['name']} -----"
        except OSError:
            return head
    return head


# ---------- messages and turns

def add_msg(frm, text, channel, kind="say", depth=0, turn=None, files=None):
    msg = {"id": (MSGS[-1]["id"] + 1) if MSGS else 1, "ts": now(), "channel": channel, "from": frm,
           "text": text, "kind": kind, "depth": depth, "turn": turn}
    if files:
        msg["files"] = files
    MSGS.append(msg)
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(msg) + "\n")
    COND.notify_all()
    return msg


def system(text, channel):
    return add_msg("ROOM", text, channel, kind="system")


def mentions(text, exclude):
    out, everyone = [], False
    for m in MENTION_RE.findall(text):
        name = m.upper()
        if name in ("ALL", "EVERYONE", "TEAM"):
            everyone = True
        elif name in AGENTS and name != exclude and name not in out:
            out.append(name)
    return out, everyone


def today_count(agent):
    day = datetime.now().strftime("%Y-%m-%d")
    counts = STATE["turn_counts"]
    if counts.get("day") != day:
        counts.clear()
        counts["day"] = day
    return counts, counts.get(agent, 0)


def queue_round(agents, trigger, depth, kind="reply"):
    """Queue turns that run one after another so each agent sees the last.
    On-call (async) and server agents don't hold up the round."""
    made, prev, ch = [], None, trigger["channel"]
    for a in agents:
        if is_server(a) and not agnes_ready():
            system(f"{a} has no API key yet. {OWNER} can add it in Settings.", ch)
            continue
        counts, n = today_count(a)
        if n >= DAILY_CAP:
            system(f"{a} hit its daily cap of {DAILY_CAP} turns, skipped.", ch)
            continue
        counts[a] = n + 1
        tid = STATE["next_turn"]
        STATE["next_turn"] += 1
        after = None if (is_async(a) or is_server(a)) else prev
        TURNS[tid] = {"id": tid, "agent": a, "trigger": trigger["id"], "channel": ch, "depth": depth,
                      "kind": kind, "after": after, "status": "queued", "created": now(),
                      "ready_at": now() if after is None else None, "claimed_by": None,
                      "claimed_at": None, "acked": False, "reply": None, "epoch": meeting(ch)["epoch"]}
        made.append(tid)
        if not (is_async(a) or is_server(a)):
            prev = tid
    save_state()
    COND.notify_all()
    return made


def finish_turn(t, status, reply_id=None):
    if t["status"] in ("done", "timeout", "noanswer"):
        return
    t["status"], t["reply"] = status, reply_id
    for other in TURNS.values():
        if other["after"] == t["id"] and other["ready_at"] is None:
            other["ready_at"] = now()
    COND.notify_all()
    check_adjourn(t["channel"])


def dispatch(msg, sender):
    """Decide who is needed after a message. Returns new turn ids."""
    if msg["kind"] in ("pass", "system", "error"):
        return []
    ch = msg["channel"]
    named, everyone = mentions(msg["text"], sender)
    depth = msg["depth"]
    if sender == OWNER:
        # The owner talks to the whole channel unless the message STARTS with @NAME(s).
        lead = re.match(r"^\s*((?:@[A-Za-z][A-Za-z0-9_-]*[\s,]*)+)", msg["text"])
        direct = mentions(lead.group(1), sender)[0] if lead else []
        if direct and not everyone:
            return queue_round(direct, msg, depth)
        if agnes_ready() and SETTINGS.get("route", True) and not everyone:
            threading.Thread(target=route_with_agnes, args=(msg,), daemon=True).start()
            return []
        return queue_everyone(msg, depth)
    if not named and not everyone:
        return []
    if depth >= MAX_HOPS:
        system(f"Hop limit ({MAX_HOPS}) reached in this thread. @{OWNER} needs to weigh in to continue.", ch)
        return []
    targets = named or [a for a in AGENTS if reachable(a) and a != sender]
    for a in targets:
        if not online(a) and not is_server(a):
            system(f"{a} isn't connected right now. The request waits {timeout_for(a) // 60} min.", ch)
    return queue_round(targets, msg, depth)


def queue_everyone(msg, depth):
    targets = [a for a in AGENTS if reachable(a) and not is_server(a)]
    away = [a for a in AGENTS if not reachable(a) and not is_server(a)]
    if away:
        system("Not in the room right now: " + ", ".join(away), msg["channel"])
    return queue_round(targets, msg, depth)


def render_line(x):
    line = f"[#{x['id']} {hhmm(x['ts'])} {x['from']}] {x['text'][:2000]}"
    for meta in x.get("files", []):
        line += "\n" + attachment_text(meta)
    return line


def context_for(agent, upto_id, ch):
    """This channel's messages since the agent last spoke there (at least 8, at most 40)."""
    chan = [m for m in MSGS if m["channel"] == ch]
    since = 0
    for m in reversed(chan):
        if m["from"] == agent and m["id"] <= upto_id:
            since = m["id"]
            break
    since = max(since, meeting(ch)["last_adjourn"])
    window = [m for m in chan if since < m["id"] <= upto_id + 200]
    if len(window) < 8:
        window = [m for m in chan if m["id"] <= upto_id + 200][-8:]
    return window[-40:]


def roster_text():
    lines = []
    for n, m in MEMBERS.items():
        tag = " (owner, human)" if n == OWNER else " (human)" if m.get("human") else ""
        lines.append(f"- {n}{tag}: {m.get('label', n)}. {m.get('role', '')}".rstrip())
    return "\n".join(lines)


def build_prompt(t):
    agent, ch = t["agent"], t["channel"]
    m = MEMBERS[agent]
    trig = next((x for x in MSGS if x["id"] == t["trigger"]), None)
    reason = ""
    if t["kind"] == "actions":
        reason = (f"{OWNER} is adjourning this project's meeting. List YOUR action items from it, one per line, "
                  "each starting with '- '. Only things you committed to do. If you have none, reply exactly: NONE")
    elif trig and trig["from"] == OWNER:
        reason = f"{OWNER} just spoke and you're needed. Reply to the channel."
    elif trig:
        reason = f"{trig['from']} pulled you in. Reply to the channel."
    direct = bool(trig and re.search(rf"@{agent}\b", trig["text"], re.I))
    if t["kind"] == "actions":
        pass_rule = "- If you committed to nothing, reply exactly: NONE"
    elif direct or t.get("routed"):
        pass_rule = "- You were picked for this, so always answer. Never reply PASS."
    else:
        pass_rule = "- If you truly have nothing useful to add, reply exactly: PASS"
    ctx = context_for(agent, t["trigger"], ch)
    convo = "\n".join(render_line(x) for x in ctx)
    how = ""
    if is_async(agent):
        how = (f"(You check in on a schedule. Answer this turn with POST /api/say "
               f"{{\"turn_id\": {t['id']}, \"text\": \"...\"}} using your Board Room key.)\n")
    files_rule = ("- Attached files appear inline (text) or as links. Fetch a link with your Board Room key "
                  "(header Authorization: Bearer <key>) if you need it.")
    return how + f"""[{room()} message] This arrives in your own working session for the {channel_name(ch)} project. Use everything you already know here. {OWNER} will ask you to do the actual work in this same session later, with your full tools.

You are {agent} ({m.get('label', agent)}) in {room()}: one live chat between {OWNER} (the owner, a human) and their AI agents, split into project channels.

This conversation is the **{channel_name(ch)}** channel. Stay on this project.

Members:
{roster_text()}

{reason}

Rules:
- Speak only as {agent}. Plain text, short: under 150 words unless {OWNER} asks for more.
- This is a meeting, not a work session. Don't start long jobs now. Say what you'll do and who you need; it becomes your action item.
- To pull another agent in, write @NAME with a specific question. Only @mention when you truly need them.
{files_rule}
{pass_rule}

Recent messages in {channel_name(ch)} (newest last):
{convo}

Your reply as {agent}:"""


def turn_payload(t):
    return {"turn_id": t["id"], "agent": t["agent"], "kind": t["kind"], "epoch": t["epoch"],
            "channel": t["channel"], "channel_name": channel_name(t["channel"]),
            "trigger": t["trigger"], "prompt": build_prompt(t)}


def ready_turn_for(agent, runner, standby, host=None):
    for t in sorted(TURNS.values(), key=lambda x: x["id"]):
        if t["agent"] != agent or t["ready_at"] is None:
            continue
        bound = STATE["bindings"].get(agent, {}).get(t["channel"])
        if host and bound and bound.get("host") and bound["host"] != host:
            continue  # this agent's memory for that project lives on the other Mac
        if t["status"] == "claimed" and t["claimed_by"] == runner:
            return t
        if t["status"] == "queued":
            if standby and now() - t["ready_at"] < STANDBY_DELAY:
                continue
            return t
    return None


def post_message(me, text, ch, kind="say", turn_id=None, file_ids=None):
    """Shared by the HTTP API and server-side agents. Caller holds COND."""
    text = re.sub(rf"^\**{re.escape(me)}\**\s*:\s*", "", text.strip()[:8000], flags=re.I)  # agents often sign "CODEX: ..."
    files = files_for(file_ids)
    if not text and not files:
        raise ValueError("empty")
    depth, turn = (0 if me == OWNER else 1), None
    if turn_id is not None:
        turn = TURNS.get(int(turn_id))
        if not turn or turn["agent"] != me:
            raise ValueError("not your turn")
        depth, ch = turn["depth"] + 1, turn["channel"]
    if not valid_channel(ch):
        raise ValueError(f"no channel '{ch}'")
    if kind not in ("say", "present", "error"):
        kind = "say"
    if text.upper().rstrip(".") in ("PASS", "NONE") and turn:
        kind = "pass"
    msg = add_msg(me, text, ch, kind=kind, depth=depth, turn=turn["id"] if turn else None, files=files)
    if turn:
        finish_turn(turn, "done", msg["id"])
    new_turns = [] if (turn and turn["kind"] == "actions") else dispatch(msg, me)
    return msg, new_turns


# ---------- Agnes (server-side, API)

def agnes_chat(prompt, max_tokens=900, temperature=0.4):
    body = json.dumps({"model": SECRETS.get("agnes_model", "agnes-2.0-flash"),
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    last = None
    for attempt in range(3):
        req = urllib.request.Request(f"{AGNES_BASE}/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {SECRETS['agnes_key']}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.loads(r.read())
            return (out["choices"][0]["message"]["content"] or "").strip()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last or "no answer")


AGNES_MODELS = ["agnes-2.0-flash", "agnes-2.5-flash", "agnes-2.5-pro-alpha", "agnes-1.5-flash"]


def agnes_check(key):
    """Prove the key with a real 1-token chat call (the models list is public). Returns (ok, model or reason)."""
    last = "no model answered"
    for model in AGNES_MODELS:
        body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}).encode()
        req = urllib.request.Request(f"{AGNES_BASE}/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                json.loads(r.read())
            return True, model
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, "Agnes rejected the key"
            last = f"HTTP {e.code} on {model}"
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
    return False, last


def route_with_agnes(msg):
    """Agnes reads the owner's message and picks the fewest agents needed."""
    ch = msg["channel"]
    with COND:
        pool = [a for a in AGENTS if reachable(a) and not is_server(a)]
        ctx = "\n".join(f"{x['from']}: {x['text'][:500]}" for x in context_for("AGNES", msg["id"], ch)[-6:])
        roster = "\n".join(f"- {a}: {MEMBERS[a].get('role', '')}" for a in pool + ["AGNES"])
    prompt = f"""You route messages in a team chat between {OWNER} (the boss) and their AI agents. Project channel: {channel_name(ch)}.

Agents available right now:
{roster}

Recent conversation:
{ctx}

{OWNER}'s new message:
{msg['text'][:3000]}

Pick the FEWEST agents needed to answer or act on this message (usually 1 or 2, at most 3).
If {OWNER} clearly asks everyone (a roll call, "everyone", "all of you", "each of you"), pick all of them.
Pick AGNES only for quick text answers, brainstorming, or summaries.
Reply with JSON only, like {{"agents": ["NAME"], "why": "five to ten words"}}"""
    picked, why = None, ""
    try:
        raw = agnes_chat(prompt, max_tokens=200, temperature=0)
        data = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))
        picked = [a.upper() for a in data.get("agents", []) if a.upper() in pool + ["AGNES"]]
        why = str(data.get("why", ""))[:140]
    except Exception:
        picked = None
    with COND:
        if not picked:
            queue_everyone(msg, msg["depth"])
            return
        system(f"Agnes routed this to {', '.join(picked)}" + (f": {why}" if why else ""), ch)
        tids = queue_round(picked, msg, msg["depth"])
        for tid in tids:
            TURNS[tid]["routed"] = True


def agnes_worker():
    while True:
        with COND:
            t = None
            while t is None:
                t = next((x for x in sorted(TURNS.values(), key=lambda x: x["id"])
                          if is_server(x["agent"]) and x["status"] == "queued" and x["ready_at"]), None)
                if t is None:
                    COND.wait(timeout=30)
            t["status"], t["claimed_by"], t["claimed_at"], t["acked"] = "claimed", "server", now(), True
            PRESENCE[t["agent"]] = {"t": now(), "runner": "server"}
            prompt = build_prompt(t)
        try:
            text, kind = agnes_chat(prompt) or "PASS", "say"
        except Exception as e:
            text, kind = f"(couldn't answer: {str(e)[:200]})", "error"
        with COND:
            try:
                post_message(t["agent"], text, t["channel"], kind=kind, turn_id=t["id"])
            except ValueError:
                pass


# ---------- adjourn

def parse_actions(text):
    items = []
    for line in text.splitlines():
        s = line.strip()
        if re.match(r"^([-*•]|\d+[.)])\s+", s):
            items.append(re.sub(r"^([-*•]|\d+[.)])\s+", "", s))
    return items


def start_adjourn(ch):
    mt = meeting(ch)
    if mt["adjourning"]:
        return False
    start = mt["last_adjourn"]
    spoke = [a for a in AGENTS if any(m["from"] == a and m["channel"] == ch and m["id"] > start
                                      and m["kind"] == "say" for m in MSGS)]
    targets = spoke or [a for a in AGENTS if reachable(a) and not is_server(a)]
    trig = system("Adjourning. Collecting action items from " + (", ".join(targets) or "nobody") + ".", ch)
    tids = queue_round(targets, trig, 0, kind="actions")
    mt["adjourning"] = {"turns": tids, "start": start, "trigger": trig["id"]}
    save_state()
    check_adjourn(ch)
    return True


def check_adjourn(ch):
    mt = meeting(ch)
    adj = mt.get("adjourning")
    if not adj:
        return
    if any(TURNS.get(t, {}).get("status") in ("queued", "claimed") for t in adj["turns"]):
        return
    start, stamp = adj["start"], datetime.now()
    new_items = []
    for tid in adj["turns"]:
        t = TURNS.get(tid)
        if not t or not t["reply"]:
            continue
        reply = next((m for m in MSGS if m["id"] == t["reply"]), None)
        for item in parse_actions(reply["text"] if reply else ""):
            new_items.append({"id": STATE["next_action"], "agent": t["agent"], "channel": ch, "text": item,
                              "status": "open", "created": now(), "meeting": stamp.strftime("%Y-%m-%d %H:%M"),
                              "note": ""})
            STATE["next_action"] += 1
    ACTIONS.extend(new_items)
    save_json(ACTIONS_PATH, ACTIONS)
    fname = f"{stamp.strftime('%Y-%m-%d_%H%M')}_{ch}.md"
    lines = [f"# {channel_name(ch)}: {room()} minutes, {stamp.strftime('%A %b %d %Y, %H:%M')}", "",
             "## Action items", ""]
    for a in AGENTS:
        mine = [i for i in new_items if i["agent"] == a]
        if mine:
            lines.append(f"**{a}**")
            lines += [f"- [ ] #{i['id']} {i['text']}" for i in mine]
            lines.append("")
    if not new_items:
        lines += ["None.", ""]
    lines += ["## Transcript", ""]
    for m in MSGS:
        if m["channel"] == ch and start < m["id"] < adj["trigger"] and m["kind"] != "system":
            lines.append(f"**{m['from']}** ({hhmm(m['ts'])}): {m['text']}")
            for meta in m.get("files", []):
                lines.append(f"  - attached: [{meta['name']}]({file_url(meta)})")
            lines.append("")
    with open(os.path.join(MINUTES_DIR, fname), "w") as f:
        f.write("\n".join(lines))
    mt["last_adjourn"] = MSGS[-1]["id"] if MSGS else 0
    mt["epoch"] += 1
    mt["adjourning"] = None
    save_state()
    system(f"Meeting adjourned. {len(new_items)} action items. Minutes: /minutes/{fname}", ch)


def sweeper():
    while True:
        time.sleep(3)
        with COND:
            for t in list(TURNS.values()):
                if t["status"] == "queued" and t["ready_at"] and now() - t["ready_at"] > timeout_for(t["agent"]):
                    finish_turn(t, "noanswer")
                    system(f"{t['agent']} didn't pick up, moving on.", t["channel"])
                elif t["status"] == "claimed" and not t["acked"] and now() - t["claimed_at"] > 15:
                    # runner vanished before confirming (dead connection): offer it to the next runner
                    t["status"], t["claimed_by"], t["claimed_at"] = "queued", None, None
                    t["ready_at"] = now() - STANDBY_DELAY
                    COND.notify_all()
                elif t["status"] == "claimed" and now() - t["claimed_at"] > timeout_for(t["agent"]) + 30:
                    finish_turn(t, "timeout")
                    system(f"{t['agent']} took too long, moving on.", t["channel"])
            old = [k for k, t in TURNS.items() if t["status"] not in ("queued", "claimed") and now() - t["created"] > 86400]
            for k in old:
                del TURNS[k]


def member_status(ch=None):
    out = []
    for n, m in MEMBERS.items():
        mine = [t for t in TURNS.values() if t["agent"] == n and (ch is None or t["channel"] == ch)]
        busy = any(t["status"] == "claimed" for t in mine)
        waiting = sum(1 for t in mine if t["status"] == "queued")
        out.append({"name": n, "label": m.get("label", n), "role": m.get("role", ""), "owner": n == OWNER,
                    "human": bool(m.get("human")), "online": n == OWNER or online(n),
                    "oncall": is_async(n), "server": is_server(n),
                    "busy": busy, "queued": waiting})
    return out


def channel_list():
    out = []
    for c in STATE["channels"]:
        chat = [m for m in MSGS if m["channel"] == c["id"] and m["kind"] != "system"]
        busy = any(t["channel"] == c["id"] and t["status"] in ("queued", "claimed") for t in TURNS.values())
        opened = sum(1 for a in ACTIONS if a["channel"] == c["id"] and a["status"] == "open")
        out.append({"id": c["id"], "name": c["name"], "last_id": chat[-1]["id"] if chat else 0,
                    "last_from": chat[-1]["from"] if chat else None, "count": len(chat),
                    "busy": busy, "actions": opened})
    return out


def manage_member(b):
    """Owner tools: add a member, give someone a fresh key (the old one stops working), or remove them.
    Every person and agent gets their own key, so access can be handed out and taken back one by one."""
    action = str(b.get("action", ""))
    name = re.sub(r"[^A-Z0-9_-]", "", str(b.get("name", "")).upper())[:24]
    members = CFG["members"]
    if not name or not name[0].isalpha():
        raise ValueError("names are letters, numbers, - or _, starting with a letter")
    if action == "add":
        if name in members or name == "ROOM":
            raise ValueError(f"{name} is already in the room")
        kind = str(b.get("kind", "runner"))
        m = {"label": str(b.get("label") or name.title())[:60], "role": str(b.get("role", ""))[:300]}
        if kind == "human":
            m["human"] = True
        elif kind == "oncall":
            m["async"], m["turn_timeout"] = True, 2400
        elif kind == "agnes":
            if name != "AGNES":
                raise ValueError("the server-side agent must be named AGNES")
            m.update(server=True, label="Agnes (Agnes AI)",
                     role=m["role"] or "Always on (runs on the server). Routes the room, quick answers, summaries.")
        members[name] = m
    elif action in ("rotate", "remove"):
        if name not in members:
            raise ValueError(f"no member {name}")
        if action == "remove" and members[name].get("owner"):
            raise ValueError("the owner can't be removed")
        if action == "remove":
            del members[name]
            save_config()
            apply_members()
            try:
                os.remove(os.path.join(DATA, "join", f"JOIN-{name}.md"))
            except OSError:
                pass
            return {"ok": True}
    else:
        raise ValueError("action must be add, rotate or remove")
    out = {"ok": True, "name": name}
    if not members[name].get("server"):
        members[name]["key"] = secrets.token_urlsafe(24)
        out["key"] = members[name]["key"]
        out["guide"] = guides.render(dict(CFG, room_name=room(), public_url=PUBLIC_URL or CFG.get("public_url")),
                                     name, out["key"])
        jdir = os.path.join(DATA, "join")  # same place setup.py keeps guides
        os.makedirs(jdir, mode=0o700, exist_ok=True)
        gpath = os.path.join(jdir, f"JOIN-{name}.md")
        with open(gpath, "w") as f:
            f.write(out["guide"])
        os.chmod(gpath, 0o600)
    save_config()
    apply_members()
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "BoardRoom/3"

    def log_message(self, fmt, *args):
        pass

    def key(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "br_key":
                return v
        return None

    def send(self, code, obj=None, ctype="application/json", body=None, headers=None):
        data = body if body is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 200_000:
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                page = f.read().replace(b"<title>Board Room</title>",
                                        f"<title>{html.escape(room())}</title>".encode())
            return self.send(200, ctype="text/html; charset=utf-8", body=page)
        if u.path == "/api/room":
            return self.send(200, {"room": room()})
        public_assets = {
            "/favicon.ico": "image/x-icon",
            "/icon-16.png": "image/png",
            "/icon-32.png": "image/png",
            "/icon-180.png": "image/png",
            "/icon-192.png": "image/png",
            "/icon-512.png": "image/png",
            "/site.webmanifest": "application/manifest+json",
        }
        if u.path in public_assets:
            with open(os.path.join(HERE, u.path[1:]), "rb") as f:
                return self.send(200, ctype=public_assets[u.path], body=f.read(),
                                 headers={"X-Content-Type-Options": "nosniff"})
        if u.path == "/healthz":
            return self.send(200, {"ok": True})
        me = who(self.key())
        if not me:
            return self.send(401, {"error": "login required"})
        if u.path == "/api/state":
            since = int(q.get("since", 0))
            ch = q.get("channel")
            with COND:
                msgs = [m for m in MSGS if m["id"] > since and (not ch or m["channel"] == ch)][-300:]
                open_actions = [a for a in ACTIONS if a["status"] == "open" and (not ch or a["channel"] == ch)]
                mt = meeting(ch) if ch else {}
                return self.send(200, {"me": me, "owner": OWNER, "room": room(), "members": member_status(ch),
                                       "channels": channel_list(), "messages": msgs,
                                       "last_id": MSGS[-1]["id"] if MSGS else 0, "actions": open_actions,
                                       "adjourning": bool(mt.get("adjourning"))})
        if u.path == "/api/channels":
            with COND:
                return self.send(200, {"channels": channel_list()})
        if u.path == "/api/settings":
            if me != OWNER:
                return self.send(403, {"error": "owner only"})
            k = SECRETS.get("agnes_key", "")
            return self.send(200, {"room": room(), "agnes_member": "AGNES" in MEMBERS,
                                   "agnes": {"set": bool(k), "last4": k[-4:] if k else "",
                                             "model": SECRETS.get("agnes_model", "agnes-2.0-flash")},
                                   "route": SETTINGS.get("route", True)})
        if u.path == "/api/members":
            if me != OWNER:
                return self.send(403, {"error": "owner only"})
            return self.send(200, {"members": [
                {"name": n, "label": m.get("label", n), "role": m.get("role", ""), "owner": bool(m.get("owner")),
                 "human": bool(m.get("human")), "oncall": bool(m.get("async")), "server": bool(m.get("server")),
                 "has_key": bool(m.get("key"))} for n, m in MEMBERS.items()]})
        if u.path.startswith("/files/"):
            parts = u.path.split("/")
            blob, meta = file_path(parts[2] if len(parts) > 2 else "")
            if not blob:
                return self.send(404, {"error": "not found"})
            with open(blob, "rb") as f:
                data = f.read()
            disp = "inline" if meta["type"].startswith(("image/", "text/", "application/pdf")) else "attachment"
            return self.send(200, ctype=meta["type"], body=data,
                             headers={"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(meta['name'])}",
                                      "X-Content-Type-Options": "nosniff",
                                      "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"})
        if u.path == "/api/sessions":
            ch = q.get("channel")
            with COND:
                out = []
                for agent, chans in STATE["bindings"].items():
                    for cid, b in chans.items():
                        if not ch or cid == ch:
                            out.append(dict(b, agent=agent, channel=cid))
                return self.send(200, {"sessions": out})
        if u.path == "/api/listen":
            if me == OWNER:
                return self.send(400, {"error": "the owner doesn't listen for turns"})
            runner = q.get("runner") or "anon"
            standby = q.get("standby") == "1"
            wait = max(0.0, min(float(q.get("wait", 25)), 55))
            deadline = now() + wait
            with COND:
                while True:
                    PRESENCE[me] = {"t": now(), "runner": runner}
                    t = ready_turn_for(me, runner, standby, q.get("host"))
                    if t:
                        if t["status"] == "queued":
                            t["status"], t["claimed_by"], t["claimed_at"] = "claimed", runner, now()
                            t["acked"] = False
                        return self.send(200, {"turn": turn_payload(t), "last_id": MSGS[-1]["id"] if MSGS else 0})
                    left = deadline - now()
                    if left <= 0:
                        return self.send(200, {"turn": None, "last_id": MSGS[-1]["id"] if MSGS else 0})
                    COND.wait(timeout=min(left, STANDBY_DELAY if standby else left))
        if u.path == "/api/turns":
            ids = [int(x) for x in q.get("ids", "").split(",") if x.strip().isdigit()]
            with COND:
                out = []
                for i in ids:
                    t = TURNS.get(i)
                    if not t:
                        out.append({"id": i, "status": "unknown"})
                        continue
                    reply = next((m for m in MSGS if m["id"] == t["reply"]), None) if t["reply"] else None
                    out.append({"id": i, "agent": t["agent"], "status": t["status"],
                                "reply": reply["text"] if reply else None})
                return self.send(200, {"turns": out})
        if u.path == "/api/actions":
            with COND:
                items = [a for a in ACTIONS if (not q.get("agent") or a["agent"] == q["agent"].upper())
                         and (not q.get("channel") or a["channel"] == q["channel"])
                         and (q.get("status", "open") == "all" or a["status"] == q.get("status", "open"))]
                return self.send(200, {"actions": items})
        if u.path.startswith("/minutes/"):
            name = os.path.basename(u.path)
            if not name:
                files = sorted(os.listdir(MINUTES_DIR), reverse=True)
                if q.get("channel"):
                    files = [f for f in files if f.endswith(f"_{q['channel']}.md")]
                return self.send(200, {"minutes": files})
            path = os.path.join(MINUTES_DIR, name)
            if not os.path.isfile(path):
                return self.send(404, {"error": "not found"})
            with open(path, "rb") as f:
                return self.send(200, ctype="text/markdown; charset=utf-8", body=f.read())
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self.upload()
        b = self.body()
        if b is None:
            return self.send(400, {"error": "bad body"})
        if u.path == "/api/login":
            name = who(str(b.get("key", "")))
            if not name:
                return self.send(401, {"error": "wrong key"})
            cookie = f"br_key={b['key']}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000"
            if self.headers.get("X-Forwarded-Proto") == "https":
                cookie += "; Secure"
            return self.send(200, {"me": name}, headers={"Set-Cookie": cookie})
        if u.path == "/api/logout":
            return self.send(200, {"ok": True}, headers={"Set-Cookie": "br_key=; Path=/; Max-Age=0"})
        me = who(self.key())
        if not me:
            return self.send(401, {"error": "login required"})
        if u.path == "/api/say":
            with COND:
                try:
                    msg, turns = post_message(me, str(b.get("text", "")), str(b.get("channel") or "general"),
                                              kind=b.get("kind", "say"), turn_id=b.get("turn_id"),
                                              file_ids=b.get("files"))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
                return self.send(200, {"message": msg, "turns": turns})
        if u.path == "/api/adjourn":
            if me != OWNER:
                return self.send(403, {"error": "only the owner can adjourn"})
            ch = str(b.get("channel") or "general")
            if not valid_channel(ch):
                return self.send(400, {"error": "no such channel"})
            with COND:
                return self.send(200, {"ok": start_adjourn(ch)})
        if u.path == "/api/channels":
            if me != OWNER:
                return self.send(403, {"error": "only the owner can add channels"})
            name = str(b.get("name", "")).strip()[:40]
            if not name:
                return self.send(400, {"error": "name required"})
            with COND:
                cid = slug(name)
                if not valid_channel(cid):
                    STATE["channels"].append({"id": cid, "name": name})
                    save_state()
                return self.send(200, {"id": cid, "channels": channel_list()})
        if u.path == "/api/members":
            if me != OWNER:
                return self.send(403, {"error": "owner only"})
            with COND:
                try:
                    return self.send(200, manage_member(b))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
        if u.path == "/api/settings":
            if me != OWNER:
                return self.send(403, {"error": "owner only"})
            if "room_name" in b:
                name = str(b["room_name"]).strip()[:60]
                if name:
                    SETTINGS["room_name"] = name
                else:
                    SETTINGS.pop("room_name", None)
                save_json(SETTINGS_PATH, SETTINGS)
            if "route" in b:
                SETTINGS["route"] = bool(b["route"])
                save_json(SETTINGS_PATH, SETTINGS)
            if "agnes_key" in b:
                key = str(b["agnes_key"]).strip()
                if not key:
                    SECRETS.pop("agnes_key", None)
                else:
                    ok, info = agnes_check(key)
                    if not ok:
                        return self.send(400, {"error": f"Agnes didn't accept that key ({info})."})
                    SECRETS["agnes_key"], SECRETS["agnes_model"] = key, info
                save_json(SECRETS_PATH, SECRETS, private=True)
                with COND:
                    COND.notify_all()
            return self.send(200, {"ok": True})
        if u.path == "/api/actions/done":
            with COND:
                item = next((a for a in ACTIONS if a["id"] == int(b.get("id", 0))), None)
                if not item or (me != OWNER and item["agent"] != me):
                    return self.send(404, {"error": "no such item for you"})
                item["status"], item["note"], item["done_at"] = "done", str(b.get("note", ""))[:2000], now()
                save_json(ACTIONS_PATH, ACTIONS)
                system(f"{me} closed action #{item['id']}: {item['text']}" + (f" ({item['note']})" if item["note"] else ""),
                       item["channel"])
                return self.send(200, {"ok": True})
        if u.path == "/api/sessions":
            if me == OWNER or is_server(me):
                return self.send(403, {"error": "agents only"})
            ch = str(b.get("channel") or "general")
            sid = str(b.get("session") or "")[:80]
            if not valid_channel(ch) or not sid:
                return self.send(400, {"error": "channel and session required"})
            with COND:
                old = STATE["bindings"].get(me, {}).get(ch, {})
                STATE["bindings"].setdefault(me, {})[ch] = {
                    "harness": str(b.get("harness", ""))[:20], "session": sid, "host": str(b.get("host", ""))[:80],
                    "open": str(b.get("open", ""))[:300], "ts": now()}
                save_state()
                if old.get("session") != sid:
                    system(f"{me}'s seat in #{channel_name(ch)} is its {b.get('harness', '')} session on "
                           f"{str(b.get('host', '')).split('.')[0]}. To work there: {b.get('open', '')}", ch)
                return self.send(200, {"ok": True})
        if u.path == "/api/ack":
            with COND:
                t = TURNS.get(int(b.get("turn_id", 0)))
                if not t or t["agent"] != me or t["status"] != "claimed" or t["claimed_by"] != b.get("runner"):
                    return self.send(409, {"error": "turn is no longer yours"})
                t["acked"] = True
                return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def upload(self):
        me = who(self.key())
        if not me:
            return self.send(401, {"error": "login required"})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_FILE:
            return self.send(413, {"error": f"files must be under {MAX_FILE // 1024 // 1024} MB"})
        name = os.path.basename(unquote(self.headers.get("X-Filename", "file")))[:120] or "file"
        name = re.sub(r"[\x00-\x1f/\\]", "_", name)
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if not ctype or ctype == "application/octet-stream":
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype in ("text/html", "image/svg+xml", "application/xhtml+xml"):
            ctype = "text/plain"  # never serve uploaded markup as a live page
        if os.path.splitext(name)[1].lower() == ".md":
            ctype = "text/markdown"
        fid = secrets.token_urlsafe(12)
        d = os.path.join(FILES_DIR, fid)
        os.makedirs(d)
        left = n
        with open(os.path.join(d, "blob"), "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        meta = {"id": fid, "name": name, "size": n - left, "type": ctype, "by": me, "ts": now()}
        save_json(os.path.join(d, "meta.json"), meta)
        return self.send(200, {"file": meta, "url": file_url(meta)})


if __name__ == "__main__":
    threading.Thread(target=sweeper, daemon=True).start()
    threading.Thread(target=agnes_worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"{room()} on :{PORT}, data in {DATA}, channels: {', '.join(c['id'] for c in STATE['channels'])}", flush=True)
    srv.serve_forever()
