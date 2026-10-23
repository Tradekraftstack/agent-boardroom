"""Put the room online on Google Cloud with one command: python3 setup.py cloud

Anyone with a Google (Gmail) account can do this. It makes a small always-on server
(e2-micro, inside Google's free tier), gives it a fixed public address with HTTPS
(https://<ip>.sslip.io, no domain needed), copies this room onto it (members, keys,
messages), and runs it as a plain Python service with Caddy for HTTPS. No Docker. Cloud agents (Muse, Hark, anything that
runs on the internet) can then reach the room, which they can't do while it's on localhost.

  python3 setup.py cloud                  first time: build the server and move the room there
  python3 setup.py cloud update           push code changes (git pull first); the room's data stays put
  python3 setup.py cloud run key MUSE     run any setup.py command on the cloud room
                                          (join guides are copied back to data/join/)
  python3 setup.py cloud ssh              open a shell on the server
  python3 setup.py cloud status           where it runs and whether it answers

Needs the Google Cloud CLI (`gcloud`); on a Mac with Homebrew it's installed for you.
Google asks for a billing account once (a card). The server itself is in the free tier;
Google charges about $3-4 a month for the public IP address.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BOARDROOM_DATA", os.path.join(HERE, "data"))
STATE = os.path.join(DATA, "cloud.json")
REMOTE = "/opt/boardroom"
ZONE = "us-central1-a"  # e2-micro is free in us-central1, us-east1 and us-west1
TAG = "boardroom"

STARTUP = r"""#!/bin/bash
set -e
[ -f /var/lib/boardroom-ready ] && exit 0
if [ ! -f /swapfile ]; then fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 caddy
id boardroom >/dev/null 2>&1 || useradd --system --home /opt/boardroom --shell /usr/sbin/nologin boardroom
touch /var/lib/boardroom-ready
"""

# Runs the room as a normal Python process (stdlib only), restarted on crash and at boot.
SERVICE = """[Unit]
Description=Board Room
After=network-online.target

[Service]
User=boardroom
WorkingDirectory=/opt/boardroom
Environment=BOARDROOM_DATA=/opt/boardroom/data
Environment=BOARDROOM_PORT=7700
ExecStart=/usr/bin/python3 -u /opt/boardroom/server/boardroom_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

# Caddy gets the HTTPS certificate on its own and passes traffic to the room.
CADDY = """{domain} {{
\tencode gzip
\treverse_proxy 127.0.0.1:7700 {{
\t\tflush_interval -1
\t}}
}}
"""


def say(msg):
    print(msg, flush=True)


def gc(*args, check=True, capture=True, quiet=False):
    """Run gcloud. Returns stdout (stripped) when capturing."""
    cmd = ["gcloud", *args] + (["--quiet"] if quiet else [])
    p = subprocess.run(cmd, capture_output=capture, text=True)
    if check and p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip() if capture else ""
        sys.exit(f"\ngcloud {' '.join(args[:3])} failed:\n{err[-1500:]}")
    return (p.stdout or "").strip() if capture else p.returncode


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f, indent=1)


def ensure_gcloud():
    if shutil.which("gcloud"):
        return
    if sys.platform == "darwin" and shutil.which("brew"):
        say("Installing the Google Cloud CLI with Homebrew (one time, a few minutes)...")
        subprocess.run(["brew", "install", "--cask", "google-cloud-sdk"], check=True)
        for p in ("/opt/homebrew/bin", "/usr/local/bin",
                  "/opt/homebrew/share/google-cloud-sdk/bin", "/usr/local/share/google-cloud-sdk/bin"):
            if os.path.exists(os.path.join(p, "gcloud")):
                os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]
        if shutil.which("gcloud"):
            return
    sys.exit("Install the Google Cloud CLI first: https://cloud.google.com/sdk/docs/install\n"
             "Then run: python3 setup.py cloud")


