#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-telegram-bot>=21.0", "websockets>=14.0"]
# ///
"""herdr-remote Telegram bot — monitor and approve agents from Telegram."""
import asyncio, hashlib, html, json, os, logging, re, secrets
from collections import Counter, OrderedDict

from telegram import ForceReply, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, ContextTypes, filters

from agent_state import apply_agent_message
import approval_rules
import transcript

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO; that URL contains the bot token. Silence it.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("herdr-tg")

TOKEN = os.environ.get("HERDR_TG_TOKEN", "")
CHAT_ID = os.environ.get("HERDR_TG_CHAT_ID", "")
RELAY_WS = os.environ.get("HERDR_RELAY", "ws://127.0.0.1:8375")
RELAY_WS_SAFE = RELAY_WS.split("?", 1)[0]  # token-free variant for display and logging; never leak the token
_RELAY_TOKEN = RELAY_WS.split("token=", 1)[1] if "token=" in RELAY_WS else ""


def scrub(value) -> str:
    """Strip secrets from any string before it is logged or sent to Telegram.

    WebSocket exceptions (e.g. InvalidURI) embed the full relay URL incl. the
    ?token= query, so raw exception text must never be surfaced unredacted.
    """
    s = str(value)
    for secret in (_RELAY_TOKEN, TOKEN):
        if secret:
            s = s.replace(secret, "<redacted>")
    return s

if not TOKEN:
    print("Set HERDR_TG_TOKEN (from @BotFather)")
    exit(1)

# State
pending: OrderedDict[tuple[int, int], str] = OrderedDict()  # (chat_id, message_id) -> pane_id
# callback token -> (pane_id, `before` turn uuid or None): a turn uuid does not fit in 64 bytes of callback_data
history_cursors: OrderedDict[str, tuple[str, str | None]] = OrderedDict()
approval_tokens: dict[str, str] = {}  # pane_id -> current blocked-notification generation
blocked_prompt_ids: dict[str, str] = {}  # pane_id -> current relay prompt identity
# pane_id -> the live approval message we must edit (not re-notify) when it closes
approval_messages: dict[str, dict] = {}
# How many times you manually allowed the same subject before we offer a rule
RULE_OFFER_AFTER = 3
manual_approvals: Counter = Counter()  # (project, subject) -> manual allow count
rule_offers: dict[str, dict] = {}      # pane_id -> {project, subject, text} while the save button lives
# finish -> restart within this window reuses the same card (opencode flaps
# working/idle between turns; each flap must not become a new ping)
FLAP_GRACE = 30
# an opencode session with no DB updates for this long while "working" is suspect
STUCK_AFTER = 600
# one follow-up ping after an approval card sits unanswered this long
NUDGE_AFTER = 300

# --- Quiet projects: cards still arrive, but silently (no ping) ---

QUIET_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "herdr-remote", "quiet_projects.json")


def load_quiet() -> list:
    try:
        with open(QUIET_PATH, encoding="utf-8") as fh:
            quiet = json.load(fh)
        return quiet if isinstance(quiet, list) else []
    except (OSError, ValueError):
        return []


def save_quiet(quiet: list):
    os.makedirs(os.path.dirname(QUIET_PATH), exist_ok=True)
    with open(QUIET_PATH, "w", encoding="utf-8") as fh:
        json.dump(sorted(quiet), fh, indent=2)


def is_quiet(project: str) -> bool:
    return project in load_quiet()


# Public web dashboard base (deep-link buttons); '' disables them. Read from the
# shared config so the bot service needs no new env plumbing.
def _web_base() -> str:
    hostname = os.environ.get("HERDR_TUNNEL_HOSTNAME", "")
    if not hostname:
        try:
            with open(os.path.expanduser(
                    "~/.config/herdr-remote/config.env"), encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("HERDR_TUNNEL_HOSTNAME="):
                        hostname = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
    return f"https://{hostname}" if hostname else ""
# pane_id -> the live session card: created on start, edited in place while
# working, edited into the finished card at the end. One message per session.
working_cards: dict[str, dict] = {}
# pane_id -> the live multi-select question card: every toggle edits it in place
# (the relay re-broadcasts the new selection as an update), Submit closes it.
multi_cards: dict[str, dict] = {}
agents: list[dict] = []       # current agent list from relay
prev_statuses: dict[str, str] = {}  # pane_id -> last known status
relay_connected = False
daily_stats: dict[str, dict] = {}  # pane_id -> {agent, project, blocked_count, working_mins, last_change}

AGENT_PAGE_SIZE = 20
PENDING_LIMIT = 500
COMMAND_ACK_TIMEOUT = 20
PANE_READ_TIMEOUT = 20
# Terminal chrome that is not part of the output. Port of CHROME_RE (herdr_relay.py).
# herdr_telegram must not import herdr_relay (its import validates env and can SystemExit),
# so the pattern is copied, as the Windows client does (HerdrPoller.cs).
CHROME_RE = re.compile(
    r"^[\s\u2500-\u257f\u2580-\u259f\u2014\u2026_|\u25d4\u25d1\u25d5\u25cf\s]+$"
    r"|Kiro\s[\u00b7\u2022]"
    r"|esc to cancel"
    r"|type to queue"
    r"|^\s*[\u25d4\u25d1\u25d5\u25cf]\s+(Shell|Bash)"
    # pi's TUI chrome, ported with CHROME_RE: questionnaire hint row, token/cost
    # footer, MCP line, spinner divider, herdr widget sibling header/rows.
    r"|Enter to select"
    r"|\u2191\d[\d.,]*[kMG]?\s+\u2193\d"
    r"|MCP:\s*\d+ servers? enabled"
    r"|Herdr \u00b7 \d+ sibling"
    r"|^\s*[\u25cb\u25cf\u25d0-\u25d3]\s+(working|idle|blocked|done)\s"
    r"|^\s*\u2500+\s*[\u2800-\u28ff]\s"
)
# OpenCode's inline TUI marks structure on text lines: a `┃`/`│` rail prefixes
# user input and tool blocks (the agent's own reply is plain unrailled text),
# `▣` status, `+ Thought:` timers, `⚙` tool echoes, `ctrl+p` the footer hint.
TUI_MARK_RE = re.compile(
    r"^[\s\u2502\u2503]*[\u2502\u2503]\s"
    r"|^\s*\u25a3"
    r"|^\s*\+ Thought:"
    r"|^\s*\u2699"
    r"|^\s*\$ "
    r"|ctrl\+p"
    r"|shift\+tab"
)
# Claude Code's own chrome that carries text, so the two regexes above keep it:
# the block-letter banner, the ✻ status/timer line, the upgrade notice, and a
# bare prompt. What is left of the tail is the actual exchange: `❯ input`,
# `⏺ reply`, `⎿ result`.
CLAUDE_NOISE_RE = re.compile(
    r"^\s*[\u2580-\u259f]{2,}\s"          # banner rows: "▐▛███▛█ Claude Code v…", "▝▝   ▝▝   ~/…"
    r"|^\s*[\u273b\u273d\u273c\u2736]\s"    # "✻ Cooked for 4s · done 4:18 PM"
    r"|^\s*❯\s*$"                        # empty prompt
    r"|^\s*Update available"             # "Update available! Run: brew upgrade claude-code"
    r"|Allow rule\b.*allows any option"  # settings-lint warnings printed at startup
)
STATUS_ORDER = {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 3}
STATUS_LABELS = {"blocked": "BLOCKED", "working": "WORKING", "done": "DONE", "idle": "IDLE", "unknown": "IDLE"}
ACTION_CODES = {
    "read": "r",
    "interrupt": "i",
    "select_send": "s",
    "select_reply": "q",
    "trust": "t",
    "approval": "k",
    "approve": "o",
    "always": "y",
    "trust_project": "j",
    "reject": "x",
    "reply": "e",
    "save_rule": "v",
    "retry": "z",
    "q_toggle": "m",
    "q_submit": "u",
    "history": "h",
}
CODE_ACTIONS = {code: action for action, code in ACTION_CODES.items()}


# --- Relay communication ---

async def await_command_result(ws, request_id: str, command: str):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + COMMAND_ACK_TIMEOUT
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise RuntimeError(f"relay did not acknowledge {command}")
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"relay did not acknowledge {command}") from exc
        response = json.loads(raw)
        response_request_id = response.get("request_id")
        if response.get("type") == "command_result" and response_request_id != request_id:
            continue
        if response_request_id not in (None, request_id):
            continue
        if response.get("type") == "error":
            raise RuntimeError(response.get("message", f"relay rejected {command}"))
        if response.get("type") == "command_result" and response.get("command") == command:
            if not response.get("ok"):
                raise RuntimeError(response.get("message", f"relay rejected {command}"))
            return


async def send_to_relay(pane_id: str, text: str, prompt_id: str | None = None):
    """Send a response to the relay via WebSocket."""
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        await ws.send(json.dumps({
            "type": "respond",
            "pane_id": pane_id,
            "prompt_id": prompt_id if prompt_id is not None else blocked_prompt_ids.get(pane_id, ""),
            "text": text,
            "request_id": request_id,
        }))
        await await_command_result(ws, request_id, "respond")


async def send_keys_to_relay(pane_id: str, keys: list[str], prompt_id: str | None = None):
    """Send raw key presses to the relay via WebSocket (e.g. ["1"] to pick a prompt option)."""
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        message = {
            "type": "send_keys",
            "pane_id": pane_id,
            "keys": keys,
            "request_id": request_id,
        }
        if prompt_id is not None:
            message["prompt_id"] = prompt_id
        await ws.send(json.dumps(message))
        await await_command_result(ws, request_id, "send_keys")


async def question_action(pane_id: str, kind: str, option: str = "",
                          prompt_id: str | None = None) -> str | None:
    """Fire a question_toggle / question_submit at the relay and wait for its ack.

    The relay acks request_id'd question commands with a command_result (and
    errors carry the request_id too). Broadcasts tagged for other clients pass
    through this socket unheard. Returns the error text, or None on success /
    ack-timeout — the optimistic card state is authoritative until the relay's
    next poll corrects it either way.
    """
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        message = {
            "type": kind,
            "pane_id": pane_id,
            "prompt_id": prompt_id if prompt_id is not None else blocked_prompt_ids.get(pane_id, ""),
            "request_id": request_id,
        }
        if option:
            message["option"] = option
        await ws.send(json.dumps(message))
        loop = asyncio.get_running_loop()
        # Keys land in ~100ms locally; 5s covers a remote pane over ssh.
        deadline = loop.time() + 5
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            except Exception:
                return None  # socket dropped after the send; treat as delivered
            reply = json.loads(raw)
            if reply.get("request_id") != request_id:
                continue
            if reply.get("type") == "error":
                return reply.get("message", kind)
            if reply.get("type") == "command_result":
                return None if reply.get("ok") else reply.get("message", kind)


