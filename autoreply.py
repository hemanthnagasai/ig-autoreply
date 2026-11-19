"""Instagram comment keyword -> DM auto-reply, run every 5 minutes by GitHub Actions.

1. Comments: any recent post's comment containing a keyword from rules.json (SAFE, CODE, ...) gets a private-reply
   DM and a short public reply. Comments that already have a reply from the account (ootto, or the owner by hand)
   are left alone; new comments wait `ootto_grace_minutes`.
2. Follow gate: the first DM asks them to follow and reply. The gate step reads the DM threads themselves: when a
   person's newest message comes after our ask (or reminder) for a resource, Instagram's is_user_follow_business
   decides: resource, or a friendly reminder. Nothing here depends on saved state, so a lost cache never strands anyone.
3. Every DM and public reply is composed fresh from the text banks in rules.json (greeting, a line about the reel
   they commented on, the ask or intro, a closing), using their first name when the username gives one, and never
   repeats a recent text or greeting. The resource itself stays word for word, so facts never drift.
4. New posts need nothing: all recent posts are scanned. If a caption asks for a keyword rules.json does not have
   ("Comment XYZ"), the run opens one GitHub issue so the owner gets an email.

Secrets: IG_TOKEN (repository secret). Privacy: this repo and its logs are public, so logs print counts only and the
cached state.json holds comment ids and SHA-256 hashes, never usernames or text.
Usage: python autoreply.py [--dry-run]
"""
import argparse, hashlib, json, os, random, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOST = os.environ.get("IG_HOST", "https://graph.facebook.com") + "/" + os.environ.get("IG_VERSION", "v23.0")
TOKEN = os.environ.get("IG_TOKEN", "")
UID = os.environ.get("IG_USER_ID", "17841414351865744")
ME = os.environ.get("IG_USERNAME", "hemanth_chakka").lower()
GH_TOKEN, GH_REPO = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
HERE = Path(__file__).resolve().parent
RULES = HERE / "rules.json"
STATE = Path(os.environ.get("STATE_FILE", HERE / "state" / "state.json"))
DRY = False
C = {"comments_seen": 0, "matched": 0, "asked": 0, "sent": 0, "reminders": 0, "skipped_answered": 0, "errors": 0}
STOP = {"the", "its", "im", "iam", "mr", "mrs", "ms", "dr", "official", "real", "not", "just", "itz"}
NOT_NAMES = {"user", "insta", "ig", "page", "tech", "world", "daily", "vibes", "life", "india", "telugu", "the", "official", "fan", "fans", "admin", "music", "news", "shop", "store", "photo", "photos"}


class ApiError(Exception):
    pass


def http(url: str, data: bytes | None = None, method: str = "GET", headers: dict | None = None) -> dict:
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", {})
            msg = f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})" if isinstance(err, dict) else str(err)
        except Exception:
            msg = "unreadable error body"
        msg = f"HTTP {e.code}: {msg}"
        raise ApiError(msg.replace(TOKEN, "<token>") if TOKEN else msg) from None
    except urllib.error.URLError as e:
        raise ApiError(f"network: {e.reason}") from None


def call(path: str, params: dict | None = None, method: str = "GET") -> dict:
    params = dict(params or {}, access_token=TOKEN)
    url = f"{HOST}/{path.lstrip('/')}"
    if method == "GET":
        return http(url + "?" + urllib.parse.urlencode(params))
    return http(url, urllib.parse.urlencode(params).encode(), method)


def paged(path: str, params: dict, limit: int) -> list:
    out, res = [], call(path, params)
    while True:
        out += res.get("data", [])
        nxt = res.get("paging", {}).get("next")
        if len(out) >= limit or not nxt:
            return out[:limit]
        res = http(nxt)


def log(msg: str) -> None:  # never put usernames or message text in here: the logs are public
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {'[dry] ' if DRY else ''}{msg}", flush=True)


def hsh(s: str) -> str:
    return hashlib.sha256(("ig-autoreply:" + s).encode()).hexdigest()[:20]


def ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z").timestamp()


# ---------- writing like a person ----------

def first_name(raw: str) -> str:
    """A first name from a username or display name, only when it clearly looks like one ('krishna__priyanka' ->
    'Krishna'; 'techfactsrafee' or 'user_8812' -> '')."""
    for part in re.split(r"[^A-Za-z]+", raw or ""):
        p = part.lower()
        if p in STOP:
            continue
        return p.capitalize() if 3 <= len(p) <= 10 and p.isalpha() and p not in NOT_NAMES else ""
    return ""


def fill(t: str, **kw) -> str:
    for k, v in kw.items():
        t = t.replace("{" + k + "}", v)
    return t


def fresh(state: dict, options: list, key: str, n_recent: int, **kw) -> str:
    """A random option (placeholders filled), skipping ones that need a missing value and ones used recently."""
    usable = [o for o in options if all(kw.get(p) for p in re.findall(r"\{(\w+)\}", o))]
    recent = state.setdefault("recent", {}).setdefault(key, [])
    random.shuffle(usable)
    choice = next((o for o in usable if hsh(o) not in recent), usable[0] if usable else "")
    recent.append(hsh(choice)); del recent[:-n_recent]
    return fill(choice, **kw)