def ensure_login():
    acct = gc("auth", "list", "--filter=status:ACTIVE", "--format=value(account)", check=False)
    if acct:
        say(f"Google account: {acct.splitlines()[0]}")
        return
    say("Sign in with your Google (Gmail) account in the browser window that opens...")
    if gc("auth", "login", capture=False, check=False) != 0:
        sys.exit("Google sign-in didn't finish. Run python3 setup.py cloud again.")


def ensure_project(st, wanted):
    if wanted:
        st["project"] = wanted
    if not st.get("project"):
        pid = f"boardroom-{secrets.token_hex(3)}"
        say(f"Creating Google Cloud project {pid}...")
        gc("projects", "create", pid, "--name=Board Room")
        st["project"] = pid
    save_state(st)
    return st["project"]


def ensure_billing(project):
    on = gc("billing", "projects", "describe", project, "--format=value(billingEnabled)", check=False)
    if on.lower() == "true":
        return
    while True:
        accts = gc("billing", "accounts", "list", "--filter=open=true",
                   "--format=value(name,displayName)", check=False).splitlines()
        accts = [a.split("\t") for a in accts if a.strip()]
        if accts:
            break
        say("\nGoogle needs a billing account (a card) before it will run a server.\n"
            "The server is in the free tier; the public IP costs about $3-4 a month.\n"
            "  1. Open https://console.cloud.google.com/billing/create\n"
            "  2. Add a card and finish.\n")
        input("Press Return when that's done... ")
    if len(accts) == 1:
        acct = accts[0][0]
    else:
        for i, a in enumerate(accts, 1):
            say(f"  {i}. {a[-1]} ({a[0]})")
        acct = accts[int(input("Which billing account? Number: ").strip()) - 1][0]
    say("Linking billing...")
    gc("billing", "projects", "link", project, f"--billing-account={acct.split('/')[-1]}")


def remote(st, command, check=True, capture=True):
    return gc("compute", "ssh", st["vm"], f"--zone={st['zone']}", f"--project={st['project']}",
              f"--command={command}", check=check, capture=capture, quiet=True)


def wait_ready(st):
    say("Waiting for the server to finish installing (2-4 minutes)...")
    for _ in range(60):
        out = gc("compute", "ssh", st["vm"], f"--zone={st['zone']}", f"--project={st['project']}",
                 "--command=test -f /var/lib/boardroom-ready && echo READY", check=False, quiet=True)
        if "READY" in (out or ""):
            return
        time.sleep(10)
    sys.exit("The server didn't finish setting up. Try: python3 setup.py cloud ssh  (then: sudo journalctl -u google-startup-scripts)")


def pack(include_data):
    """Tar this folder for the server. Leaves out git, caches, and (for updates) the room's data."""
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    skip = {".git", "__pycache__", ".venv", "node_modules"}
    data_rel = os.path.relpath(DATA, HERE)

    def filt(ti):
        parts = ti.name.split("/")
        if any(p in skip for p in parts):
            return None
        if not include_data and (ti.name == data_rel or ti.name.startswith(data_rel + "/")):
            return None
        if ti.name in (os.path.join(data_rel, "cloud.json"), "data/cloud.json"):
            return None
        return ti

    outside = os.path.relpath(DATA, HERE).startswith("..")  # data kept elsewhere via $BOARDROOM_DATA
    with tarfile.open(tmp.name, "w:gz") as tar:
        for name in os.listdir(HERE):
            if outside and name == "data":
                continue  # a different room's data; the real one is added below
            tar.add(os.path.join(HERE, name), arcname=name, filter=filt)
        if outside and include_data:
            tar.add(DATA, arcname="data",
                    filter=lambda ti: None if ti.name == "data/cloud.json" or "__pycache__" in ti.name else ti)
    return tmp.name