async def read_pane(pane_id: str, lines: int = 100) -> str:
    """Read pane content from relay, stripped of TUI chrome (borders, spinners)."""
    import websockets
    try:
        async with websockets.connect(RELAY_WS) as ws:
            await ws.send(json.dumps({
                "type": "read_pane", "pane_id": pane_id, "lines": lines, "source": "visible",
            }))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PANE_READ_TIMEOUT
            # The connect preamble (sessions/agents/one-blocked-per-blocked-
            # agent) can put any number of unrelated messages ahead of
            # pane_content. Skip by deadline, not by a fixed message count.
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "(no response)"
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "pane_content":
                    content = msg.get("content", "(empty)")
                    kept = [
                        stripped for stripped in (
                            line.strip() for line in content.splitlines()
                        )
                        if stripped
                        and not CHROME_RE.search(stripped)
                        and not TUI_MARK_RE.search(stripped)
                        and not CLAUDE_NOISE_RE.search(stripped)
                    ]
                    return "\n".join(kept[-50:]) if kept else "(no output)"
    except Exception as e:
        return f"(error reading pane: {scrub(e)})"


async def fetch_session_info(pane_id: str) -> dict | None:
    """Session details (context, cost, todos) from the relay, if the pane has any."""
    import websockets
    try:
        async with websockets.connect(RELAY_WS) as ws:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PANE_READ_TIMEOUT
            await ws.send(json.dumps({"type": "session_info", "pane_id": pane_id}))
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "session_info":
                    return msg.get("info")
    except Exception as e:
        log.warning("Failed to read session info: %s", scrub(e))
        return None


async def fetch_last_prompt(pane_id: str) -> dict | None:
    """The pane's last user prompt (for Retry), via the relay."""
    import websockets
    try:
        async with websockets.connect(RELAY_WS) as ws:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PANE_READ_TIMEOUT
            await ws.send(json.dumps({"type": "last_prompt", "pane_id": pane_id}))
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "last_prompt":
                    return msg.get("prompt")
    except Exception as e:
        log.warning("Failed to read last prompt: %s", scrub(e))
        return None


# ponytail: model->window map for the "22% used" line; unknown models show tokens only.
MODEL_CONTEXT_WINDOWS = {"glm-5.3": 1_000_000}

TODO_MARKS = {"in_progress": "[•]", "completed": "[x]", "pending": "[ ]"}


def session_details_lines(info: dict | None) -> list[str]:
    if not info:
        return []
    lines = []
    context = info.get("context_tokens") or 0
    if context:
        window = MODEL_CONTEXT_WINDOWS.get(info.get("model_id") or "")
        pct = f" · {round(context * 100 / window)}%" if window else ""
        import time
        since = time.strftime("%H:%M", time.localtime((info.get("created_ms") or 0) / 1000))
        lines.append(f"Context: {context:,} tokens{pct}")
        lines.append(f"${(info.get('cost') or 0):.2f} · since {since}")
    todos = [t for t in info.get("todos") or [] if t.get("status") != "cancelled"]
    if todos:
        done = sum(t.get("status") == "completed" for t in todos)
        lines += ["", f"Todo {done}/{len(todos)}"]
        lines += [
            f"{TODO_MARKS.get(t.get('status'), '[ ]')} {html.escape((t.get('content') or '')[:80])}"
            for t in todos[:6]
        ]
        if len(todos) > 6:
            lines.append(f"+{len(todos) - 6} more")
    return lines


async def fetch_sessions() -> list[dict] | None:
    """Per-host session lists from the relay's connect preamble."""
    import websockets
    try:
        async with websockets.connect(RELAY_WS) as ws:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PANE_READ_TIMEOUT
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "sessions":
                    return msg.get("sources", [])
    except Exception as e:
        log.warning("Failed to read sessions: %s", scrub(e))
        return None


async def fetch_history(pane_id: str, before: str | None = None) -> dict | None:
    """One page of the pane's transcript from the relay (`get_history`), oldest turn first."""
    import websockets
    request = {"type": "get_history", "pane_id": pane_id, "limit": HISTORY_FETCH, "include_tools": True}
    if before:
        request["before"] = before
    try:
        async with websockets.connect(RELAY_WS) as ws:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + PANE_READ_TIMEOUT
            await ws.send(json.dumps(request))
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("type") == "history" and msg.get("pane_id") == pane_id:
                    return msg
                if msg.get("type") == "error":
                    log.warning("History refused: %s", scrub(msg.get("message", "")))
                    return None
    except Exception as e:
        log.warning("Failed to read history: %s", scrub(e))
        return None


async def switch_session(host: str, session: str):
    """Point one host at a named session; raises on relay rejection."""
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        message = {"type": "session_switch", "session": session, "request_id": request_id}
        if host != "local":
            message["host"] = host
        await ws.send(json.dumps(message))
        await await_command_result(ws, request_id, "session_switch")


async def send_text_to_relay(pane_id: str, text: str):
    """Send text to an agent using 'herdr agent prompt' for proper submission."""
    if not text or len(text) > 10000:
        raise ValueError("text must contain 1-10000 characters")
    import websockets
    async with websockets.connect(RELAY_WS) as ws:
        request_id = secrets.token_hex(8)
        await ws.send(json.dumps({
            "type": "agent_prompt",
            "pane_id": pane_id,
            "text": text,
            "request_id": request_id,
        }))
        await await_command_result(ws, request_id, "agent_prompt")


# --- Auth guard ---

def authorized(update: Update) -> bool:
    """Reject messages from unauthorized users."""
    if not CHAT_ID:
        return True  # No restriction if CHAT_ID not set (first-run discovery mode)
    return str(update.effective_chat.id) == CHAT_ID


def register_pending(chat_id: int, message_id: int, pane_id: str):
    key = (int(chat_id), int(message_id))
    pending[key] = pane_id
    pending.move_to_end(key)
    while len(pending) > PENDING_LIMIT:
        pending.popitem(last=False)


def pending_pane(chat_id: int, message_id: int) -> str | None:
    return pending.get((int(chat_id), int(message_id)))


def find_agent(pane_id: str) -> dict | None:
    matches = [agent for agent in agents if agent.get("pane_id") == pane_id]
    return matches[0] if len(matches) == 1 else None


def clear_relay_connection_state():
    global agents, relay_connected
    relay_connected = False
    agents = []
    approval_tokens.clear()
    blocked_prompt_ids.clear()
    approval_messages.clear()
    working_cards.clear()
    multi_cards.clear()


async def send_reply_prompt(message, chat, agent: dict, content: str | None = None):
    if content is None:
        target = agent.get("project") or agent.get("agent") or "the agent"
        parts = [f"✎ Reply to this message to answer {target}."]
    else:
        parts = [pane_content_message(agent, content, limit=3000), "",
                 "Reply to this message to send your response."]
    kwargs = {}
    if getattr(chat, "type", None) == "private":
        kwargs["reply_markup"] = ForceReply(
            selective=True,
            input_field_placeholder=f"Reply to {agent.get('project') or agent.get('agent') or 'agent'}"[:64],
        )
    sent = await message.reply_text("\n".join(parts), parse_mode="HTML", **kwargs)
    register_pending(chat.id, sent.message_id, agent["pane_id"])
    return sent


STATUS_EMOJI = {"blocked": "◆", "working": "◐", "done": "✓", "idle": "○", "unknown": "○"}


def pane_content_message(agent: dict, content: str, limit: int = 3500) -> str:
    """One glanceable Telegram message: status header, then the agent's reply.

    HTML, not Markdown: agent output can contain * _ ` freely and must only be
    escaped, never interpreted.
    """
    status = agent.get("status", "unknown")
    project = agent.get("project") or agent.get("agent") or "agent"
    if len(content) > limit:
        content = content[-limit:]
    header = (
        f"{STATUS_EMOJI.get(status, '○')} <b>{html.escape(project)}</b>"
        f" · {html.escape(agent.get('agent', ''))} · <i>{STATUS_LABELS.get(status, 'IDLE')}</i>"
    )
    body = folded_output(content) if content else "<i>(no output yet)</i>"
    return f"{header}\n\n{body}"


FOLD_TAIL_LINES = 12


def folded_output(text: str, tail_lines: int = FOLD_TAIL_LINES) -> str:
    """Terminal text as HTML: the last lines in a <pre>, everything above them folded away.

    <pre> keeps the columns and box drawing a proportional font scrambles; the expandable
    quote keeps a long read from filling the chat. The quote holds escaped text only --
    Telegram lets no code entity sit inside a blockquote.
    """
    if not text.strip():
        return ""
    lines = text.splitlines()
    head, tail = lines[:-tail_lines], lines[-tail_lines:]
    pre = f"<pre>{html.escape(chr(10).join(tail))}</pre>"
    if not "".join(head).strip():
        return pre
    return f"<blockquote expandable>{html.escape(chr(10).join(head))}</blockquote>\n{pre}"


HISTORY_FETCH = 20
HISTORY_BUDGET = 3800       # Telegram caps a message at 4096 characters
HISTORY_TURN_CHARS = 1200
HISTORY_DIFF_LINES = 12
HISTORY_UNAVAILABLE = {
    "no-session": "This pane names no agent transcript.",
    "no-log": "The transcript file was not found.",
    "unsupported": "History is not supported for this agent.",
    "disabled": "History is disabled on the relay.",
    "error": "The relay could not read the transcript.",
}
INLINE_MD_RE = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*")
HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")
FENCE_LANG_RE = re.compile(r"^[\w+-]{1,20}$")


def md_inline(text: str) -> str:
    """Escape one line, turning `code` and **bold** into tags. Neither may hold the other."""
    out, pos = [], 0
    for match in INLINE_MD_RE.finditer(text):
        out.append(html.escape(text[pos:match.start()]))
        if match.group(1) is not None:
            out.append(f"<code>{html.escape(match.group(1))}</code>")
        else:
            out.append(f"<b>{html.escape(match.group(2))}</b>")
        pos = match.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def pre_block(body: str, lang: str = "") -> str:
    if not body.strip():
        return ""
    if FENCE_LANG_RE.match(lang):
        return f'<pre><code class="language-{lang}">{html.escape(body)}</code></pre>'
    return f"<pre>{html.escape(body)}</pre>"


def md_html(text: str) -> str:
    """Agent markdown as Telegram HTML: fenced code, inline code, bold, headings; the rest escaped."""
    out, code, lang = [], None, ""
    for line in text.split("\n"):
        stripped = line.strip()
        if code is not None:
            if stripped.startswith("```"):
                out.append(pre_block("\n".join(code), lang))
                code = None
            else:
                code.append(line)
            continue
        if stripped.startswith("```"):
            code, lang = [], stripped[3:].strip()
            continue
        heading = HEADING_RE.match(line)
        if heading:
            # Bold may not contain code, so a heading drops its inline marks.
            out.append(f"<b>{html.escape(heading.group(1).replace('**', '').replace('`', ''))}</b>")
        else:
            out.append(md_inline(line))
    if code is not None:  # a fence the clip cut open
        out.append(pre_block("\n".join(code), lang))
    return "\n".join(out).strip()


def turn_stamp(ts: str) -> str:
    """HH:MM in this host's zone -- transcripts are written in UTC -- with the date when not today."""
    from datetime import datetime
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except (TypeError, ValueError):
        return ""
    if moment.date() == datetime.now().astimezone().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%b %d %H:%M")


