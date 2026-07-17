"""
PPA PortLink dispatch board monitor.

Polls the public Pacific Pilotage Authority marine-traffic feed, alerts a
Telegram chat when NEW jobs appear that involve watched dock codes, and lets
the owner manage the watch list by messaging the bot:

    /add RB2        watch a dock code (add ALL to watch everything)
    /remove RB2     stop watching a dock code
    /list           show the current watch list
    /help           show available commands

State (seen job ids, watch list, telegram offset) is kept in state.json,
which the GitHub Actions workflow commits back to the repo after each run.

Required environment variables:
    TG_TOKEN    Telegram bot token from BotFather
    TG_CHAT_ID  Your personal Telegram chat id (alerts go here; commands
                are only accepted from this chat)
"""

import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

FEED_URL = "https://ppaportal.portlink.co/api/pdams/GetCurrentVesselTraffic"
STATE_FILE = "state.json"
PACIFIC = ZoneInfo("America/Vancouver")
MAX_SEEN_IDS = 20000  # plenty; keeps the state file from growing forever

TG_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

HELP_TEXT = (
    "PPA dispatch monitor commands:\n"
    "/add CODE - watch a dock (e.g. /add RB2). Use /add ALL for every dock.\n"
    "/remove CODE - stop watching a dock\n"
    "/route FROM TO - watch a specific route (e.g. /route NSB TPL).\n"
    "    Use ANY as a wildcard: /route ANY TPL alerts on anything\n"
    "    going TO Triple Island, regardless of origin.\n"
    "/unroute FROM TO - stop watching a route\n"
    "/list - show watched docks and routes\n"
    "/help - this message\n\n"
    "You'll get an alert when a new job appears whose FROM or TO dock is "
    "on your dock list, or whose FROM-TO pair matches one of your routes."
)


def parse_route(parts):
    """Accept '/route NSB TPL', '/route NSB-TPL', or '/route NSB>TPL'.
    Returns 'NSB>TPL' or None if it can't be parsed."""
    args = parts[1:]
    if len(args) == 1:
        for sep in (">", "-", "/"):
            if sep in args[0]:
                args = args[0].split(sep, 1)
                break
    if len(args) != 2:
        return None
    frm, to = args[0].strip().upper(), args[1].strip().upper()
    if not frm or not to:
        return None
    return f"{frm}>{to}"


# ---------------------------------------------------------------- state ----

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("seen_ids", [])
    state.setdefault("watched", [])          # list of dock short codes
    state.setdefault("routes", [])           # list of "FROM>TO" route strings
    state.setdefault("tg_offset", 0)         # telegram getUpdates offset
    state.setdefault("initialized", False)   # first run = baseline, no alerts
    return state


def save_state(state):
    # Trim the seen-id list if it ever gets huge (keep the newest entries).
    if len(state["seen_ids"]) > MAX_SEEN_IDS:
        state["seen_ids"] = state["seen_ids"][-MAX_SEEN_IDS:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ------------------------------------------------------------- telegram ----

def tg_send(text, chat_id=None):
    """Send a plain-text Telegram message. Returns True on success."""
    if not TG_TOKEN:
        print("TG_TOKEN missing; would have sent:\n" + text)
        return False
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id or TG_CHAT_ID, "text": text},
            timeout=20,
        )
        if not r.ok:
            print("Telegram send failed:", r.status_code, r.text[:300])
        return r.ok
    except requests.RequestException as e:
        print("Telegram send error:", e)
        return False


def process_commands(state):
    """Read new messages sent to the bot and apply /add /remove /list."""
    if not TG_TOKEN:
        return
    try:
        r = requests.get(
            f"{TG_API}/getUpdates",
            params={"offset": state["tg_offset"] + 1, "timeout": 0},
            timeout=20,
        )
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        print("getUpdates error:", e)
        return

    for upd in updates:
        state["tg_offset"] = max(state["tg_offset"], upd.get("update_id", 0))
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        # Only obey the owner.
        if TG_CHAT_ID and chat_id != TG_CHAT_ID:
            tg_send("Sorry, this is a private bot.", chat_id=chat_id)
            continue

        parts = text.split()
        cmd = parts[0].lower()
        arg = parts[1].upper() if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            tg_send(HELP_TEXT)
        elif cmd == "/add" and arg:
            if arg not in state["watched"]:
                state["watched"].append(arg)
                state["watched"].sort()
            tg_send(f"Now watching: {', '.join(state['watched'])}")
        elif cmd == "/remove" and arg:
            if arg in state["watched"]:
                state["watched"].remove(arg)
                tg_send(
                    "Removed " + arg + ". Now watching: "
                    + (", ".join(state["watched"]) or "nothing")
                )
            else:
                tg_send(f"{arg} wasn't on the list. Watching: "
                        + (", ".join(state["watched"]) or "nothing"))
        elif cmd == "/route":
            route = parse_route(parts)
            if not route:
                tg_send("Couldn't read that route. Format: /route NSB TPL")
            else:
                if route not in state["routes"]:
                    state["routes"].append(route)
                    state["routes"].sort()
                tg_send("Now watching routes: "
                        + ", ".join(r.replace(">", " > ")
                                    for r in state["routes"]))
        elif cmd == "/unroute":
            route = parse_route(parts)
            if route and route in state["routes"]:
                state["routes"].remove(route)
                tg_send("Removed route " + route.replace(">", " > ")
                        + ". Routes now: "
                        + (", ".join(r.replace(">", " > ")
                                     for r in state["routes"]) or "none"))
            else:
                tg_send("That route wasn't on the list. Routes: "
                        + (", ".join(r.replace(">", " > ")
                                     for r in state["routes"]) or "none"))
        elif cmd == "/list":
            tg_send(
                "Docks: " + (", ".join(state["watched"]) or "none")
                + "\nRoutes: "
                + (", ".join(r.replace(">", " > ") for r in state["routes"])
                   or "none")
                + "\n\nAdd with /add CODE or /route FROM TO"
            )
        else:
            tg_send("Didn't understand that.\n\n" + HELP_TEXT)