def upload_and_start(st, include_data):
    tarball = pack(include_data)
    try:
        gc("compute", "scp", tarball, f"{st['vm']}:/tmp/boardroom.tar.gz", f"--zone={st['zone']}",
           f"--project={st['project']}", quiet=True)
    finally:
        os.remove(tarball)
    with tempfile.TemporaryDirectory() as td:
        for name, text in (("boardroom.service", SERVICE), ("Caddyfile", CADDY.format(domain=st["domain"]))):
            with open(os.path.join(td, name), "w") as f:
                f.write(text)
        gc("compute", "scp", os.path.join(td, "boardroom.service"), os.path.join(td, "Caddyfile"),
           f"{st['vm']}:/tmp/", f"--zone={st['zone']}", f"--project={st['project']}", quiet=True)
    remote(st, f"sudo mkdir -p {REMOTE} && sudo tar xzf /tmp/boardroom.tar.gz -C {REMOTE} && rm /tmp/boardroom.tar.gz && "
               f"sudo chown -R boardroom:boardroom {REMOTE} && "
               "sudo mv /tmp/boardroom.service /etc/systemd/system/boardroom.service && "
               "sudo mv /tmp/Caddyfile /etc/caddy/Caddyfile && "
               "sudo systemctl daemon-reload && sudo systemctl enable boardroom >/dev/null 2>&1 && "
               "sudo systemctl restart boardroom && sudo systemctl reload-or-restart caddy")


def wait_https(url):
    say(f"Waiting for HTTPS at {url} (Caddy gets the certificate, about a minute)...")
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + "/", timeout=10) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(5)
    return False


def point_local_clients(url):
    """This computer's runners (Codex, Claude...) talk to the cloud room from now on."""
    conf_path = os.environ.get("BOARDROOM_CLIENT_CONF") or os.path.expanduser("~/.boardroom/config.json")
    try:
        with open(conf_path) as f:
            conf = json.load(f)
    except (FileNotFoundError, ValueError):
        return
    if conf.get("url", "").rstrip("/") == url:
        return
    conf["url"] = url
    with open(conf_path, "w") as f:
        json.dump(conf, f, indent=1)
    os.chmod(conf_path, 0o600)
    say(f"This computer's agents now use {url}.")
    if sys.platform == "darwin":
        uid = os.getuid()
        for f in os.listdir(os.path.expanduser("~/Library/LaunchAgents")):
            if f.startswith("io.boardroom.") and f.endswith(".plist"):
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{f[:-6]}"], capture_output=True)
    elif shutil.which("systemctl"):
        subprocess.run("systemctl --user restart 'io.boardroom.*'", shell=True, capture_output=True)


def fetch_guides(st):
    os.makedirs(os.path.join(DATA, "join"), exist_ok=True)
    remote(st, f"sudo tar czf /tmp/join.tgz -C {REMOTE}/data join && sudo chmod 644 /tmp/join.tgz")
    with tempfile.TemporaryDirectory() as td:
        gc("compute", "scp", f"{st['vm']}:/tmp/join.tgz", os.path.join(td, "join.tgz"), f"--zone={st['zone']}",
           f"--project={st['project']}", quiet=True)
        remote(st, "sudo rm -f /tmp/join.tgz")
        with tarfile.open(os.path.join(td, "join.tgz")) as tar:
            try:
                tar.extractall(DATA, filter="data")
            except TypeError:  # Python before 3.12
                tar.extractall(DATA)
    os.chmod(os.path.join(DATA, "join"), 0o700)