def history_turn_html(turn: dict, agent_name: str) -> str:
    role = turn.get("role")
    text = turn.get("text") or ""
    if len(text) > HISTORY_TURN_CHARS:
        text = text[:HISTORY_TURN_CHARS] + "…"
    if role == "tool":
        line = f"▸ <b>{html.escape(turn.get('tool') or 'tool')}</b>"
        if turn.get("target"):
            line += f" <code>{html.escape(turn['target'][:200])}</code>"
        if turn.get("error"):
            line += f"\n✕ {html.escape((turn.get('result') or 'error')[:200])}"
        diff = turn.get("diff")
        if diff:
            diff_lines = diff.split("\n")
            line += "\n" + pre_block("\n".join(diff_lines[:HISTORY_DIFF_LINES]), "diff")
            clipped = len(diff_lines) > HISTORY_DIFF_LINES or turn.get("diff_clipped")
            line += (f"<i>+{turn.get('added', 0)} −{turn.get('removed', 0)}"
                     f"{' · first lines only' if clipped else ''}</i>")
        return line
    if not text.strip():
        return ""
    stamp = turn_stamp(turn.get("ts") or "")
    stamp = f" · {stamp}" if stamp else ""
    if role == "user":
        quote = "blockquote expandable" if len(text) > 300 else "blockquote"
        return f"<b>You</b>{stamp}\n<{quote}>{html.escape(text)}</blockquote>"
    if role == "note":
        return f"<i>{html.escape(text)}</i>"
    return f"<b>{html.escape(agent_name or 'agent')}</b>{stamp}\n{md_html(text)}"


def history_text(agent: dict, body: dict) -> tuple[str, str | None]:
    """(HTML message, `before` cursor for the next older page or None).

    Fills the message newest turn first until the budget is spent, then shows them in
    reading order; the cursor is the oldest turn shown, so nothing is skipped between pages.
    """
    project = agent.get("project") or agent.get("agent") or "agent"
    title = body.get("title") or ""
    header = f"◷ <b>{html.escape(project)}</b>" + (f" · {html.escape(title[:80])}" if title else "")
    reason = body.get("unavailable")
    if reason:
        return f"{header}\n\n<i>{html.escape(HISTORY_UNAVAILABLE.get(reason, reason))}</i>", None
    messages = body.get("messages") or []
    blocks, used, oldest, cut = [], len(header), None, False
    for turn in reversed(messages):
        rendered = history_turn_html(turn, body.get("agent") or agent.get("agent", ""))
        if rendered and blocks and used + len(rendered) + 2 > HISTORY_BUDGET:
            cut = True
            break
        if rendered:
            blocks.append(rendered)
            used += len(rendered) + 2
        oldest = turn.get("uuid") or oldest
    if not blocks:
        return f"{header}\n\n<i>No conversation yet.</i>", None
    older = cut or bool(body.get("has_more"))
    text = "\n\n".join([header, *reversed(blocks)])
    if not older and body.get("file_truncated"):
        text += "\n\n<i>Only the end of the transcript was read; it starts earlier.</i>"
    return text, (oldest if older else None)


def history_callback(pane_id: str, before: str | None) -> str:
    token = secrets.token_hex(4)
    history_cursors[token] = (pane_id, before)
    while len(history_cursors) > PENDING_LIMIT:
        history_cursors.popitem(last=False)
    return json.dumps({"a": ACTION_CODES["history"], "c": token}, separators=(",", ":"))


def history_button(pane_id: str) -> list[InlineKeyboardButton]:
    """A ◷ History button, only where the relay can read a transcript: the pane names a
    session and its harness has a reader. Anywhere else it could only say "unavailable"."""
    agent = find_agent(pane_id)
    if not agent or not agent.get("has_session") or agent.get("agent") not in transcript.HARNESSES:
        return []
    return [InlineKeyboardButton("◷ History", callback_data=pane_callback_data("history", pane_id))]


def details_keyboard(pane_id: str) -> InlineKeyboardMarkup | None:
    button = history_button(pane_id)
    return InlineKeyboardMarkup([button]) if button else None


def history_keyboard(pane_id: str, older: str | None, paged: bool) -> InlineKeyboardMarkup | None:
    row = []
    if older:
        row.append(InlineKeyboardButton("↑ Older", callback_data=history_callback(pane_id, older)))
    if paged:
        row.append(InlineKeyboardButton("↓ Latest", callback_data=history_callback(pane_id, None)))
    return InlineKeyboardMarkup([row]) if row else None


async def send_history(message, chat, agent: dict, before: str | None = None, edit: bool = False):
    """Post one history page, or turn the page in place when `edit` (the Older/Latest buttons)."""
    body = await fetch_history(agent["pane_id"], before)
    if body is None:
        text, older = "Could not read history from the relay.", None
    else:
        text, older = history_text(agent, body)
    markup = history_keyboard(agent["pane_id"], older, paged=before is not None)
    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            log.warning("Failed to turn history page: %s", scrub(e))
        return
    sent = await message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    register_pending(chat.id, sent.message_id, agent["pane_id"])


def agents_for_action(action: str) -> list[dict]:
    if action == "interrupt":
        return [agent for agent in agents if agent.get("status") in ("working", "blocked")]
    if action == "trust":
        return [agent for agent in agents if agent.get("status") == "blocked"]
    return list(agents)


def sorted_agents(agent_list: list[dict]) -> list[dict]:
    return sorted(agent_list, key=lambda agent: (
        STATUS_ORDER.get(agent.get("status", "unknown"), 3),
        agent.get("project", "").lower(),
        agent.get("agent", "").lower(),
        agent.get("host", "local").lower(),
        agent.get("pane_id", ""),
    ))


def compact_identifier(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha1(value.encode()).hexdigest()[:12]
    return value[:limit - 15] + "..." + digest


def pane_callback_token(pane_id: str) -> str:
    return hashlib.sha256(pane_id.encode()).hexdigest()[:16]


def pane_callback_data(action: str, pane_id: str, **extra) -> str:
    data = {"a": ACTION_CODES[action], "p": pane_callback_token(pane_id), **extra}
    return json.dumps(data, separators=(",", ":"))


def resolve_pane_token(token: str) -> str | None:
    agent_matches = [
        agent.get("pane_id") for agent in agents
        if agent.get("pane_id") and pane_callback_token(agent["pane_id"]) == token
    ]
    if len(agent_matches) == 1:
        return agent_matches[0]
    if len(agent_matches) > 1:
        return None
    pending_matches = {
        pane_id for pane_id in pending.values()
        if pane_id and pane_callback_token(pane_id) == token
    }
    return next(iter(pending_matches)) if len(pending_matches) == 1 else None


def parse_callback_data(raw: str) -> dict:
    data = json.loads(raw)
    if "a" in data:
        data["action"] = CODE_ACTIONS.get(data["a"], "invalid")
    if "p" in data:
        data["pane_id"] = resolve_pane_token(data["p"])
    return data


def agent_button_labels(agent_list: list[dict]) -> list[str]:
    bases = []
    contexts = []
    for agent in agent_list:
        status = STATUS_LABELS.get(agent.get("status", "unknown"), "UNKNOWN")
        project = agent.get("project") or agent.get("cwd") or "unknown"
        name = agent.get("agent") or "agent"
        host = agent.get("host", "local")
        bases.append(f"[{status}] {project} ({name})")
        contexts.append(f" @{compact_identifier(host, 24)}" if host != "local" else "")

    provisional = [base[:max(1, 64 - len(context))] + context for base, context in zip(bases, contexts)]
    counts = Counter(provisional)
    labels = []
    for agent, base, context, candidate in zip(agent_list, bases, contexts, provisional):
        if counts[candidate] == 1:
            labels.append(candidate)
            continue
        pane_id = compact_identifier(agent.get("pane_id", "?"), 18)
        suffix = context + f" [{pane_id}]"
        labels.append(base[:max(1, 64 - len(suffix))] + suffix)
    unique_labels = []
    used = set()
    for label in labels:
        candidate = label
        ordinal = 2
        while candidate in used:
            marker = f" #{ordinal}"
            candidate = label[:64 - len(marker)] + marker
            ordinal += 1
        used.add(candidate)
        unique_labels.append(candidate)
    return unique_labels


def build_agent_keyboard(action: str, page: int = 0, agent_list: list[dict] | None = None) -> InlineKeyboardMarkup:
    ordered = sorted_agents(agents_for_action(action) if agent_list is None else agent_list)
    page_count = max(1, (len(ordered) + AGENT_PAGE_SIZE - 1) // AGENT_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * AGENT_PAGE_SIZE
    visible = ordered[start:start + AGENT_PAGE_SIZE]
    labels = agent_button_labels(ordered)[start:start + AGENT_PAGE_SIZE]
    keyboard = [[InlineKeyboardButton(
        label,
        callback_data=pane_callback_data(action, agent["pane_id"]),
    )] for agent, label in zip(visible, labels)]

    if page_count > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "Previous",
                callback_data=json.dumps(
                    {"action": "page", "menu": action, "page": page - 1}, separators=(",", ":")
                ),
            ))
        if page + 1 < page_count:
            navigation.append(InlineKeyboardButton(
                "Next",
                callback_data=json.dumps(
                    {"action": "page", "menu": action, "page": page + 1}, separators=(",", ":")
                ),
            ))
        keyboard.append(navigation)
    return InlineKeyboardMarkup(keyboard)


def refresh_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Refresh agents",
            callback_data=json.dumps({"action": "page", "menu": action, "page": 0}, separators=(",", ":")),
        )
    ]])


def dashboard_text() -> str:
    if not relay_connected:
        text = "herdr-remote bot\n\nRelay disconnected. Use /status for connection details."
    elif not agents:
        text = "herdr-remote bot\n\nConnected to relay. No agents are running."
    else:
        blocked = sum(agent.get("status") == "blocked" for agent in agents)
        working = sum(agent.get("status") == "working" for agent in agents)
        done = sum(agent.get("status") == "done" for agent in agents)
        idle = len(agents) - blocked - working - done
        text = f"herdr-remote bot\n\nAgents: {len(agents)} ({blocked} blocked, {working} working, {done} done, {idle} idle)\nSelect an agent to read and reply."
    if not CHAT_ID:
        text += "\n\nChat ID: {chat_id}"
    return text


# --- Bot commands ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not authorized(update):
        return
    text = dashboard_text()
    if not CHAT_ID:
        text = text.format(chat_id=update.effective_chat.id)
    if relay_connected and agents:
        await update.message.reply_text(text, reply_markup=build_agent_keyboard("select_reply"))
        return
    await update.message.reply_text(text)


