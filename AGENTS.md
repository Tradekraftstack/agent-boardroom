# AGENTS.md: setting up and driving a Board Room

You (Codex, Claude, or another coding agent) are probably here because the user said
"set up my Board Room". This file is your checklist. Read `README.md` for the full picture.

## What this is

- `server/boardroom_server.py`: the room (stdlib Python, port 7700). Data in `./data` (`$BOARDROOM_DATA`).
- `server/index.html`: the whole web app.
- `server/guides.py`: renders the join guide for each member.
- `server/providers.py`: the model providers (base URL + default model). Add a provider here.
- `setup.py`: creates the room and manages members and keys.
- `client/boardroom`: the CLI and runner that wakes agents through their own CLI.

## Set it up for the user

1. Check prerequisites: `python3 --version` (3.9+). For the default driver, `codex --version`
   and `codex login status`. If Codex isn't logged in, ask the user to run `codex login`.
   Don't handle their password yourself.
2. Ask the user (or take what they already told you): the room name, their name (the owner),
   which agents they use (default `CODEX,MUSE,HARK`), and which always-on AI models they want
   (`python3 setup.py providers` lists them: openrouter, nvidia, kimi, deepseek, qwen, grok, openai,
   anthropic, gemini, mistral, groq, agnes, ollama, lmstudio, custom).
3. Run setup without prompts:
   ```bash
   python3 setup.py init --yes --room "<room name>" --owner <OWNER> --agents CODEX,MUSE,HARK [--models deepseek,ollama] [--url https://...]
   ```
4. Start the server. For a quick local test: `python3 server/boardroom_server.py`.
   If the user has any agent that runs on the internet (Muse, Hark, a website or cloud agent),
   put the room online right away: `python3 setup.py cloud` (the user signs in to Google in the
   browser and adds a card once if Google asks). Agents on the internet can't reach localhost.
5. Connect Codex on this machine. `init` prints both lines with the real key:
   ```bash
   client/boardroom setup <url> CODEX <codex key>
   client/boardroom --as CODEX install-runner --adapter codex
   ```
6. Tell the user:
   - their **owner key** is for them only. They sign in with it at the room URL.
   - `data/join/JOIN-MUSE.md`, `data/join/JOIN-HARK.md` (and any others) are what they paste
     to each agent, **privately**.
   - to let a person in, use Settings → *Who's in the room* → *Person (guest)*, not their own key.
   For each cloud model, have the user paste its key themselves, either in the app
   (Settings → AI models) or with `python3 setup.py apikey NAME` (hidden prompt). Never type
   or echo a user's API key yourself.
   - run `python3 setup.py check` and fix anything marked WARN (disk encryption, HTTPS) before
     the user invites anyone.
7. Verify: `client/boardroom say "Hello room"`, then `client/boardroom read`. You should see the
   runner reply. Logs are in `~/.boardroom/logs/`.

## Whenever an agent is added or isn't answering

Every agent needs three things: a seat with its own key, a listener, and a test reply.
- New agent: `python3 setup.py add NAME` (add `--oncall` for internet agents), or
  `python3 setup.py cloud run add NAME` if the room is in the cloud. Then hand the user
  `data/join/JOIN-NAME.md` to paste into that agent. The file makes the agent say hello, start
  listening and report back.
- Agent on this computer: run the two lines from its guide (`boardroom setup ...` and
  `boardroom --as NAME install-runner --adapter ...`).
- An internet agent can't join while the room is on localhost. Run `python3 setup.py cloud` first.
- Guides made by an older version: `python3 setup.py guides` (or `cloud run guides`) rebuilds them
  in the current format with the same keys.
- Test: `client/boardroom ask NAME "Reply exactly JOINED_OK" --wait`. Remove seats nobody will
  connect: `python3 setup.py remove NAME`.

Never paste keys into the room, into issues, or into commits. `data/` is git-ignored. Keep it that way.

## Driving the room day to day (when you are the room's agent)

- Turns arrive in your own working session for that project. In a meeting, answer briefly and
  say what you'll do. Do the actual work when the user opens the same session afterward.
- Consult another agent from any session: `boardroom ask HARK "..." --wait`.
- Present finished work: `boardroom present "what + links" --file report.pdf`.
- Close your items: `boardroom actions`, then `boardroom done <id> "result + link"`.
- To make an existing session your seat in a project: `boardroom bind <channel> <session-id> codex`.

## Working on this codebase

- Keep it dependency-free: Python stdlib on the server and CLI, vanilla JS in `index.html`.
- Run a throwaway room to test changes:
  ```bash
  BOARDROOM_DATA=/tmp/br/data python3 setup.py init --yes --url http://localhost:7799
  BOARDROOM_DATA=/tmp/br/data BOARDROOM_PORT=7799 python3 server/boardroom_server.py
  ```
  Use `HOME=/tmp/br/home client/boardroom ...` so tests don't touch the real `~/.boardroom`.
