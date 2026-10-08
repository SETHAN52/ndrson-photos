#!/usr/bin/env python3
"""Send the daily 12:34 selfie reminder for one group (run by .github/workflows/reminder.yml).

All private data comes from environment variables (repo secrets); nothing private lives here.
  AGENTMAIL_API_KEY           AgentMail key allowed to read + send from the reminder inbox
  NDRSON_REMINDER_RECIPIENTS  JSON: {"from_inbox": str, "alert_to": str,
                                     "groups": {"<group>": {"tz": str,
                                       "people": [{"name","to","subject","text"}]}}}
  GROUP     which group to send (e.g. central | eastern)
  DRY_RUN   "true" -> do every check but send nothing (no reminders, no alert email)
  FORCE     "true" -> ignore the 12:00-15:00 local-time window (still never double-sends)

Logs are public (public repo): only names and ok/skip/fail are printed, never addresses.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API = "https://api.agentmail.to/v0"
WINDOW_START = dt.time(12, 0)
WINDOW_END = dt.time(15, 0)
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def mask(value):
    # Extra safety on GitHub: redact the value if it ever reaches the log.
    if IN_ACTIONS and value:
        print(f"::add-mask::{value}", flush=True)


def api_key():
    k = os.environ.get("AGENTMAIL_API_KEY", "").strip()
    if "=" in k and k.split("=", 1)[0].isupper():  # tolerate a pasted "NAME=value"
        k = k.split("=", 1)[1].strip()
    if not k:
        sys.exit("missing AGENTMAIL_API_KEY")
    mask(k)
    return k


class ApiError(Exception):
    pass


def call(key, method, path, params=None, body=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "ndrson-reminders",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:  # never echo URLs or bodies (they contain addresses)
        raise ApiError(f"HTTP {e.code}") from None
    except Exception as e:
        raise ApiError(type(e).__name__) from None


def sent_today(key, inbox, since_utc):
    """Lower-cased recipient addresses of messages sent from `inbox` since `since_utc`."""
    seen, token = set(), None
    for _ in range(20):  # <= 2000 messages; one day is a handful
        params = {"labels": "sent", "limit": 100,
                  "after": since_utc.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if token:
            params["page_token"] = token
        d = call(key, "GET", f"/inboxes/{urllib.parse.quote(inbox, safe='')}/messages", params)
        for m in d.get("messages", []):
            ts = m.get("timestamp") or m.get("created_at") or ""
            try:
                when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                when = since_utc  # unknown time: count it (errs toward not double-sending)
            if when >= since_utc:
                for a in (m.get("to") or []):
                    seen.add(str(a).split("<")[-1].strip(" >").lower())
        token = d.get("next_page_token")
        if not token:
            break
    return seen


def send(key, inbox, to, subject, text):
    body = {"to": [to], "text": text}
    if subject:
        body["subject"] = subject
    return call(key, "POST", f"/inboxes/{urllib.parse.quote(inbox, safe='')}/messages/send", body=body)


def alert(key, cfg, group, lines, dry):
    to = cfg.get("alert_to")
    if not to:
        print("::warning::no alert_to configured; cannot email failure notice")
        return
    text = (f"The ndrson 12:34 reminder run for group '{group}' had problems:\n\n"
            + "\n".join(f"- {l}" for l in lines)
            + f"\n\nRun: {os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
              f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
    if dry:
        print("DRY RUN: would email a failure notice to the owner")
        return
    try:
        send(key, cfg["from_inbox"], to, f"ndrson reminder failed ({group})", text)
        print("Failure notice emailed to owner")
    except ApiError as e:
        print(f"::error::could not email failure notice ({e})")


def main():
    group = os.environ.get("GROUP", "").strip().lower()
    dry = truthy(os.environ.get("DRY_RUN"))
    force = truthy(os.environ.get("FORCE"))
    key = api_key()
    try:
        cfg = json.loads(os.environ.get("NDRSON_REMINDER_RECIPIENTS") or "")
    except ValueError:
        sys.exit("NDRSON_REMINDER_RECIPIENTS missing or not valid JSON")
    for v in (cfg.get("from_inbox"), cfg.get("alert_to")):
        mask(v)
    for g in cfg.get("groups", {}).values():
        for p in g.get("people", []):
            mask(p.get("to"))

    if group not in cfg.get("groups", {}):
        sys.exit(f"unknown group {group!r}")
    g = cfg["groups"][group]
    tz = ZoneInfo(g["tz"])
    now = dt.datetime.now(tz)
    print(f"Group {group}: local time {now:%Y-%m-%d %H:%M:%S %Z}; dry_run={dry} force={force}")

    if not (WINDOW_START <= now.time() <= WINDOW_END):
        if not force:
            print(f"Outside the {WINDOW_START:%H:%M}-{WINDOW_END:%H:%M} local window; nothing sent.")
            return 0
        print("Outside the local window, but force=true; continuing.")

    inbox = cfg["from_inbox"]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since_utc = midnight.astimezone(dt.timezone.utc)
    try:
        already = sent_today(key, inbox, since_utc)
    except ApiError as e:
        msg = f"could not check today's sent messages ({e}); sent nothing to avoid double-texting"
        print(f"::error::{msg}")
        alert(key, cfg, group, [msg], dry)
        return 1
    print(f"Sent-today check ok ({len(already)} address(es) already messaged since local midnight)")

    failures, sent, skipped = [], 0, 0
    for p in g["people"]:
        name, to = p["name"], p["to"].strip()
        if to.lower() in already:
            print(f"{name}: skip (already sent today)")
            skipped += 1
            continue
        if dry:
            print(f"{name}: DRY RUN, would send (subject {'set' if p.get('subject') else 'empty'})")
            continue
        ok, err = False, ""
        for attempt in (1, 2):
            try:
                send(key, inbox, to, p.get("subject", ""), p["text"])
                ok = True
                break
            except ApiError as e:
                err = str(e)
                print(f"{name}: attempt {attempt} failed ({err})")
                time.sleep(5)
                try:  # the send may have gone through anyway; never send twice
                    if to.lower() in sent_today(key, inbox, since_utc):
                        ok = True
                        break
                except ApiError:
                    break  # can't confirm -> don't risk a duplicate; report failure
        if ok:
            print(f"{name}: sent")
            sent += 1
            already.add(to.lower())
        else:
            print(f"::error::{name}: send failed ({err})")
            failures.append(f"{name}: send failed ({err})")
        time.sleep(1)

    print(f"Done: sent={sent} skipped={skipped} failed={len(failures)}")
    if failures:
        alert(key, cfg, group, failures, dry)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