async def cmd_agents(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /agents — list current agents with status."""
    if not agents:
        await update.message.reply_text("No agents connected." if relay_connected else "Not connected to relay.")
        return

    blocked = [a for a in agents if a.get("status") == "blocked"]
    working = [a for a in agents if a.get("status") == "working"]
    done = [a for a in agents if a.get("status") == "done"]
    idle = [a for a in agents if a.get("status") in ("idle", "unknown")]

    lines = []
    if blocked:
        lines.append("BLOCKED:")
        for a in blocked:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")
    if working:
        lines.append("WORKING:")
        for a in working:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")
    if done:
        lines.append("DONE:")
        for a in done:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")
    if idle:
        lines.append("IDLE:")
        for a in idle:
            host = f" @{a['host']}" if a.get('host', 'local') != 'local' else ''
            lines.append(f"  {a['project']} ({a['agent']}){host}")

    await update.message.reply_text("\n".join(lines))


async def cmd_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /now — one card per active session, on demand."""
    if not authorized(update):
        return
    if not relay_connected:
        await update.message.reply_text("Not connected to relay. Use /start after it reconnects.")
        return
    import time
    active = [a for a in agents if a.get("status") in ("working", "blocked")]
    if not active:
        await update.message.reply_text("No active sessions.")
        return
    for agent in sorted_agents(active):
        started = daily_stats.get(agent["pane_id"], {}).get("last_change")
        elapsed = time.time() - started if started else 0
        state = "paused" if agent.get("status") == "blocked" else "working"
        info = await fetch_session_info(agent["pane_id"])
        activity = (info or {}).get("activity") \
            or working_cards.get(agent["pane_id"], {}).get("activity", "") \
            or agent.get("title", "")
        await update.message.reply_text(
            working_text(agent, elapsed, activity, state=state,
                         details=session_details_lines(info),
                         branch=(info or {}).get("branch", "")),
            parse_mode="HTML",
            reply_markup=working_keyboard(agent["pane_id"]),
        )


async def cmd_quiet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /quiet [project] — toggle silent cards for a project."""
    if not authorized(update):
        return
    args = ctx.args
    if not args:
        quiet = load_quiet()
        names = [a.get("project") for a in agents if a.get("project")]
        known = sorted({p for p in names if p})
        lines = ["Quiet projects (cards arrive silently):",
                 ", ".join(quiet) if quiet else "(none)",
                 "", "Toggle with /quiet <project>.",
                 "Known projects: " + (", ".join(known) if known else "(none connected)")]
        await update.message.reply_text("\n".join(lines))
        return
    project = " ".join(args).strip()
    quiet = load_quiet()
    if project in quiet:
        quiet.remove(project)
        state = "removed — cards ping again"
    else:
        quiet.append(project)
        state = "added — cards arrive silently"
    save_quiet(quiet)
    await update.message.reply_text(f"{project}: {state}")


async def cmd_rules(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /rules — list and delete approval rules."""
    if not authorized(update):
        return
    await update.message.reply_text(rules_text(), parse_mode="HTML",
                                    reply_markup=rules_keyboard())


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /status — show connection info."""
    b = len([a for a in agents if a.get("status") == "blocked"])
    w = len([a for a in agents if a.get("status") == "working"])
    d = len([a for a in agents if a.get("status") == "done"])
    i = len([a for a in agents if a.get("status") in ("idle", "unknown")])

    status = "Connected" if relay_connected else "Disconnected"
    text = (
        f"Relay: {RELAY_WS_SAFE}\n"
        f"Status: {status}\n"
        f"Agents: {len(agents)} ({b} blocked, {w} working, {d} done, {i} idle)"
    )
    await update.message.reply_text(text)


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /session — list herdr sessions; /session <name> or /session <host> <name> switches."""
    args = ctx.args
    if not args:
        sources = await fetch_sessions()
        if sources is None:
            await update.message.reply_text("Could not read sessions from the relay.")
            return
        lines = []
        for source in sources:
            host = source.get("host", "?")
            active = source.get("active")
            entries = ", ".join(
                f"● {entry['name']}" if entry["name"] == active
                else entry["name"] + ("" if entry.get("running") else " (stopped)")
                for entry in source.get("sessions", [])
            ) or "none"
            suffix = "" if active else " (herdr default)"
            lines.append(f"<b>{html.escape(host)}</b>: {html.escape(entries)}{suffix}")
        await update.message.reply_text(
            "Sessions (● active):\n" + "\n".join(lines)
            + "\n\nSwitch: /session <name>, or /session <host> <name> for a remote.",
            parse_mode="HTML",
        )
        return

    host, session = ("local", args[0]) if len(args) == 1 else (args[0], " ".join(args[1:]))
    try:
        await switch_session(host, session)
    except Exception as e:
        await update.message.reply_text(f"Switch failed: {scrub(e)}")
        return
    await update.message.reply_text(f"Switched {host} to {session}. Use /agents to see its panes.")


async def cmd_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /read [project] — read pane output."""
    args = ctx.args
    if not args:
        # Show agent picker
        if not agents:
            await update.message.reply_text("No agents. Use /agents to check.")
            return
        await update.message.reply_text("Read which agent?", reply_markup=build_agent_keyboard("read"))
        return

    # Find agent by project name
    query = " ".join(args).lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'. Use /agents to see list.")
        return

    content = await read_pane(match["pane_id"])
    msg = await update.message.reply_text(pane_content_message(match, content), parse_mode="HTML",
                                          reply_markup=details_keyboard(match["pane_id"]))
    register_pending(update.effective_chat.id, msg.message_id, match["pane_id"])


async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /history [project] — the agent's conversation, read from its transcript."""
    args = ctx.args
    if not args:
        if not agents:
            await update.message.reply_text("No agents. Use /agents to check.")
            return
        await update.message.reply_text("History of which agent?", reply_markup=build_agent_keyboard("history"))
        return

    query = " ".join(args).lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'. Use /agents to see list.")
        return

    await send_history(update.message, update.effective_chat, match)


async def cmd_interrupt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /interrupt [project] — send Ctrl+C."""
    args = ctx.args
    if not args:
        if not agents:
            await update.message.reply_text("No agents.")
            return
        working = [a for a in agents if a.get("status") in ("working", "blocked")]
        if not working:
            await update.message.reply_text("No active agents to interrupt.")
            return
        await update.message.reply_text(
            "Interrupt which agent?",
            reply_markup=build_agent_keyboard("interrupt", agent_list=working),
        )
        return

    query = " ".join(args).lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'.")
        return

    try:
        await send_keys_to_relay(match["pane_id"], ["C-c"])
        await update.message.reply_text(f"Sent Ctrl+C to {match['project']}")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


async def cmd_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /send [project] [text] — send text + Enter to a pane."""
    args = ctx.args
    if not args:
        if not agents:
            await update.message.reply_text("No agents.")
            return
        await update.message.reply_text(
            "Send to which agent?\n(After selecting, reply with your text)",
            reply_markup=build_agent_keyboard("select_send"),
        )
        return

    query = args[0].lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'. Use /agents to see list.")
        return

    text = " ".join(args[1:])
    if not text:
        await send_reply_prompt(update.message, update.effective_chat, match)
        return

    try:
        await send_text_to_relay(match["pane_id"], text)
        await update.message.reply_text(f"Sent to {match['project']}: {text}")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


async def cmd_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /reply [project] — show agent output then accept input."""
    args = ctx.args

    if not agents:
        await update.message.reply_text("No agents.")
        return

    if not args:
        await update.message.reply_text("Reply to which agent?", reply_markup=build_agent_keyboard("select_reply"))
        return

    query = " ".join(args).lower()
    match = next((a for a in agents if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No agent matching '{query}'.")
        return

    content = await read_pane(match["pane_id"])
    await send_reply_prompt(update.message, update.effective_chat, match, content)


async def cmd_trust(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /trust [project] — send 'trust, always allow' to a blocked agent."""
    args = ctx.args
    blocked = [a for a in agents if a.get("status") == "blocked"]

    if not blocked:
        await update.message.reply_text("No blocked agents.")
        return

    if not args:
        await update.message.reply_text(
            "Trust which agent?",
            reply_markup=build_agent_keyboard("trust", agent_list=blocked),
        )
        return

    query = " ".join(args).lower()
    match = next((a for a in blocked if query in a.get("project", "").lower() or query in a.get("agent", "").lower()), None)
    if not match:
        await update.message.reply_text(f"No blocked agent matching '{query}'.")
        return

    try:
        await send_to_relay(
            match["pane_id"],
            "trust, always allow",
            prompt_id=blocked_prompt_ids.get(match["pane_id"]),
        )
        await update.message.reply_text(f"Trusted {match['project']} (always allow)")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


def digest_text() -> str:
    if not daily_stats:
        return "No activity recorded yet today."
    lines = ["Today's activity:\n"]
    sorted_agents = sorted(daily_stats.values(), key=lambda x: x.get("working_mins", 0), reverse=True)
    for s in sorted_agents:
        blocked = f", blocked {s['blocked_count']}x" if s.get("blocked_count") else ""
        auto = f", auto-approved {s['auto_approved']}x" if s.get("auto_approved") else ""
        mins = s.get("working_mins", 0)
        time_str = f"{mins}m" if mins < 60 else f"{mins//60}h{mins%60}m"
        lines.append(f"  {s['project']} ({s['agent']}): {time_str} working{blocked}{auto}")
    return "\n".join(lines)


def seconds_until(hhmm: str, now_ts: float) -> float:
    """Seconds from now_ts until the next local hh:mm occurrence."""
    import time
    hour, minute = (int(part) for part in hhmm.split(":", 1))
    local = time.localtime(now_ts)
    target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                          hour, minute, 0, local.tm_wday, local.tm_yday, -1))
    if target <= now_ts:
        target += 86400
    return target - now_ts


async def morning_digest_task(app: Application):
    """Push the daily digest once per day at HERDR_TG_DIGEST_AT (default 09:00)."""
    at = os.environ.get("HERDR_TG_DIGEST_AT", "09:00")
    if not CHAT_ID or not at:
        return
    while True:
        try:
            await asyncio.sleep(max(30, seconds_until(at, __import__("time").time())))
            if not daily_stats:
                continue
            await app.bot.send_message(chat_id=int(CHAT_ID), text=digest_text())
            daily_stats.clear()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Morning digest failed: %s", scrub(e))
            await asyncio.sleep(3600)


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /digest — show today's agent activity summary."""
    await update.message.reply_text(digest_text())