# ----------------------------------------------------------------- feed ----

def fetch_jobs():
    r = requests.get(
        FEED_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "ppa-dispatch-monitor (personal notification tool)",
        },
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("result") != "Succeed":
        raise RuntimeError("Feed returned unexpected result: "
                           + str(payload.get("result")))
    return payload.get("data", [])


def fmt_time(iso_str):
    """UTC ISO string -> 'Jul 11, 08:00 PT' (or the raw string on failure)."""
    if not iso_str:
        return "n/a"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(PACIFIC).strftime("%b %d, %H:%M PT")
    except ValueError:
        return iso_str


def job_matches(job, watched, routes):
    frm = (job.get("fromLocationShortCode") or "").upper()
    to = (job.get("toLocationShortCode") or "").upper()
    if "ALL" in watched:
        return True
    if frm in watched or to in watched:
        return True
    for route in routes:
        r_frm, r_to = route.split(">", 1)
        if (r_frm in ("ANY", "*") or r_frm == frm) and \
           (r_to in ("ANY", "*") or r_to == to):
            return True
    return False


def matched_routes(job, routes):
    frm = (job.get("fromLocationShortCode") or "").upper()
    to = (job.get("toLocationShortCode") or "").upper()
    hits = []
    for route in routes:
        r_frm, r_to = route.split(">", 1)
        if (r_frm in ("ANY", "*") or r_frm == frm) and \
           (r_to in ("ANY", "*") or r_to == to):
            hits.append(route.replace(">", " > "))
    return hits


def alert_text(job, watched, routes=()):
    v = job.get("vessel") or {}
    frm_code = job.get("fromLocationShortCode") or "?"
    to_code = job.get("toLocationShortCode") or "?"
    frm_mark = " *" if frm_code.upper() in watched else ""
    to_mark = " *" if to_code.upper() in watched else ""
    lines = [
        "NEW JOB ON PPA BOARD",
        f"Vessel: {v.get('name', '?')} ({v.get('type', '?')}, "
        f"{v.get('flagRegistryName', '?')})",
        f"From: {job.get('fromLocationName') or frm_code}{frm_mark}",
        f"To: {job.get('toLocationName') or to_code}{to_mark}",
        f"Order time: {fmt_time(job.get('orderTime'))}",
        f"ETD: {fmt_time(job.get('etd'))}",
        f"Status: {job.get('status', '?')}",
        f"Agency: {job.get('agencyName', '?')}",
    ]
    remarks = (job.get("remarks") or "").strip()
    if remarks:
        lines.append(f"Remarks: {remarks}")
    hits = matched_routes(job, routes)
    if hits:
        lines.append("Matched route: " + ", ".join(hits))
    if frm_mark or to_mark:
        lines.append("(* = your watched dock)")
    return "\n".join(lines)


# ----------------------------------------------------------------- main ----

def main():
    state = load_state()

    # 1. Apply any dock-list commands you've texted the bot since last run.
    process_commands(state)

    # 2. Fetch the live board.
    try:
        jobs = fetch_jobs()
    except Exception as e:
        print("Feed fetch failed:", e)
        save_state(state)  # keep command changes even if the feed hiccups
        sys.exit(0)        # don't fail the workflow for a transient error

    seen = set(state["seen_ids"])
    watched = [w.upper() for w in state["watched"]]
    routes = [r.upper() for r in state["routes"]]
    new_alerts = 0

    for job in jobs:
        uid = job.get("id") or job.get("jobPilotId") or job.get("jobId")
        if uid is None:
            continue
        if uid in seen:
            continue
        seen.add(uid)
        state["seen_ids"].append(uid)
        if state["initialized"] and job_matches(job, watched, routes):
            if tg_send(alert_text(job, watched, routes)):
                new_alerts += 1

    if not state["initialized"]:
        state["initialized"] = True
        print(f"Baseline established: {len(jobs)} jobs recorded, no alerts sent.")
        if TG_TOKEN and TG_CHAT_ID:
            tg_send(
                "PPA monitor is up and running. Baseline set with "
                f"{len(jobs)} current jobs - you'll be alerted about new "
                "ones from now on.\n\n" + HELP_TEXT
            )

    print(f"Run complete at {datetime.now(timezone.utc).isoformat()} - "
          f"{len(jobs)} jobs on board, {new_alerts} alert(s) sent, "
          f"watching: {', '.join(watched) or 'nothing'}")
    save_state(state)


if __name__ == "__main__":
    main()