def compose(state: dict, conf: dict, kind: str, things: list, name: str, topic: str, rules: list | None = None) -> str:
    """Builds one DM. Shapes vary too: greeting on its own line or run into the next sentence, context line or not."""
    thing = " and the ".join(things)
    for _ in range(12):
        greet = fresh(state, conf["greetings_name"] if name and random.random() < 0.75 else conf["greetings_plain"], "greet", 6, name=name)
        ctx = ""
        if random.random() < 0.7:
            ctx = fresh(state, conf["context_topic"], "ctx", 4, topic=topic) if topic else fresh(state, conf["context_plain"], "ctx", 3)
        if kind == "ask":
            body = fresh(state, conf["gate_asks"], "ask", 5, thing=thing)
        elif kind == "remind":
            body, ctx = fresh(state, conf["reminders"], "remind", 4, thing=thing), ""
            greet = f"{name}," if name and random.random() < 0.4 else ""
            if greet and not body.startswith("I "):
                body = body[0].lower() + body[1:]
        else:  # resource
            intro = fresh(state, conf["intros"], "intro", 5, thing=thing, name=name)
            parts = []
            for r in rules or []:
                parts.append(r["dm"])
                extra = (r.get("_extra") or "").strip()
                if extra:
                    parts.append(extra)
            outro = fresh(state, sum((r.get("outros", []) for r in rules or []), []) or [""], "outro", 4)
            body = intro + "\n\n" + "\n\n".join(parts) + ("\n\n" + outro if outro else "")
            ctx = "" if random.random() < 0.6 else ctx
        head = " ".join(x for x in [greet, ctx] if x) if random.random() < 0.5 else "\n".join(x for x in [greet, ctx] if x)
        text = (head + ("\n" if "\n" in head or random.random() < 0.5 else " ") + body).strip() if head else body
        sent = state.setdefault("sent_hashes", [])
        if hsh(text) not in sent:
            sent.append(hsh(text)); del sent[:-300]
            return text
    return text


def public_text(state: dict, rule: dict, name: str) -> str:
    return fresh(state, rule["public_replies"], "pub_" + rule["keyword"], 6)


# ---------- what each post is about ----------

def topic_of(conf: dict, caption: str) -> dict:
    c = (caption or "").lower()
    return next((t for t in conf.get("topics", []) if any(m in c for m in t["match"])), {})


def caption_keywords(caption: str) -> set:
    # keywords are written in capitals ("Comment SAFE"), so "comment below" never counts
    return set(re.findall(r"(?i:\bcomment)\s+[\"'“]?([A-Z]{3,12})\b", caption or ""))


def match(text: str, rules: list) -> list:
    return [r for r in rules if re.search(rf"(?<![A-Za-z]){re.escape(r['keyword'])}(?![A-Za-z])", text or "", re.I)]


def send_dm(recipient: dict, text: str) -> None:
    if not DRY:
        call(f"{UID}/messages", {"recipient": json.dumps(recipient), "message": json.dumps({"text": text})}, "POST")