# --- Callback handler (buttons) ---

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    if CHAT_ID and str(update.effective_chat.id) != CHAT_ID:
        await query.answer("Unauthorized")
        return
    await query.answer()

    if not relay_connected:
        await query.message.reply_text("Relay disconnected. Use /start after it reconnects.")
        return

    data = parse_callback_data(query.data)
    action = data.get("action", "approval")

    if action == "page":
        menu = data.get("menu", "select_reply")
        if not agents_for_action(menu):
            await query.message.reply_text("No eligible agents are available.")
            return
        await query.edit_message_reply_markup(
            reply_markup=build_agent_keyboard(menu, page=data.get("page", 0))
        )
        return

    if action == "rule_del":
        rules = approval_rules.load()
        try:
            index = int(data.get("i", -1))
            removed = rules.pop(index)
        except (ValueError, IndexError, TypeError):
            removed = None
        if removed is None:
            await query.edit_message_text("That rule no longer exists.")
            return
        approval_rules.save(rules)
        try:
            await query.edit_message_text(
                rules_text(), parse_mode="HTML", reply_markup=rules_keyboard())
        except Exception:
            pass
        return

    if action == "save_rule":
        offer = rule_offers.pop(data.get("pane_id") or "", None)
        if not offer:
            await query.message.reply_text("That offer has expired.")
            return
        if approval_rules.is_dangerous(offer["subject"]):
            await query.message.reply_text("Too dangerous for a rule — it will always ask.")
            return
        approval_rules.add_command_rule(offer["project"], offer["subject"])
        try:
            await query.edit_message_text(
                offer.get("text", "∞") + "\n\n∞ Rule saved", parse_mode="HTML")
        except Exception:
            pass
        return

    if action == "history":
        # From the agent picker the pane rides in "p"; Older/Latest carry a cursor token
        # instead and turn the page in place.
        before = None
        if data.get("c"):
            cursor = history_cursors.get(data["c"])
            if cursor is None:
                await query.message.reply_text("That page has expired. Use /history again.")
                return
            pane_id, before = cursor
        else:
            pane_id = data.get("pane_id")
        agent = find_agent(pane_id) if pane_id else None
        if agent is None:
            await query.message.reply_text("That agent is no longer available. Use /history again.")
            return
        await send_history(query.message, update.effective_chat, agent, before, edit=bool(data.get("c")))
        return

    selected_agent = None
    if action in {"read", "interrupt", "select_send", "select_reply", "reply",
                  "trust", "approval", "retry"}:
        eligible_action = "trust" if action == "approval" else action
        selected_agent = next(
            (agent for agent in agents_for_action(eligible_action) if agent.get("pane_id") == data.get("pane_id")),
            None,
        )
        if selected_agent is None:
            await query.message.reply_text(
                "That agent is no longer available for this action. Refresh the list and choose another agent.",
                reply_markup=refresh_keyboard(eligible_action),
            )
            return

    if action in {"approve", "always", "trust_project", "reject"}:
        pane_id = data.get("pane_id")
        if not pane_id:
            await query.message.reply_text("That approval action is no longer valid.")
            return
        live = approval_messages.get(pane_id)
        stale = not data.get("g") or data["g"] != approval_tokens.get(pane_id)
        if stale or not blocked_prompt_ids.get(pane_id):
            # Answered elsewhere, or a newer prompt owns this pane: close the old
            # card in place instead of sending yet another notification.
            if live and query.message and query.message.message_id == live.get("message_id"):
                await edit_approval_message(ctx.bot, pane_id, "■ Closed")
            else:
                await query.message.reply_text(
                    "That approval belongs to an older prompt. Use the latest blocked notification."
                )
            return
        # Destructive subjects confirm once: the first press re-renders the same
        # card as a warning, the second executes. Reject always goes straight.
        subject = approval_subject(live["summary"], live["detail"]) if live else ""
        if (action in {"approve", "always", "trust_project"}
                and approval_rules.is_dangerous(subject)
                and not live.get("confirming")):
            live["confirming"] = True
            try:
                await ctx.bot.edit_message_text(
                    chat_id=live["chat_id"], message_id=live["message_id"],
                    text=approval_text(live["project"], live["host"], live["cwd"],
                                       live.get("branch", ""), live["summary"],
                                       live["detail"],
                                       "△ Destructive — press again to confirm"),
                    parse_mode="HTML",
                    reply_markup=approval_keyboard(pane_id, approval_tokens[pane_id]),
                )
            except Exception as e:
                log.warning("Failed to arm approval confirm: %s", scrub(e))
            return
        if action == "always" and live:
            approval_rules.add_command_rule(
                live.get("project", ""), approval_subject(live.get("summary", ""), live.get("detail")))
            outcome = "∞ Allowed · rule saved for this"
        elif action == "trust_project" and live:
            approval_rules.add_project_rule(live.get("project", ""))
            outcome = "⌂ Trusted project · allowed"
        elif action == "reject":
            outcome = "✕ Rejected"
        else:
            outcome = "✓ Allowed · once"
        try:
            await send_to_relay(pane_id, "Reject" if action == "reject" else "Allow once",
                                prompt_id=blocked_prompt_ids.get(pane_id))
        except Exception as e:
            await query.message.reply_text(f"Failed: {scrub(e)}")
            return
        approval_tokens.pop(pane_id, None)
        save_button = None
        if action == "approve":
            live = approval_messages.get(pane_id)
            if live:
                subject = approval_subject(live["summary"], live["detail"])
                manual_approvals[(live["project"], subject)] += 1
                if (manual_approvals[(live["project"], subject)] >= RULE_OFFER_AFTER
                        and not approval_rules.is_dangerous(subject)):
                    rule_offers[pane_id] = {"project": live["project"], "subject": subject}
                    save_button = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "∞ Allow always for this",
                            callback_data=pane_callback_data("save_rule", pane_id),
                        )
                    ]])
        await edit_approval_message(ctx.bot, pane_id, outcome, reply_markup=save_button)
        return

    if action == "read":
        content = await read_pane(data["pane_id"])
        msg = await query.message.reply_text(
            pane_content_message(selected_agent, content), parse_mode="HTML",
            reply_markup=details_keyboard(data["pane_id"]),
        )
        register_pending(update.effective_chat.id, msg.message_id, data["pane_id"])
        return

    if action in ("q_toggle", "q_submit"):
        pane_id = data.get("pane_id") or ""
        card = multi_cards.get(pane_id)
        if not card:
            await query.message.reply_text("That question has already been answered.")
            return

        async def render_card(submitted: bool = False):
            """Edit the live card to the card's current state."""
            try:
                await ctx.bot.edit_message_text(
                    chat_id=card["chat_id"], message_id=card["message_id"],
                    text=multi_question_text(
                        card["project"], card.get("question", ""),
                        card["options"], card.get("details"), card["selected"],
                        host=card.get("host", ""), submitted=submitted),
                    parse_mode="HTML",
                    reply_markup=multi_question_keyboard(
                        pane_id, card["options"], card["selected"]),
                )
                return True
            except Exception:
                return False

        if action == "q_toggle":
            try:
                index = int(data.get("i", -1))
                option = card["options"][index]
            except (ValueError, IndexError):
                await query.message.reply_text("That option no longer exists.")
                return
            # Optimistic: flip the visible state NOW, then drive the pane. The
            # relay's next poll re-renders the authoritative selection onto the
            # same message; on failure the flip is taken back.
            selected = card["selected"]
            card["selected"] = (
                [s for s in selected if s != option] if option in selected
                else [*selected, option]
            )
            await render_card()
            error = await question_action(pane_id, "question_toggle", option=option)
            if error:
                card["selected"] = selected  # take the flip back
                await render_card()
                await query.message.reply_text(f"Failed: {scrub(error)}")
                return
            return
        error = await question_action(pane_id, "question_submit")
        if error:
            await query.message.reply_text(f"Failed: {scrub(error)}")
            return
        card["submitted"] = True
        await render_card(submitted=True)
        return

    if action == "interrupt":
        try:
            await send_keys_to_relay(data["pane_id"], ["C-c"])
            await query.message.reply_text("Sent Ctrl+C")
        except Exception as e:
            await query.message.reply_text(f"Failed: {scrub(e)}")
        return

    if action == "select_send":
        await send_reply_prompt(query.message, update.effective_chat, selected_agent)
        return

    if action == "select_reply":
        content = await read_pane(data["pane_id"])
        await send_reply_prompt(query.message, update.effective_chat, selected_agent, content)
        return

    if action == "reply":
        await send_reply_prompt(query.message, update.effective_chat, selected_agent)
        return

    if action == "retry":
        last = await fetch_last_prompt(data["pane_id"])
        prompt = (last or {}).get("prompt")
        if not prompt:
            await query.message.reply_text("No previous prompt found to retry.")
            return
        try:
            await send_text_to_relay(data["pane_id"], prompt)
        except Exception as e:
            await query.message.reply_text(f"Retry failed: {scrub(e)}")
            return
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text(
            f"↻ Resubmitted the last prompt to {selected_agent['project']}.")
        return

    if action == "trust":
        try:
            await send_to_relay(
                data["pane_id"],
                "trust, always allow",
                prompt_id=blocked_prompt_ids.get(data["pane_id"]),
            )
            await query.message.reply_text(f"Trusted {selected_agent['project']} (always allow)")
        except Exception as e:
            await query.message.reply_text(f"Failed: {scrub(e)}")
        return

    if action != "approval":
        await query.message.reply_text("That action is no longer valid. Refresh the list and try again.")
        return

    pane_id = data["pane_id"]
    if not data.get("g") or data["g"] != approval_tokens.get(pane_id):
        await query.message.reply_text(
            "That approval belongs to an older prompt. Use the controls on the latest blocked notification."
        )
        return

    prompt_id = blocked_prompt_ids.get(pane_id)
    if not prompt_id:
        await query.message.reply_text(
            "That approval belongs to an older prompt. Use the controls on the latest blocked notification."
        )
        return

    option_index = data.get("i")
    if option_index is not None:
        try:
            option_index = int(option_index)
        except (TypeError, ValueError):
            await query.message.reply_text("That approval action is no longer supported. Use the latest notification.")
            return
        label = None
        if query.message and query.message.reply_markup:
            for row in query.message.reply_markup.inline_keyboard:
                for btn in row:
                    try:
                        button_data = parse_callback_data(btn.callback_data)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
                    if button_data.get("i") == str(option_index):
                        label = btn.text
                        break
                if label is not None:
                    break
        if not label:
            await query.message.reply_text("That approval action is no longer supported. Use the latest notification.")
            return
        try:
            await send_to_relay(pane_id, label, prompt_id=prompt_id)
        except Exception as e:
            await query.message.reply_text(f"Failed: {scrub(e)}")
            return
        approval_tokens.pop(pane_id, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Sent: {label}")
        return

    # Confirm a blocked agent's prompt by pressing the option number.
    # Sending the option *text* via `respond` does NOT work: the relay pastes it via
    # send-text, and Claude's TUI treats a pasted trailing newline as paste content,
    # not as Enter, so the prompt never gets confirmed. A real key press does.
    key = data.get("k")
    if key is None:
        await query.message.reply_text("That approval action is no longer supported. Use the latest notification.")
        return
    # Recover the pressed button's label for the confirmation (kept out of
    # callback_data to respect Telegram's 64-byte limit).
    label = f"option {key}"
    if query.message and query.message.reply_markup:
        for row in query.message.reply_markup.inline_keyboard:
            for btn in row:
                try:
                    if json.loads(btn.callback_data).get("k") == key:
                        label = btn.text
                except (ValueError, TypeError):
                    pass
    try:
        await send_keys_to_relay(pane_id, [key], prompt_id=prompt_id)
    except Exception as e:
        await query.message.reply_text(f"Failed: {scrub(e)}")
        return
    approval_tokens.pop(pane_id, None)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(f"Sent: {label}")


# --- Free text reply ---

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send a Telegram reply to its associated agent pane."""
    if not authorized(update) or not update.message.reply_to_message:
        return
    pane_id = pending_pane(update.effective_chat.id, update.message.reply_to_message.message_id)
    if not pane_id:
        return
    if not relay_connected:
        await update.message.reply_text("Relay disconnected. Use /start after it reconnects.")
        return
    if find_agent(pane_id) is None:
        await update.message.reply_text(
            "That agent is no longer available. Refresh the list and choose another agent.",
            reply_markup=refresh_keyboard("select_reply"),
        )
        return

    try:
        prompt_id = blocked_prompt_ids.get(pane_id)
        if prompt_id:
            await send_to_relay(pane_id, update.message.text, prompt_id=prompt_id)
        else:
            await send_text_to_relay(pane_id, update.message.text)
        await update.message.reply_text("Sent")
    except Exception as e:
        await update.message.reply_text(f"Failed: {scrub(e)}")


# --- Blocked notification ---

TOOL_BUTTONS = [
    ("Yes (once)", "yes, single permission"),
    ("Trust (always)", "trust, always allow"),
    ("No", "no (tab to edit)"),
]

SUBAGENT_BUTTONS = [
    ("Approve all", "approve all pending"),
    ("Configure", "configure individually"),
    ("Cancel", "exit (cancel subagents)"),
]


def make_keyboard(
    pane_id: str,
    options: list[str] | None,
    generation: str | None = None,
    interaction: str | None = None,
) -> InlineKeyboardMarkup:
    if not options:
        return interaction_keyboard(pane_id)
    # omp_question, opencode_question and opencode_permission all answer to their
    # option TEXT via `respond` (the relay maps it to keystrokes); the numeric-key
    # path below is for Claude-style prompts where only a real key press works.
    text_responses = interaction in (
        "omp_question", "opencode_question", "opencode_review", "opencode_permission",
    )
    if text_responses:
        buttons = [(opt, opt) for opt in options]
    elif "trust" in " ".join(options).lower():
        buttons = TOOL_BUTTONS
    elif "approve all" in " ".join(options).lower():
        buttons = SUBAGENT_BUTTONS
    else:
        buttons = [(opt.split(",")[0], opt) for opt in options]

    generation = generation or secrets.token_hex(4)
    if text_responses:
        keyboard = [
            [InlineKeyboardButton(label, callback_data=pane_callback_data(
                "approval", pane_id, g=generation, i=str(i),
            ))]
            for i, (label, _resp) in enumerate(buttons)
        ]
        keyboard.append([InlineKeyboardButton(
            "≡ Details", callback_data=pane_callback_data("read", pane_id)),
            InlineKeyboardButton("✎ Reply", callback_data=pane_callback_data("reply", pane_id)),
            *history_button(pane_id),
        ])
        return InlineKeyboardMarkup(keyboard)

    # Standard approval prompts require a real numeric key press.
    keyboard = [
        [InlineKeyboardButton(label, callback_data=pane_callback_data(
            "approval", pane_id, g=generation, k=str(i + 1)))]
        for i, (label, _resp) in enumerate(buttons)
    ]
    keyboard.append([InlineKeyboardButton(
        "≡ Details", callback_data=pane_callback_data("read", pane_id)),
        InlineKeyboardButton("✎ Reply", callback_data=pane_callback_data("reply", pane_id)),
        *history_button(pane_id),
    ])
    return InlineKeyboardMarkup(keyboard)


def interaction_keyboard(pane_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("≡ Details", callback_data=pane_callback_data("read", pane_id)),
        InlineKeyboardButton("✎ Reply", callback_data=pane_callback_data("reply", pane_id)),
        *history_button(pane_id),
    ]])


def approval_subject(summary: str, detail: list[str] | None) -> str:
    return "\n".join([summary, *(detail or [])]).strip()


# Known OpenCode permission kinds: (summary pattern, human title). The target
# captured from the summary line rides under a Target heading; anything the
# dialog lists as `- ...` lines becomes Patterns.
PERMISSION_KINDS = [
    (re.compile(r"^access external directory\s+(.+)$", re.I), "External directory access"),
    (re.compile(r"^bash command\b\s*(.*)$", re.I), "Bash command"),
    (re.compile(r"^(edit|write) file\s+(.+)$", re.I), "File edit"),
    (re.compile(r"^read file\s+(.+)$", re.I), "File read"),
]


def parse_permission(summary: str, detail: list[str] | None):
    """(title, target, patterns) with the noise ('Patterns' labels, dashes) gone."""
    title, target = summary, None
    for kind_re, label in PERMISSION_KINDS:
        match = kind_re.match(summary)
        if match:
            title = label
            target = next((g for g in match.groups() if g), None)
            break
    patterns, rest = [], []
    for line in detail or []:
        if line.strip().lower() == "patterns":
            continue
        if line.startswith("- "):
            patterns.append(line[2:])
        else:
            rest.append(line)
    if target is None and rest:
        target = rest.pop(0)
    patterns.extend(rest)
    if target:
        # A command restated with a `$` prefix duplicates the target; one copy.
        norm = lambda line: line.strip().lstrip("$").strip()
        patterns = [p for p in patterns if norm(p) != norm(target)]
    return title, target, patterns[:6]


def shorten_home(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def fmt_duration(seconds) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s" if seconds else f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60}m"


def waited_since(created: float) -> str:
    import time
    return fmt_duration(time.time() - created)


def context_line(cwd: str, branch: str) -> str:
    """The card's context strip: `~/project · branch`."""
    line = f"<code>{html.escape(shorten_home(cwd))}</code>" if cwd else ""
    if branch:
        line = f"{line} · <i>{html.escape(branch)}</i>" if line else f"<i>{html.escape(branch)}</i>"
    return line


def approval_text(project: str, host: str, cwd: str, branch: str, summary: str,
                  detail: list[str] | None, status: str) -> str:
    """Hierarchical permission card: context header, named permission,
    target/patterns, status. Everything else lives behind Details."""
    mark = approval_rules.severity(approval_subject(summary, detail))
    title, target, patterns = parse_permission(summary, detail)
    header = f"◆ <b>Approval</b> · <b>{html.escape(project)}</b>"
    if host and host != "local":
        header += f" · {html.escape(host)}"
    lines = [header]
    context = context_line(cwd, branch)
    if context:
        lines.append(context)
    lines += ["", f"{mark} {html.escape(title)}"]
    if target:
        lines += ["", "Target", f"<code>{html.escape(shorten_home(target))}</code>"]
    if patterns:
        lines += ["", "Patterns"]
        lines += [f"• <code>{html.escape(shorten_home(p))}</code>" for p in patterns]
    lines += ["", status]
    return "\n".join(lines)


def approval_keyboard(pane_id: str, generation: str) -> InlineKeyboardMarkup:
    # Allow once is full-width: the default action must not be mistakable for
    # anything with lasting consequences.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✓ Allow once", callback_data=pane_callback_data(
            "approve", pane_id, g=generation))],
        [InlineKeyboardButton("∞ Allow always", callback_data=pane_callback_data(
            "always", pane_id, g=generation)),
         InlineKeyboardButton("⌂ Trust project", callback_data=pane_callback_data(
             "trust_project", pane_id, g=generation))],
        [InlineKeyboardButton("✕ Reject", callback_data=pane_callback_data(
            "reject", pane_id, g=generation)),
         InlineKeyboardButton("≡ Details", callback_data=pane_callback_data("read", pane_id)),
         InlineKeyboardButton("✎ Reply", callback_data=pane_callback_data("reply", pane_id))],
    ])


