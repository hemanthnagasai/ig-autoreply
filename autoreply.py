"""Instagram comment keyword -> DM auto-reply, run every 5 minutes by GitHub Actions.

A comment containing a keyword from rules.json (SAFE, CODE, ...) gets a private-reply DM and a short public reply.
With follow_gate on, the first DM asks the person to follow and reply SEND; the resource goes out when they reply
and Instagram says they follow. Comments that already have a reply from the account (ootto, or the owner by hand)
are left alone, and new comments wait `grace_minutes` first.

Secrets (GitHub repository secrets, never in this repo): IG_TOKEN. Settings: IG_USER_ID, IG_USERNAME (env).
Privacy: this repo and its logs are public, so logs never print usernames or comment text, and state.json
(kept in the Actions cache, not in git) stores only comment ids and SHA-256 hashes of usernames.
Usage: python autoreply.py [--dry-run]
"""
import argparse, hashlib, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOST = os.environ.get("IG_HOST", "https://graph.facebook.com") + "/" + os.environ.get("IG_VERSION", "v23.0")
TOKEN = os.environ.get("IG_TOKEN", "")
UID = os.environ.get("IG_USER_ID", "17841414351865744")
ME = os.environ.get("IG_USERNAME", "hemanth_chakka").lower()
HERE = Path(__file__).resolve().parent
RULES = HERE / "rules.json"
STATE = Path(os.environ.get("STATE_FILE", HERE / "state" / "state.json"))
DRY = False
COUNTS = {"comments_seen": 0, "matched": 0, "asked": 0, "sent": 0, "reminders": 0, "skipped_answered": 0, "errors": 0}


class ApiError(Exception):
    pass


def call(path: str, params: dict | None = None, method: str = "GET") -> dict:
    params = dict(params or {}, access_token=TOKEN)
    url = f"{HOST}/{path.lstrip('/')}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", {})
            msg = f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})"
        except Exception:
            msg = "unreadable error body"
        raise ApiError(f"HTTP {e.code}: {msg}".replace(TOKEN, "<token>") if TOKEN else msg) from None


def paged(path: str, params: dict, limit: int) -> list:
    out, res = [], call(path, params)
    while True:
        out += res.get("data", [])
        nxt = res.get("paging", {}).get("next")
        if len(out) >= limit or not nxt:
            return out[:limit]
        with urllib.request.urlopen(nxt, timeout=60) as r:
            res = json.loads(r.read().decode())


def log(msg: str) -> None:  # never put usernames or comment text in here: the logs are public
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {'[dry] ' if DRY else ''}{msg}", flush=True)


def h(user: str) -> str:
    return hashlib.sha256(("ig-autoreply:" + user.lower()).encode()).hexdigest()[:20]


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp()


def pick(variants: list, key: str) -> str:
    return variants[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(variants)]


def match(text: str, rules: list) -> list:
    return [r for r in rules if re.search(rf"(?<![A-Za-z]){re.escape(r['keyword'])}(?![A-Za-z])", text or "", re.I)]


def send_dm(recipient: dict, text: str) -> None:
    if not DRY:
        call(f"{UID}/messages", {"recipient": json.dumps(recipient), "message": json.dumps({"text": text})}, "POST")