def open_issue(state: dict, keyword: str) -> None:
    """One GitHub issue per unknown keyword, so the owner gets one email (never one per run)."""
    if keyword in state.setdefault("issues", []) or not GH_TOKEN or not GH_REPO or DRY:
        log(f"caption asks for keyword {keyword}, which rules.json does not have"); return
    title = f"Add the {keyword} keyword to rules.json"
    body = (f"A recent post's caption asks people to comment **{keyword}**, but `rules.json` has no rule for it, so those "
            f"comments get no DM.\n\nAdd a rule (keyword, name, marker, public_replies, dm, outros) and push, or add "
            f"`{keyword}` to `ignore_keywords` if it is answered by hand.")
    try:
        http(f"https://api.github.com/repos/{GH_REPO}/issues", json.dumps({"title": title, "body": body}).encode(), "POST",
             {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
        state["issues"].append(keyword); log(f"opened an issue: unknown keyword {keyword}")
    except ApiError as e:
        log(f"could not open the issue for {keyword}: {e}")


# ---------- 1. comments ----------

def scan_comments(conf: dict, state: dict) -> None:
    now = time.time()
    rules = conf["rules"]
    known = {r["keyword"] for r in rules} | {k.upper() for k in conf.get("ignore_keywords", [])}
    media = call(f"{UID}/media", {"fields": "id,timestamp,caption", "limit": conf["media_lookback"]}).get("data", [])
    for m in media:
        for kw in caption_keywords(m.get("caption")) - known:
            if now - ts(m["timestamp"]) < 30 * 86400:
                open_issue(state, kw)
        top = topic_of(conf, m.get("caption"))
        try:
            comments = paged(f"{m['id']}/comments", {"fields": "id,text,username,timestamp,replies{username}", "limit": 50}, 500)
        except ApiError as e:
            C["errors"] += 1; log(f"comments of a post failed: {e}"); continue
        for c in comments:
            C["comments_seen"] += 1
            cid, user = c["id"], (c.get("username") or "").lower()
            if cid in state["comments"] or user == ME:
                continue
            hits = match(c.get("text", ""), rules)
            if not hits:
                continue
            age_min = (now - ts(c["timestamp"])) / 60
            if age_min > conf["max_comment_age_days"] * 1440:
                continue  # Instagram allows a private reply only within 7 days of the comment
            C["matched"] += 1
            if any((r.get("username") or "").lower() == ME for r in c.get("replies", {}).get("data", [])):
                C["skipped_answered"] += 1
                state["comments"][cid] = {"s": "answered_elsewhere", "t": now}
                continue
            if age_min < conf["ootto_grace_minutes"]:
                continue
            name, topic = first_name(c.get("username")), top.get("topic", "")
            kws = [h["keyword"] for h in hits]
            entry = {"k": kws, "t": now, "p": top.get("topic", "")}
            try:
                if conf["follow_gate"]:
                    send_dm({"comment_id": cid}, compose(state, conf, "ask", [h["name"] for h in hits], name, topic))
                    entry["s"] = "asked"; C["asked"] += 1
                else:
                    for h in hits:
                        h["_extra"] = top.get("extra", {}).get(h["keyword"], "")
                    send_dm({"comment_id": cid}, compose(state, conf, "resource", [h["name"] for h in hits], name, topic, hits))
                    entry["s"] = "sent"; C["sent"] += 1
                log(f"keyword {'/'.join(kws)}: private reply sent ({entry['s']})")
            except ApiError as e:
                entry["s"] = "failed"; entry["e"] = str(e)[:200]; C["errors"] += 1
                log(f"keyword {'/'.join(kws)}: private reply FAILED: {e}")
            try:
                if not DRY:
                    call(f"{cid}/replies", {"message": public_text(state, hits[0], name)}, "POST")
            except ApiError as e:
                C["errors"] += 1; log(f"public reply failed: {e}")
            if not DRY:
                state["comments"][cid] = entry


# ---------- 2. follow gate, read from the DM threads ----------

def handle_gate(conf: dict, state: dict) -> None:
    if not conf["follow_gate"]:
        return
    rules = conf["rules"]
    topics = conf.get("topics", [])
    convs = paged(f"{UID}/conversations", {"platform": "instagram", "limit": 10,
                  "fields": "participants,updated_time,messages.limit(6){id,from,message,created_time}"}, 40)
    for conv in convs:
        if time.time() - ts(conv["updated_time"]) > 7 * 86400:
            continue
        msgs = sorted(conv.get("messages", {}).get("data", []), key=lambda m: m["created_time"])
        if not msgs or msgs[-1].get("from", {}).get("id") == UID:
            continue  # nothing new from them
        ours = [m for m in msgs if m.get("from", {}).get("id") == UID]
        if not ours:
            continue
        last_ours = ours[-1].get("message") or ""
        wanted = [r for r in rules if any(a.lower() in last_ours.lower() for a in [r["name"]] + r.get("aliases", []))
                  and r["marker"].lower() not in last_ours.lower()]
        if not wanted:
            continue  # our last message was not a gate ask or reminder (or the resource already went out)
        newest = msgs[-1]
        if newest["id"] in state.setdefault("handled_msgs", []):
            continue
        other = next((p for p in conv.get("participants", {}).get("data", []) if p.get("id") != UID), {})
        try:
            prof = call(other["id"], {"fields": "name,is_user_follow_business"})
            follows, name = prof.get("is_user_follow_business"), first_name(prof.get("name") or other.get("username"))
        except ApiError as e:
            follows, name = None, first_name(other.get("username"))
            log(f"follow check unavailable ({e}); sending anyway")
        entry_topic = next((t for t in topics if t.get("topic") and t["topic"].lower() in last_ours.lower()), {})
        try:
            if follows is False:
                send_dm({"id": other["id"]}, compose(state, conf, "remind", [r["name"] for r in wanted], name, ""))
                C["reminders"] += 1; log("replied but not following yet: reminder sent")
            else:
                for r in wanted:
                    r["_extra"] = entry_topic.get("extra", {}).get(r["keyword"], "")
                send_dm({"id": other["id"]}, compose(state, conf, "resource", [r["name"] for r in wanted], name,
                                                     entry_topic.get("topic", ""), wanted))
                C["sent"] += 1; log(f"follows={follows}: {'/'.join(r['keyword'] for r in wanted)} resource sent")
            state["handled_msgs"].append(newest["id"]); del state["handled_msgs"][:-500]
        except ApiError as e:
            C["errors"] += 1; log(f"gate DM FAILED: {e}")


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    DRY = ap.parse_args().dry_run
    if not TOKEN:
        log("IG_TOKEN secret is missing"); return 1
    conf = json.loads(RULES.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    state.setdefault("comments", {})
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
    log("summary " + json.dumps(C) + f", {len(state['comments'])} comments on file")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