def rules_text() -> str:
    rules = approval_rules.load()
    if not rules:
        return ("No approval rules yet.\n\n"
                "Press ∞ Allow always on an approval card, or allow the same thing "
                f"{RULE_OFFER_AFTER}× and an ∞ Allow always for this button appears.")
    lines = [f"Approval rules ({len(rules)})", ""]
    for i, rule in enumerate(rules, 1):
        subject = rule.get("subject")
        if subject is None:
            scope = "project · all safe actions"
        else:
            scope = "always · " + " ".join(subject.split())[:70]
        lines.append(f"{i}. {html.escape(rule.get('project', '?'))} · {html.escape(scope)}")
    return "\n".join(lines)


def rules_keyboard() -> InlineKeyboardMarkup:
    rules = approval_rules.load()
    row, rows = [], []
    for i in range(len(rules)):
        row.append(InlineKeyboardButton(
            f"✕ {i + 1}",
            callback_data=json.dumps({"action": "rule_del", "i": i}, separators=(",", ":")),
        ))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def edit_approval_message(app, pane_id: str, prefix: str,
                                reply_markup=None) -> bool:
    """Move a live approval card to its final state (edit, never re-notify)."""
    info = approval_messages.pop(pane_id, None)
    if not info:
        return False
    status = f"{prefix} · waited {waited_since(info['created'])}"
    text = approval_text(info["project"], info["host"], info["cwd"],
                         info.get("branch", ""), info["summary"],
                         info["detail"], status)
    if reply_markup is not None and pane_id in rule_offers:
        rule_offers[pane_id]["text"] = text
    try:
        await app.bot.edit_message_text(
            chat_id=info["chat_id"],
            message_id=info["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        log.warning("Failed to close approval message: %s", scrub(e))
        return False


# ponytail: keyword heuristic over the pane tail, not a real exit-status channel —
# opencode does not expose one; upgrade when the agent reports failures structurally.
COMPLETION_FAILURE_RE = re.compile(
    r"\berrors?\b|\bfail(?:ed|ure|ing)?\b|\btraceback\b|\bexception\b|✗|✖|exit code [1-9]",
    re.IGNORECASE,
)
NO_FAILURE_RE = re.compile(r"\b0\s+(?:errors?|failed|failures)\b|\bno\s+(?:errors?|failures)\b", re.IGNORECASE)


def completion_summary(content: str):
    """(failed, last meaningful lines, hidden count) from a finished pane."""
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    if not lines:
        return False, [], 0
    window = NO_FAILURE_RE.sub("", "\n".join(lines[-10:]))
    failed = bool(COMPLETION_FAILURE_RE.search(window))
    take = 8 if failed else 4
    hidden = max(0, len(lines) - take)
    return failed, lines[-take:], hidden


def finished_keyboard(pane_id: str, failed: bool) -> InlineKeyboardMarkup:
    rows = []
    if failed:
        rows.append([InlineKeyboardButton("↻ Retry", callback_data=pane_callback_data(
            "retry", pane_id))])
    last = [InlineKeyboardButton("≡ Details", callback_data=pane_callback_data("read", pane_id)),
            InlineKeyboardButton("✎ Reply", callback_data=pane_callback_data("reply", pane_id))]
    base = _web_base()
    if base:
        last.append(InlineKeyboardButton("↗\ufe0e Web", url=f"{base}/?pane={pane_id}"))
    rows.append(last)
    return InlineKeyboardMarkup(rows)


def working_keyboard(pane_id: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("■ Interrupt", callback_data=pane_callback_data("interrupt", pane_id)),
        InlineKeyboardButton("≡ Details", callback_data=pane_callback_data("read", pane_id)),
    ]]
    base = _web_base()
    if base:
        rows[0].append(InlineKeyboardButton("↗\ufe0e Web", url=f"{base}/?pane={pane_id}"))
    return InlineKeyboardMarkup(rows)


def working_text(agent_data: dict, elapsed, title: str, state: str = "working",
                 details: list[str] | None = None, branch: str = "") -> str:
    """Live session card. `state`: working | paused | ended. `title` is the live
    activity line; `details` are the opencode session lines (context, cost, todos)."""
    project = agent_data.get("project") or agent_data.get("agent") or "agent"
    host = agent_data.get("host", "local")
    cwd = agent_data.get("cwd", "")
    mark, verb = {
        "working": ("◐", "working"),
        "paused": ("‖", "waiting for approval"),
        "ended": ("■", "session ended"),
        "stuck": ("△", "possibly stuck"),
    }[state]
    header = f"{mark} <b>{project}</b> · {verb}"
    if state != "ended":
        header += f" · {fmt_duration(elapsed)}"
    if host and host != "local":
        header += f" · {html.escape(host)}"
    out = [header]
    context = context_line(cwd, branch)
    if context:
        out.append(context)
    body = details if details else ([html.escape(title)] if title else [])
    if body:
        out += ["", *body]
    return "\n".join(out)


def finished_text(agent_data: dict, duration, lines: list[str], hidden: int,
                  failed: bool, branch: str = "") -> str:
    """Completion card: verdict + duration, project context, last output."""
    project = agent_data.get("project") or agent_data.get("agent") or "agent"
    host = agent_data.get("host", "local")
    cwd = agent_data.get("cwd", "")
    mark, verb = ("✕", "failed") if failed else ("✓", "finished")
    header = f"{mark} <b>{html.escape(project)}</b> {verb} · {fmt_duration(duration)}"
    if host and host != "local":
        header += f" · {html.escape(host)}"
    out = [header]
    context = context_line(cwd, branch)
    if context:
        out.append(context)
    out.append("")
    if lines:
        out += [html.escape(line) for line in lines]
        if hidden:
            out.append(f"<i>… {hidden} more lines</i>")
    else:
        out.append("<i>Completed with no output.</i>")
    return "\n".join(out)


def multi_question_text(project: str, question: str, options: list[str],
                        details: list[str] | None, selected: list[str],
                        host: str = "", submitted: bool = False) -> str:
    """The multi-select card, rendered ONLY from structured data -- the raw
    terminal tail never appears, so no numbered rows, paths or internal
    echoes leak in, and nothing is duplicated with the buttons."""
    header = f"◆ <b>{html.escape(project)}</b> · choose one or more"
    if host and host != "local":
        header += f" · {html.escape(host)}"
    out = [header]
    if question:
        # One line of context if pi's header row survived next to the question.
        out += ["", html.escape(question)]
    details = details or []
    if options:
        out += [""]
        for index, option in enumerate(options):
            mark = "▣" if option in selected else "□"
            out.append(f"{mark} <b>{html.escape(option)}</b>")
            detail = details[index] if index < len(details) else ""
            if detail:
                out.append(f"　<i>{html.escape(detail)}</i>")
    count = len(selected)
    if submitted:
        out += ["", f"<i>‖ Submitted — {count} selected, waiting for the agent.</i>"]
    else:
        out += ["", f"<i>{count} selected · tap options to toggle</i>"]
    return "\n".join(out)


def multi_question_keyboard(pane_id: str, options: list[str],
                            selected: list[str]) -> InlineKeyboardMarkup:
    """One toggle per option, its checkbox IN the button label -- the button
    row itself shows state, not just the message body above it."""
    rows = [
        [InlineKeyboardButton(
            f"{'▣' if option in selected else '□'} {option}"[:64],
            callback_data=pane_callback_data("q_toggle", pane_id, i=str(i)),
        )]
        for i, option in enumerate(options)
    ]
    rows.append([
        InlineKeyboardButton(f"✓ Submit ({len(selected)} selected)",
                             callback_data=pane_callback_data("q_submit", pane_id)),
        InlineKeyboardButton("≡ Details", callback_data=pane_callback_data("read", pane_id)),
        InlineKeyboardButton("✎ Reply", callback_data=pane_callback_data("reply", pane_id)),
    ])
    history = history_button(pane_id)
    if history:
        rows.append(history)  # its own row: the Submit row is already three wide
    return InlineKeyboardMarkup(rows)


async def notify_blocked(
    app: Application,
    pane_id: str,
    agent: str,
    project: str,
    prompt: str,
    options: list[str] | None,
    prompt_id: str | None = None,
    interaction: str | None = None,
    multi: bool = False,
    host: str = "",
    summary: str | None = None,
    detail: list[str] | None = None,
    cwd: str = "",
    branch: str = "",
    multi_options: list[str] | None = None,
    selected_options: list[str] | None = None,
    multi_details: list[str] | None = None,
    question_text: str = "",
    update: bool = False,
):
    if not CHAT_ID:
        return
    if interaction == "opencode_permission" and summary:
        import time
        msg = await app.bot.send_message(
            chat_id=int(CHAT_ID),
            text=approval_text(project, host, cwd, branch, summary, detail, "‖ Waiting"),
            parse_mode="HTML",
            reply_markup=approval_keyboard(pane_id, generation := secrets.token_hex(4)),
        )
        approval_tokens[pane_id] = generation
        if prompt_id:
            blocked_prompt_ids[pane_id] = prompt_id
        approval_messages[pane_id] = {
            "chat_id": int(CHAT_ID),
            "message_id": msg.message_id,
            "project": project,
            "host": host,
            "cwd": cwd,
            "branch": branch,
            "summary": summary,
            "detail": detail or [],
            "created": time.time(),
        }
        register_pending(int(CHAT_ID), msg.message_id, pane_id)
        return
    if multi and multi_options is not None:
        # Multi-select question card, rendered only from structured data.
        # The first card is sent; every selection change (a button tap edits
        # optimistically, the relay's next poll confirms authoritatively)
        # re-renders the SAME message, so a multi-pick never becomes a stack.
        if prompt_id:
            blocked_prompt_ids[pane_id] = prompt_id
        selected = list(selected_options or [])
        card = multi_cards.get(pane_id)
        text = multi_question_text(project, question_text, multi_options,
                                   multi_details, selected, host=host,
                                   submitted=bool(card and card.get("submitted")))
        keyboard = multi_question_keyboard(pane_id, multi_options, selected)
        if card and update:
            try:
                await app.bot.edit_message_text(
                    chat_id=card["chat_id"], message_id=card["message_id"],
                    text=text, parse_mode="HTML", reply_markup=keyboard,
                )
                if not card.get("submitted"):
                    card["selected"] = selected
                return
            except Exception:
                pass  # too old to edit: fall through to a fresh card
        msg = await app.bot.send_message(
            chat_id=int(CHAT_ID), text=text, parse_mode="HTML", reply_markup=keyboard,
        )
        multi_cards[pane_id] = {
            "chat_id": int(CHAT_ID), "message_id": msg.message_id,
            "options": list(multi_options), "details": list(multi_details or []),
            "question": question_text, "selected": selected,
            "project": project, "host": host, "submitted": False,
        }
        register_pending(int(CHAT_ID), msg.message_id, pane_id)
        return
    # Only the unusual case explains itself: multi-select needs the web terminal.
    # HTML, not Markdown: a ``` or _ in the prompt or project broke the legacy parse and the
    # card never arrived. The whole tail is kept -- the question sits at its end -- with the
    # context above it folded.
    body = (f"<b>{html.escape(agent)}</b> blocked in <code>{html.escape(project)}</code>\n\n"
            f"{folded_output(prompt, tail_lines=6)}")
    if multi:
        body += "\n\nUse the web terminal or manual terminal controls to select multiple options."
    generation = secrets.token_hex(4)
    keyboard = make_keyboard(
        pane_id,
        [] if multi else options,
        generation=generation,
        interaction=interaction,
    )
    msg = await app.bot.send_message(
        chat_id=int(CHAT_ID), text=body, parse_mode="HTML", reply_markup=keyboard
    )
    approval_tokens[pane_id] = generation
    if prompt_id:
        blocked_prompt_ids[pane_id] = prompt_id
    register_pending(int(CHAT_ID), msg.message_id, pane_id)


async def notify_blocked_safely(app: Application, msg: dict):
    # Updates re-render only the multi-select card (in place); everything else
    # still drops here so a sitting prompt cannot re-ping.
    if msg.get("update") and not (
        msg.get("multi") and msg.get("interaction") == "omp_question"
        and msg.get("pane_id") in multi_cards
    ):
        return
    # Rules first: a matched safe request never interrupts Telegram at all.
    if (
        msg.get("interaction") == "opencode_permission"
        and msg.get("summary")
        and msg.get("prompt_id")
    ):
        subject = approval_subject(msg["summary"], msg.get("detail") or [])
        if approval_rules.allows(msg.get("project", ""), subject):
            try:
                await send_to_relay(msg["pane_id"], "Allow once", prompt_id=msg["prompt_id"])
                import time
                stats = daily_stats.setdefault(msg["pane_id"], {
                    "agent": msg.get("agent", ""), "project": msg.get("project", ""),
                    "blocked_count": 0, "working_mins": 0, "last_change": time.time(),
                })
                stats["auto_approved"] = stats.get("auto_approved", 0) + 1
                log.info("Auto-approved %s: rule matched", scrub(msg.get("project", "")))
                return
            except Exception as e:
                log.warning("Auto-approve failed, notifying instead: %s", scrub(e))
    try:
        await notify_blocked(
            app,
            pane_id=msg["pane_id"],
            agent=msg.get("agent", "unknown"),
            project=msg.get("project", ""),
            prompt=msg.get("prompt", ""),
            options=msg.get("options"),
            prompt_id=msg.get("prompt_id"),
            interaction=msg.get("interaction"),
            multi=bool(msg.get("multi")),
            host=msg.get("host", ""),
            summary=msg.get("summary"),
            detail=msg.get("detail"),
            cwd=msg.get("cwd", ""),
            branch=msg.get("branch", ""),
            multi_options=msg.get("multi_options"),
            selected_options=msg.get("selected_options"),
            multi_details=msg.get("multi_details"),
            question_text=msg.get("question_text", ""),
            update=bool(msg.get("update")),
        )
    except Exception as e:
        log.warning("Failed to send blocked notification: %s", scrub(e))


# --- Relay listener ---

async def track_agent_updates(app: Application, updated_agents: list[dict]):
    """Track status transitions for full snapshots and pane-level updates."""
    if not CHAT_ID:
        return

    import time
    now = time.time()
    for agent_data in updated_agents:
        pane_id = agent_data["pane_id"]
        new_status = agent_data.get("status", "unknown")
        old_status = prev_statuses.get(pane_id)

        if pane_id not in daily_stats:
            daily_stats[pane_id] = {
                "agent": agent_data.get("agent", ""),
                "project": agent_data.get("project", ""),
                "blocked_count": 0,
                "working_mins": 0,
                "last_change": now,
            }
        stats = daily_stats[pane_id]
        run_started = stats["last_change"]
        if old_status == "working" and old_status != new_status:
            stats["working_mins"] += int((now - stats["last_change"]) / 60)
        if new_status == "blocked" and old_status != "blocked":
            stats["blocked_count"] += 1
        if old_status != new_status:
            stats["last_change"] = now
        if agent_data.get("status") and new_status != "blocked":
            approval_tokens.pop(pane_id, None)
            blocked_prompt_ids.pop(pane_id, None)
            if pane_id in approval_messages:
                # Answered in the terminal or pane gone: settle the card, no ping.
                await edit_approval_message(app, pane_id, "■ Closed")
            card = multi_cards.pop(pane_id, None)
            if card:
                # The multi question was answered somewhere: mark the card done.
                try:
                    await app.bot.edit_message_text(
                        chat_id=card["chat_id"], message_id=card["message_id"],
                        text=(f"✓ <b>{html.escape(agent_data.get('project') or 'agent')}</b> "
                              "· question answered"),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        # One nudge if an approval card has been sitting for NUDGE_AFTER.
        if new_status == "blocked":
            approval = approval_messages.get(pane_id)
            if (approval and not approval.get("nudged")
                    and now - approval.get("created", now) > NUDGE_AFTER):
                approval["nudged"] = True
                try:
                    await app.bot.send_message(
                        chat_id=int(CHAT_ID),
                        text=(f"◆ <b>{html.escape(agent_data.get('project') or 'agent')}</b> "
                              f"has been waiting for approval for "
                              f"{fmt_duration(now - approval['created'])} — the approval card is above."),
                        parse_mode="HTML",
                        disable_notification=is_quiet(agent_data.get("project", "")),
                    )
                except Exception as e:
                    log.warning("Failed to send approval nudge: %s", scrub(e))

        # Rolling session card: one message per pane that flips between ⚙️ and
        # ✅/❌. A finish that restarts within FLAP_GRACE reuses the same card
        # (opencode flaps working/idle between turns; a flap is not a session).
        card = working_cards.get(pane_id)
        if new_status == "working":
            title = agent_data.get("title", "")
            restart = card is not None and card.get("state") != "working"
            if card is None or (restart and now - card.get("finished_at", 0) > FLAP_GRACE):
                try:
                    info = await fetch_session_info(pane_id)
                    activity = (info or {}).get("activity") or title
                    msg = await app.bot.send_message(
                        chat_id=int(CHAT_ID),
                        text=working_text(agent_data, now - run_started, activity,
                                          details=session_details_lines(info),
                                          branch=(info or {}).get("branch", "")),
                        parse_mode="HTML",
                        reply_markup=working_keyboard(pane_id),
                        disable_notification=is_quiet(agent_data.get("project", "")),
                    )
                    working_cards[pane_id] = {
                        "chat_id": int(CHAT_ID), "message_id": msg.message_id,
                        "started": run_started, "title": title,
                        "activity": activity, "last_edit": now,
                        "paused": False, "agent": agent_data, "state": "working",
                    }
                    register_pending(int(CHAT_ID), msg.message_id, pane_id)
                    log.info("Working card sent: %s (%s)",
                             scrub(agent_data.get("project", "")), scrub(pane_id))
                except Exception as e:
                    log.warning("Failed to send working card: %s", scrub(e))
            elif restart:
                # Flap: the result card flips straight back to working.
                card.update(state="working", started=run_started, last_edit=now,
                            paused=False, title=title, agent=agent_data,
                            finished_at=None)
                card.pop("stuck_mark", None)
                try:
                    info = await fetch_session_info(pane_id)
                    card["activity"] = (info or {}).get("activity") or title
                    await app.bot.edit_message_text(
                        chat_id=card["chat_id"], message_id=card["message_id"],
                        text=working_text(agent_data, 0, card["activity"],
                                          details=session_details_lines(info),
                                          branch=(info or {}).get("branch", "")),
                        parse_mode="HTML",
                        reply_markup=working_keyboard(pane_id),
                    )
                except Exception as e:
                    log.warning("Failed to resume working card: %s", scrub(e))
            elif (
                title != card["title"] or card.get("paused")
                or now - card["last_edit"] >= 60
            ):
                card.update(title=title, last_edit=now, paused=False, agent=agent_data)
                try:
                    info = await fetch_session_info(pane_id)
                    activity = (info or {}).get("activity") or title
                    card["activity"] = activity
                    # Stuck: opencode's session row has not moved in STUCK_AFTER.
                    updated_ms = (info or {}).get("updated_ms") or 0
                    stale = bool(updated_ms) and now * 1000 - updated_ms > STUCK_AFTER * 1000
                    await app.bot.edit_message_text(
                        chat_id=card["chat_id"], message_id=card["message_id"],
                        text=working_text(agent_data, now - card["started"], activity,
                                          details=session_details_lines(info),
                                          branch=(info or {}).get("branch", ""),
                                          state="stuck" if stale else "working"),
                        parse_mode="HTML",
                        reply_markup=working_keyboard(pane_id),
                    )
                    if stale and card.get("stuck_mark") != updated_ms:
                        card["stuck_mark"] = updated_ms
                        await app.bot.send_message(
                            chat_id=int(CHAT_ID),
                            text=(f"△ <b>{html.escape(agent_data.get('project') or 'agent')}</b> "
                                  "has not touched its session in "
                                  f"{fmt_duration((now * 1000 - updated_ms) / 1000)} — possibly stuck. "
                                  "■ Interrupt it or ✎ Reply from its card."),
                            parse_mode="HTML",
                            disable_notification=is_quiet(agent_data.get("project", "")),
                        )
                    if not stale:
                        card.pop("stuck_mark", None)
                except Exception as e:
                    log.warning("Failed to update working card: %s", scrub(e))
        elif new_status == "blocked" and card is not None and not card.get("paused"):
            card.update(paused=True, last_edit=now, agent=agent_data)
            try:
                await app.bot.edit_message_text(
                    chat_id=card["chat_id"], message_id=card["message_id"],
                    text=working_text(agent_data, now - card["started"],
                                      card.get("activity", ""), state="paused"),
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("Failed to pause working card: %s", scrub(e))

        if old_status and old_status != new_status and new_status in ("idle", "done") and old_status in ("working", "blocked"):
            try:
                content = await read_pane(pane_id)
                failed, lines, hidden = completion_summary(content)
                info = await fetch_session_info(pane_id)
                text = finished_text(agent_data, now - run_started, lines, hidden, failed,
                                     branch=(info or {}).get("branch", ""))
                auto = int(stats.get("auto_approved", 0))
                if auto:
                    text += f"\n\n∞ {auto} auto-approved by rules"
                keyboard = finished_keyboard(pane_id, failed)
                card = working_cards.get(pane_id)
                edited = False
                if card:
                    # The session's one message becomes its own result.
                    try:
                        await app.bot.edit_message_text(
                            chat_id=card["chat_id"], message_id=card["message_id"],
                            text=text, parse_mode="HTML",
                            reply_markup=keyboard,
                        )
                        register_pending(card["chat_id"], card["message_id"], pane_id)
                        edited = True
                    except Exception:
                        pass  # too old to edit: fall through to a fresh message
                if not edited:
                    msg = await app.bot.send_message(
                        chat_id=int(CHAT_ID),
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                        disable_notification=is_quiet(agent_data.get("project", "")),
                    )
                    register_pending(int(CHAT_ID), msg.message_id, pane_id)
                if card is not None:
                    card.update(state="finished", finished_at=now, last_edit=now,
                                agent=agent_data, activity="")
                    card.pop("stuck_mark", None)
                else:
                    working_cards[pane_id] = {
                        "chat_id": int(CHAT_ID), "message_id": msg.message_id,
                        "state": "finished", "finished_at": now, "last_edit": now,
                        "started": run_started, "paused": False,
                        "agent": agent_data, "title": "", "activity": "",
                    }
            except Exception as e:
                log.warning("Failed to send completion notification: %s", scrub(e))
        prev_statuses[pane_id] = new_status


async def relay_listener(app: Application):
    """Persistent WebSocket connection to relay."""
    import websockets
    global agents, relay_connected, prev_statuses

    while True:
        try:
            async with websockets.connect(RELAY_WS) as ws:
                relay_connected = True
                log.info(f"Connected to relay at {RELAY_WS_SAFE}")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if msg.get("type") == "agents":
                        new_agents = msg.get("agents", [])
                        blocked_panes = {
                            agent.get("pane_id") for agent in new_agents
                            if agent.get("status") == "blocked"
                        }
                        for pane_id in list(approval_tokens):
                            if pane_id not in blocked_panes:
                                approval_tokens.pop(pane_id, None)
                                blocked_prompt_ids.pop(pane_id, None)
                                if pane_id in approval_messages:
                                    await edit_approval_message(
                                        app, pane_id, "■ Closed")
                        # A pane that vanished mid-run never sends a finished
                        # status; settle its session card instead.
                        live_panes = {agent.get("pane_id") for agent in new_agents}
                        for pane_id in list(working_cards):
                            if pane_id not in live_panes:
                                card = working_cards.pop(pane_id)
                                try:
                                    await app.bot.edit_message_text(
                                        chat_id=card["chat_id"],
                                        message_id=card["message_id"],
                                        text=working_text(card["agent"], 0,
                                                          card["title"], state="ended"),
                                        parse_mode="HTML",
                                    )
                                except Exception as e:
                                    log.warning("Failed to settle gone pane card: %s", scrub(e))
                        await track_agent_updates(app, new_agents)
                        agents = apply_agent_message(agents, msg)

                    elif msg.get("type") == "agent_update":
                        updated_agent = msg.get("agent") or {}
                        if updated_agent.get("pane_id"):
                            await track_agent_updates(app, [updated_agent])
                            agents = apply_agent_message(agents, msg)

                    elif msg.get("type") == "blocked":
                        await notify_blocked_safely(app, msg)
                clear_relay_connection_state()
        except Exception as e:
            clear_relay_connection_state()
            log.warning(f"Relay connection lost: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


# --- Main ---

def main():
    app = Application.builder().token(TOKEN).build()
    if CHAT_ID:
        auth_filter = filters.Chat(chat_id=int(CHAT_ID))
    else:
        auth_filter = filters.ALL  # No restriction in discovery mode

    app.add_handler(CommandHandler("start", cmd_start, filters=auth_filter))
    app.add_handler(CommandHandler("agents", cmd_agents, filters=auth_filter))
    app.add_handler(CommandHandler("now", cmd_now, filters=auth_filter))
    app.add_handler(CommandHandler("rules", cmd_rules, filters=auth_filter))
    app.add_handler(CommandHandler("quiet", cmd_quiet, filters=auth_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=auth_filter))
    app.add_handler(CommandHandler("session", cmd_session, filters=auth_filter))
    app.add_handler(CommandHandler("read", cmd_read, filters=auth_filter))
    app.add_handler(CommandHandler("history", cmd_history, filters=auth_filter))
    app.add_handler(CommandHandler("reply", cmd_reply, filters=auth_filter))
    app.add_handler(CommandHandler("send", cmd_send, filters=auth_filter))
    app.add_handler(CommandHandler("trust", cmd_trust, filters=auth_filter))
    app.add_handler(CommandHandler("digest", cmd_digest, filters=auth_filter))
    app.add_handler(CommandHandler("interrupt", cmd_interrupt, filters=auth_filter))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & auth_filter, handle_text))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        async with app:
            await app.start()
            await app.updater.start_polling()
            asyncio.create_task(morning_digest_task(app))
            await relay_listener(app)

    loop.run_until_complete(run())


if __name__ == "__main__":
    main()