def scan_comments(conf: dict, state: dict) -> None:
    now = time.time()
    media = call(f"{UID}/media", {"fields": "id,timestamp", "limit": conf["media_lookback"]}).get("data", [])
    for m in media:
        try:
            comments = paged(f"{m['id']}/comments", {"fields": "id,text,username,timestamp,replies{username}", "limit": 50}, 500)
        except ApiError as e:
            COUNTS["errors"] += 1; log(f"comments of a post failed: {e}"); continue
        for c in comments:
            COUNTS["comments_seen"] += 1
            cid, user = c["id"], (c.get("username") or "").lower()
            if cid in state["comments"] or user == ME:
                continue
            hits = match(c.get("text", ""), conf["rules"])
            if not hits:
                continue
            age_min = (now - ts(c["timestamp"])) / 60
            if age_min > conf["max_comment_age_days"] * 1440:
                continue  # Instagram allows a private reply only within 7 days of the comment
            COUNTS["matched"] += 1
            if any((r.get("username") or "").lower() == ME for r in c.get("replies", {}).get("data", [])):
                COUNTS["skipped_answered"] += 1
                state["comments"][cid] = {"u": h(user), "k": [x["keyword"] for x in hits], "s": "answered_elsewhere", "t": now}
                continue
            if age_min < conf["ootto_grace_minutes"]:
                continue
            kws = [x["keyword"] for x in hits]
            entry = {"u": h(user), "k": kws, "t": now}
            try:
                if conf["follow_gate"]:
                    send_dm({"comment_id": cid}, conf["gate_dm"].format(name=" + ".join(x["name"] for x in hits)))
                    entry["s"] = "asked"; COUNTS["asked"] += 1
                else:
                    send_dm({"comment_id": cid}, "\n\n".join(x["dm"] for x in hits))
                    entry["s"] = "sent"; COUNTS["sent"] += 1
                log(f"keyword {'/'.join(kws)}: private reply sent ({entry['s']})")
            except ApiError as e:
                entry["s"] = "failed"; entry["e"] = str(e)[:200]; COUNTS["errors"] += 1
                log(f"keyword {'/'.join(kws)}: private reply FAILED: {e}")
            try:
                if not DRY:
                    call(f"{cid}/replies", {"message": pick(hits[0]["public_replies"], cid)}, "POST")
            except ApiError as e:
                COUNTS["errors"] += 1; log(f"public reply failed: {e}")
            if not DRY:
                state["comments"][cid] = entry


def handle_gate(conf: dict, state: dict) -> None:
    pending: dict = {}
    for cid, e in state["comments"].items():
        if e.get("s") == "asked" and time.time() - e["t"] < 7 * 86400:
            pending.setdefault(e["u"], []).append(cid)
    if not pending:
        return
    rules = {r["keyword"]: r for r in conf["rules"]}
    convs = call(f"{UID}/conversations", {"platform": "instagram", "limit": 40,
                 "fields": "participants,updated_time,messages.limit(5){id,from,created_time}"}).get("data", [])
    for conv in convs:
        other = next((p for p in conv.get("participants", {}).get("data", []) if p.get("id") != UID), None)
        if not other or h(other.get("username") or "") not in pending:
            continue
        cids = pending[h(other["username"])]
        asked_at = min(state["comments"][c]["t"] for c in cids)
        theirs = [m for m in conv.get("messages", {}).get("data", [])
                  if m.get("from", {}).get("id") != UID and ts(m["created_time"]) > asked_at - 60]
        if not theirs:
            continue
        newest = max(theirs, key=lambda m: m["created_time"])
        if any(state["comments"][c].get("m") == newest["id"] for c in cids):
            continue
        try:
            follows = call(other["id"], {"fields": "is_user_follow_business"}).get("is_user_follow_business")
        except ApiError as e:
            follows = None
            log(f"follow check unavailable ({e}); sending anyway")
        try:
            if follows is False:
                send_dm({"id": other["id"]}, conf["not_following_dm"])
                COUNTS["reminders"] += 1; log("replied but not following yet: reminder sent")
                for c in cids:
                    state["comments"][c]["m"] = newest["id"]
                continue
            kws: list = []
            for c in cids:
                kws += [k for k in state["comments"][c]["k"] if k not in kws]
            send_dm({"id": other["id"]}, "\n\n".join(rules[k]["dm"] for k in kws if k in rules))
            for c in cids:
                state["comments"][c].update(s="sent", m=newest["id"])
            COUNTS["sent"] += 1; log(f"follows={follows}: {'/'.join(kws)} resource sent")
        except ApiError as e:
            COUNTS["errors"] += 1; log(f"resource DM FAILED: {e}")


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    DRY = ap.parse_args().dry_run
    if not TOKEN:
        log("IG_TOKEN secret is missing"); return 1
    conf = json.loads(RULES.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"comments": {}}
    # forget entries older than 30 days so the state stays small
    state["comments"] = {k: v for k, v in state["comments"].items() if time.time() - v.get("t", 0) < 30 * 86400}
    ok = True
    try:
        scan_comments(conf, state)
        handle_gate(conf, state)
    except Exception as e:
        ok = False; log(f"run failed: {type(e).__name__}: {e}")
    if not DRY:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        state["last_run"] = time.time()
        STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log("summary " + json.dumps(COUNTS) + f", {len(state['comments'])} comments on file")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