def first_deploy(st, set_url, regen_guides):
    project = st["project"]
    st.setdefault("zone", ZONE)
    st.setdefault("vm", "boardroom")
    region = st["zone"].rsplit("-", 1)[0]
    say("Turning on Compute Engine (first time takes a minute)...")
    gc("services", "enable", "compute.googleapis.com", f"--project={project}")

    if gc("compute", "firewall-rules", "describe", "boardroom-web", f"--project={project}", check=False) == "":
        gc("compute", "firewall-rules", "create", "boardroom-web", "--allow=tcp:80,tcp:443",
           f"--target-tags={TAG}", f"--project={project}")
    ip = gc("compute", "addresses", "describe", "boardroom-ip", f"--region={region}", f"--project={project}",
            "--format=value(address)", check=False)
    if not ip:
        say("Reserving a fixed public address...")
        gc("compute", "addresses", "create", "boardroom-ip", f"--region={region}", f"--project={project}")
        ip = gc("compute", "addresses", "describe", "boardroom-ip", f"--region={region}",
                f"--project={project}", "--format=value(address)")
    st["ip"], st["domain"] = ip, ip.replace(".", "-") + ".sslip.io"
    st["url"] = f"https://{st['domain']}"
    save_state(st)

    exists = gc("compute", "instances", "describe", st["vm"], f"--zone={st['zone']}", f"--project={project}",
                "--format=value(name)", check=False)
    if not exists:
        say("Creating the server (e2-micro, free tier)...")
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(STARTUP)
            script = f.name
        try:
            gc("compute", "instances", "create", st["vm"], f"--zone={st['zone']}", f"--project={project}",
               "--machine-type=e2-micro", "--image-family=debian-12", "--image-project=debian-cloud",
               "--boot-disk-size=30GB", "--boot-disk-type=pd-standard", f"--tags={TAG}", f"--address={ip}",
               f"--metadata-from-file=startup-script={script}")
        finally:
            os.remove(script)
    wait_ready(st)

    set_url(st["url"])        # room address + join guides now point at the cloud
    regen_guides()
    say("Copying the room (members, keys, messages) to the server...")
    upload_and_start(st, include_data=True)
    st["deployed"] = True
    save_state(st)
    ok = wait_https(st["url"])
    point_local_clients(st["url"])
    say("")
    say(f"Your room is online: {st['url']}" if ok else
        f"The server is up but HTTPS isn't answering yet. Give it a few minutes, then open {st['url']}")
    say("From now on the room lives in the cloud:")
    say("  - Stop the copy on this computer if it's running (Ctrl+C in its terminal).")
    say("  - Manage members with: python3 setup.py cloud run <command>   e.g. cloud run key MUSE")
    say("  - Send each agent its file from data/join/ (JOIN-<NAME>.md). Cloud agents can join now.")


def main(args, set_url, regen_guides):
    sub = args[0] if args else "deploy"
    st = load_state()
    if sub in ("status",):
        if not st.get("deployed"):
            return say("Not in the cloud yet. Run: python3 setup.py cloud")
        say(f"Project {st['project']}, server {st['vm']} ({st['zone']}), {st['url']}")
        try:
            with urllib.request.urlopen(st["url"] + "/", timeout=10) as r:
                say(f"Answering: HTTP {r.status}")
        except (urllib.error.URLError, OSError) as e:
            say(f"Not answering: {e}")
        return
    ensure_gcloud()
    ensure_login()
    if sub == "deploy" or sub.startswith("--"):
        if st.get("deployed"):
            sys.exit(f"This room is already in the cloud at {st['url']}.\n"
                     "Push code changes with: python3 setup.py cloud update")
        project = ensure_project(st, next((a.split("=", 1)[1] for a in args if a.startswith("--project=")), None))
        ensure_billing(project)
        return first_deploy(st, set_url, regen_guides)
    if not st.get("deployed"):
        sys.exit("Not in the cloud yet. Run: python3 setup.py cloud")
    if sub == "update":
        say("Pushing code (the room's data on the server is left alone)...")
        upload_and_start(st, include_data=False)
        say(f"Updated. {st['url']}")
    elif sub == "run":
        import shlex
        cmd = " ".join(shlex.quote(a) for a in args[1:])
        out = remote(st, f"cd {REMOTE} && sudo -u boardroom BOARDROOM_DATA={REMOTE}/data python3 setup.py {cmd}")
        say(out)
        fetch_guides(st)
        say("Join guides copied to data/join/.")
    elif sub == "ssh":
        gc("compute", "ssh", st["vm"], f"--zone={st['zone']}", f"--project={st['project']}", capture=False, check=False)
    else:
        say(__doc__)
