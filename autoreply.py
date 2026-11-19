"""Instagram comment -> DM auto-reply, run every 5 minutes by GitHub Actions.

1. Any comment on a post gets one private-reply DM carrying THAT post's own resource, and a short public reply.
   The post finds its resource in rules.json `posts` (by its link's shortcode, then by words in the caption's first
   lines, then by the caption's "Comment XYZ" keyword). A post with no resource keeps its comments waiting and opens
   one GitHub issue.
2. Writing like a person: the DM opener (greeting + a line reacting to what they said) and the public reply are
   written fresh for each comment by a small model on GitHub Models (free, the workflow's own GITHUB_TOKEN). Every
   result is checked (no links, numbers, promises, em dashes, bot phrases, no repeat of a recent line); anything that
   fails, or a model outage, falls back to the text banks in rules.json. The resource itself stays word for word, so
   facts never drift. Each comment waits its own random 2-7 minutes; one DM per person per post (a second comment
   from them gets only a short public reply); spam, promotions and abuse get nothing.
3. Follow gate (off: Meta allows this app one DM per comment, see README) reads the DM threads when switched on.

Secrets: IG_TOKEN (repository secret). Privacy: this repo and its logs are public, so logs print counts only and the
cached state.json holds comment ids and SHA-256 hashes, never usernames or text. The writer model sees the comment
text and a first name (both public on Instagram), nothing else.
Usage: python autoreply.py [--dry-run] [--loop SECONDS] | python autoreply.py --selftest (made-up comments only)
"""
import argparse, hashlib, json, os, random, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOST = os.environ.get("IG_HOST", "https://graph.facebook.com") + "/" + os.environ.get("IG_VERSION", "v23.0")
TOKEN = os.environ.get("IG_TOKEN", "")
UID = os.environ.get("IG_USER_ID", "17841414351865744")
ME = os.environ.get("IG_USERNAME", "hemanth_chakka").lower()
GH_TOKEN, GH_REPO = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODELS_URL = os.environ.get("MODELS_URL", "https://models.github.ai/inference/chat/completions")
HERE = Path(__file__).resolve().parent
RULES = HERE / "rules.json"
STATE = Path(os.environ.get("STATE_FILE", HERE / "state" / "state.json"))
DRY = False
C = {"comments_seen": 0, "eligible": 0, "sent": 0, "public_only": 0, "skipped_spam": 0, "waiting_no_resource": 0,
     "writer": 0, "banks": 0, "asked": 0, "reminders": 0, "errors": 0}
STOP = {"the", "its", "im", "iam", "mr", "mrs", "ms", "dr", "official", "real", "not", "just", "itz"}
NOT_NAMES = {"user", "insta", "ig", "page", "tech", "world", "daily", "vibes", "life", "india", "telugu", "the", "official",
             "fan", "fans", "admin", "music", "news", "shop", "store", "photo", "photos"}
NOT_IN_NAMES = ("tech", "fan", "insta", "gram", "official", "edit", "gamer", "vlog", "shop", "store", "news", "page",
                "media", "music", "photo", "world", "daily", "club", "army", "creat", "studio", "status", "quote", "meme")
SPAM = re.compile(r"(?i)(promot|collab|\bdm (me|us)\b|check (out )?my (page|profile|bio)|follow (me )?back|\bf4f\b|crypto|"
                  r"bitcoin|forex|earn \$|earn money|whatsapp me|telegram|https?://|www\.|\.com\b|giveaway winner|send pic)")
THANKS = re.compile(r"(?i)\b(thanks?|thank you|thx|tq|ty|nice|super|superb|great|good|useful|helpful|awesome|amazing|"
                    r"love|best|wow|informative|valuable|keep it up|well done|mast|bagundi|chala)\b|❤|♥|🔥|👏|👍|🙏|😍")
QUESTION = re.compile(r"(?i)\?|^(how|what|why|where|when|which|who|can|could|does|do|is|are|will|should|kya|ela|enti|ekkada)\b")
BOT_PHRASES = ("thank you for your comment", "we appreciate", "i hope this helps", "hope this helps", "feel free",
               "as an ai", "don't hesitate", "do not hesitate", "valued", "dear user", "kindly", "reach out",
               "i'll tell you", "i will tell you", "i'll send", "i will send", "let me know", "stay tuned")


class ApiError(Exception):
    pass


def http(url: str, data: bytes | None = None, method: str = "GET", headers: dict | None = None, timeout: int = 60) -> dict:
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            try:
                return json.loads(body) if body else {}
            except json.JSONDecodeError:
                raise ApiError(f"HTTP {r.status}, not JSON ({r.headers.get('Content-Type')}): {body[:80]!r}") from None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", {})
            msg = f"{err.get('message')} (code {err.get('code')}, subcode {err.get('error_subcode')})" if isinstance(err, dict) else str(err)
        except Exception:
            msg = "unreadable error body"
        msg = f"HTTP {e.code}: {msg}"
        for secret in (TOKEN, GH_TOKEN, ANTHROPIC_KEY):
            if secret:
                msg = msg.replace(secret, "<token>")
        raise ApiError(msg) from None
    except urllib.error.URLError as e:
        raise ApiError(f"network: {e.reason}") from None
    except (TimeoutError, json.JSONDecodeError) as e:
        raise ApiError(f"{type(e).__name__}") from None


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


# ---------- which resource belongs to which post ----------

def caption_head(caption: str) -> str:
    """The caption's first two lines that are not the 'Comment X' ask: the hook, which names the reel's topic."""
    lines = [l.strip() for l in (caption or "").splitlines() if l.strip() and not re.match(r"(?i)comment\s", l.strip())]
    return " ".join(lines[:2]).lower()


def caption_keywords(caption: str) -> set:
    # keywords are written in capitals ("Comment SAFE"), so "comment below" never counts
    return set(re.findall(r"(?i:\bcomment)\s+[\"'“]?([A-Z]{3,12})\b", caption or ""))


def shortcode(permalink: str) -> str:
    m = re.search(r"/(?:reel|reels|p|tv)/([^/?#]+)", permalink or "")
    return m.group(1) if m else ""


def resolve_post(conf: dict, media: dict) -> dict | None:
    """{'resource': {...}, 'topic': str, 'extra': str} for a post, or None when nothing in rules.json covers it."""
    res = conf["resources"]
    code, head = shortcode(media.get("permalink")), caption_head(media.get("caption"))
    entry = next((p for p in conf.get("posts", []) if p.get("shortcode") and p["shortcode"] == code), None)
    entry = entry or next((p for p in conf.get("posts", []) if not p.get("shortcode")
                           and any(m in head for m in p.get("match", []))), None)
    if entry and entry.get("resource") in res:
        return {"resource": res[entry["resource"]], "topic": entry.get("topic", ""), "extra": entry.get("extra", "")}
    for kw in caption_keywords(media.get("caption")):
        r = next((r for r in res.values() if r.get("keyword") == kw), None)
        if r:
            return {"resource": r, "topic": "", "extra": ""}
    return None


# ---------- writing like a person ----------

def first_name(raw: str) -> str:
    """A first name from a username or display name, only when it clearly looks like one ('krishna__priyanka' ->
    'Krishna'; 'techfactsrafee' or 'user_8812' -> '')."""
    for part in re.split(r"[^A-Za-z]+", raw or ""):
        p = part.lower()
        if p in STOP:
            continue
        looks_like_name = 3 <= len(p) <= 10 and p.isalpha() and p not in NOT_NAMES and not any(w in p for w in NOT_IN_NAMES)
        return p.capitalize() if looks_like_name else ""
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


def kind_of(text: str, resource: dict) -> str:
    t = (text or "").strip()
    kw = resource.get("keyword")
    if kw and re.search(rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])", t, re.I):
        return "keyword"
    if "@" in t:
        return "tag"
    if not re.search(r"[A-Za-z0-9\u0900-\u0D7F]", t):
        return "emoji"
    if QUESTION.search(t):
        return "question"
    if THANKS.search(t):
        return "thanks"
    return "other"


def bank_public(state: dict, conf: dict, resource: dict, kind: str) -> str:
    banks = conf["public_by_type"]
    if kind == "keyword" and resource.get("public_replies"):
        return fresh(state, resource["public_replies"], "pub_" + resource["name"], 6)
    return fresh(state, banks.get(kind) or banks["other"], "pub_" + kind, 4, thing=resource["name"])


def bank_opener(state: dict, conf: dict, name: str, topic: str, thing: str, kind: str = "other") -> str:
    """Greeting + a line reacting to their comment (or sometimes naming the reel) + an intro; shapes vary."""
    named = bool(name) and random.random() < 0.75
    greet = fresh(state, conf["greetings_name"] if named else conf["greetings_plain"], "greet", 6, name=name)
    if name and not named:  # the name either opens the DM or sits in a greeting-less intro ("Thanks Priya! ..."), never both
        greet = "" if random.random() < 0.5 else greet
        named = bool(greet)
    ctx = ""
    react = conf.get("react_by_type", {}).get(kind)
    if react and random.random() < 0.85:
        ctx = fresh(state, react, "react_" + kind, 3)
    elif random.random() < 0.45:
        ctx = fresh(state, conf["context_topic"], "ctx", 4, topic=topic) if topic else fresh(state, conf["context_plain"], "ctx", 3)
    intro = fresh(state, conf["intros"], "intro", 5, thing=thing, name="" if named else name)  # the name once, never twice
    parts = [x for x in (greet, ctx, intro) if x]
    parts = [p + random.choice("!.") if p[-1].isalnum() and i < len(parts) - 1 else p for i, p in enumerate(parts)]
    return (" " if random.random() < 0.5 else "\n").join(parts)


def clean(s: str) -> str:
    s = (s or "").strip().strip('"').replace("—", ", ").replace("–", ", ").replace(" ,", ",")
    return re.sub(r"[ \t]+", " ", s).strip()


def acceptable(s: str, limit: int, recent: list, allow_empty: bool = False) -> bool:
    if not s:
        return allow_empty
    low = s.lower()
    return (len(s) <= limit and not re.search(r"https?://|www\.|@|#|\d|\[|\]|\{|\}", s)
            and not any(p in low for p in BOT_PHRASES) and hsh(low) not in recent)


def writer(conf: dict, state: dict, ctx: dict) -> dict | None:
    """Asks the model for {'skip', 'public', 'opener'}. None when off, unavailable, or the answer fails a check."""
    w = conf.get("writer", {})
    if not w.get("enabled") or not (ANTHROPIC_KEY or (w.get("github_models") and GH_TOKEN)):
        return None
    rp = state.setdefault("recent", {}).setdefault("w_public", [])
    ro = state["recent"].setdefault("w_opener", [])
    system = (
        "You write as Hemanth (@hemanth_chakka), an Indian creator who makes phone-safety and AI tips simple for normal "
        "families. Someone commented on one of his posts. Write two short things in his voice, as he would type them on "
        "his phone:\n"
        "public: his reply under their comment, 2 to 12 words, reacting to what they actually said. When a DM is being "
        "sent, mention it casually and differently each time (not always 'check your DMs').\n"
        "opener: the first line or two of his DM, before the resource. Greet them (use the first name only if one is "
        "given), react to their comment like a friend would, then lead into the resource. At most 160 characters.\n"
        "Rules: simple everyday English. Change the wording, length and punctuation every time. At most one emoji in "
        "each, often none. No em dashes, hashtags, links, @mentions or digits. Add no facts, numbers or claims of your "
        "own: the resource carries the facts. Never promise anything later. If they ask a question, do not answer it; "
        "say the DM has the steps, or that it is a good question. Never sound like customer service ('Thank you for "
        "your comment', 'I hope this helps', 'Feel free', 'Let me know'). Do not reuse any line from the recent lists.\n"
        "If the comment is spam, a promotion, a bot, hate or abuse, set skip to true.\n"
        "If repeat is true, they already got the DM: set opener to \"\" and write only a short friendly public reply "
        "that does not mention DMs.\n"
        'Answer with JSON only: {"skip": false, "public": "...", "opener": "..."}')
    user = json.dumps({"post": ctx["topic"] or "a post", "resource": ctx["thing"], "first_name": ctx["name"] or None,
                       "comment": ctx["text"][:300], "repeat": ctx["repeat"],
                       "recent_public": state["recent"].get("w_public_txt", [])[-6:],
                       "recent_openers": state["recent"].get("w_opener_txt", [])[-6:]}, ensure_ascii=False)
    out, errors = None, []
    if ANTHROPIC_KEY:  # Claude writes the replies when the owner has added the ANTHROPIC_API_KEY secret
        body = json.dumps({"model": w.get("claude_model", "claude-haiku-5-5"), "max_tokens": 300, "temperature": 1.0,
                           "system": system, "messages": [{"role": "user", "content": user}]}).encode()
        try:
            res = http("https://api.anthropic.com/v1/messages", body, "POST",
                       {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}, timeout=30)
            txt = "".join(b.get("text", "") for b in res.get("content", []))
            out = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
        except (ApiError, ValueError, KeyError, TypeError) as e:
            errors.append(f"claude: {str(e)[:90]}")
    endpoints = (w.get("endpoints") or [{"url": MODELS_URL, "model": w.get("model", "openai/gpt-4.1-mini")}]) if w.get("github_models") else []
    start = state.get("writer_endpoint", 0) % max(len(endpoints), 1)  # the one that last worked goes first
    for k in ([start] + [i for i in range(len(endpoints)) if i != start]) if endpoints and out is None else []:
        ep = endpoints[k]
        body = json.dumps({"model": ep["model"], "temperature": 1.0, "max_tokens": 200,
                           "response_format": {"type": "json_object"},
                           "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
        try:
            res = http(ep["url"], body, "POST", {"Authorization": f"Bearer {GH_TOKEN}", "Content-Type": "application/json",
                                                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
                       timeout=30)
            out = json.loads(res["choices"][0]["message"]["content"])
            state["writer_endpoint"] = k
            break
        except (ApiError, KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            errors.append(f"#{k}: {str(e)[:90]}")
    if out is None:
        log(f"writer unavailable ({'; '.join(errors)}): using the text banks")
        return None
    if out.get("skip") is True:
        return {"skip": True}
    pub, op = clean(out.get("public")), clean(out.get("opener"))
    if not acceptable(pub, 110, rp) or not acceptable(op, 200, ro, allow_empty=ctx["repeat"]):
        log("writer answer failed a check: using the text banks")
        return None
    for key, txt in (("w_public", pub), ("w_opener", op)):
        if txt:
            state["recent"][key].append(hsh(txt.lower())); del state["recent"][key][:-40]
            # the last few lines themselves, so the model can avoid them; kept only in the private Actions cache
            # (first names swapped back to {name}, so the cache holds no one's name)
            kept = txt.replace(ctx["name"], "{name}") if ctx["name"] else txt
            state["recent"].setdefault(key + "_txt", []).append(kept); del state["recent"][key + "_txt"][:-6]
    return {"skip": False, "public": pub, "opener": op}


def build_dm(conf: dict, state: dict, opener: str, resource: dict, extra: str) -> str:
    """Opener + the resource word for word + the post's bonus line + the owner's closing line, under Instagram's
    1000-character cap (the bonus line goes first, then the closing line, never the resource)."""
    closing = conf.get("closing_line", "")
    sep = "\n\n" if random.random() < 0.6 else "\n"
    for parts in ([opener, resource["dm"], extra, closing], [opener, resource["dm"], closing], [opener, resource["dm"]]):
        text = sep.join(p.strip() for p in parts if p and p.strip())
        if len(text) <= 950:
            break
    sent = state.setdefault("sent_hashes", [])
    sent.append(hsh(text)); del sent[:-300]
    return text


# ---------- Instagram calls and alerts ----------

def send_dm(recipient: dict, text: str) -> None:
    if not DRY:
        call(f"{UID}/messages", {"recipient": json.dumps(recipient), "message": json.dumps({"text": text})}, "POST")


def alert(state: dict, key: str, title: str, body: str) -> None:
    """One GitHub issue per problem (an email to the owner), never one per run."""
    if key in state.setdefault("issues", []) or not GH_TOKEN or not GH_REPO or DRY:
        log(f"alert (not opened here): {key}"); return
    try:
        http(f"https://api.github.com/repos/{GH_REPO}/issues", json.dumps({"title": title, "body": body}).encode(), "POST",
             {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"})
        state["issues"].append(key); log(f"opened an issue: {key}")
    except ApiError as e:
        log(f"could not open issue {key}: {e}")


# ---------- 1. comments ----------

def scan_comments(conf: dict, state: dict) -> None:
    now = time.time()
    since = ts(conf["any_comment_since"]) if conf.get("any_comment_since") else 0
    known = {r["keyword"] for r in conf["resources"].values() if r.get("keyword")} | {k.upper() for k in conf.get("ignore_keywords", [])}
    lo, hi = conf.get("min_delay_minutes", 2), conf.get("max_delay_minutes", 7)
    media = call(f"{UID}/media", {"fields": "id,timestamp,caption,permalink,comments_count", "limit": conf["media_lookback"]}).get("data", [])
    for m in media:
        post = resolve_post(conf, m)
        if not post:
            for kw in caption_keywords(m.get("caption")) - known:
                if now - ts(m["timestamp"]) < 30 * 86400:
                    alert(state, kw, f"Add the {kw} keyword to rules.json",
                          f"A recent post's caption asks people to comment **{kw}**, but `rules.json` has no resource for "
                          f"it. Add a `posts` entry for the post (or a resource with that keyword) and push, or add `{kw}` "
                          f"to `ignore_keywords`.")
        if not m.get("comments_count"):
            continue
        try:
            comments = paged(f"{m['id']}/comments", {"fields": "id,text,username,timestamp,replies{username}", "limit": 50}, 500)
        except ApiError as e:
            C["errors"] += 1; log(f"comments of a post failed: {e}"); continue
        for c in comments:
            C["comments_seen"] += 1
            cid, user, text = c["id"], (c.get("username") or "").lower(), c.get("text") or ""
            if cid in state["comments"] or user == ME:
                continue
            t = ts(c["timestamp"])
            if t < since or now - t > conf["max_comment_age_days"] * 86400:
                continue  # before the switch-over, or past Instagram's 7-day private-reply window
            if (now - t) / 60 < lo + int(hsh(cid), 16) % (hi - lo + 1):
                continue  # this comment's own random wait is not over yet
            C["eligible"] += 1
            if not post:
                C["waiting_no_resource"] += 1
                alert(state, "post-" + m["id"], "A post has comments but no resource in rules.json",
                      f"Post {m.get('permalink')} has new comments, but `rules.json` does not say which resource it "
                      f"gives, so those comments are waiting (Instagram allows a DM up to 7 days after a comment). Add a "
                      f"`posts` entry (shortcode `{shortcode(m.get('permalink'))}` + resource) and push.")
                continue
            if SPAM.search(text):
                C["skipped_spam"] += 1
                if not DRY:
                    state["comments"][cid] = {"s": "spam", "t": now}
                continue
            res, topic = post["resource"], post["topic"]
            ukey = hsh(m["id"] + ":" + user)
            repeat = ukey in state.setdefault("dm_users", {})
            already_public = any((r.get("username") or "").lower() == ME for r in c.get("replies", {}).get("data", []))
            name = first_name(c.get("username"))
            w = writer(conf, state, {"topic": topic, "thing": res["name"], "name": name, "text": text, "repeat": repeat})
            if w and w.get("skip"):
                C["skipped_spam"] += 1
                if not DRY:
                    state["comments"][cid] = {"s": "skipped", "t": now}
                log("writer marked a comment as spam/abuse: skipped")
                continue
            C["writer" if w else "banks"] += 1
            kind = kind_of(text, res)
            public = w["public"] if w else bank_public(state, conf, res, "repeat" if repeat else kind)
            entry = {"t": now, "r": res["name"], "w": bool(w)}
            if repeat:
                entry["s"] = "repeat"; C["public_only"] += 1
                log("second comment from someone already sent this post's resource: public reply only")
            else:
                opener = w["opener"] if w else bank_opener(state, conf, name, topic, res["name"], kind)
                try:
                    send_dm({"comment_id": cid}, build_dm(conf, state, opener, res, post["extra"]))
                    entry["s"] = "sent"; C["sent"] += 1
                    state["dm_users"][ukey] = now
                    log(f"DM sent ({'writer' if w else 'banks'}, {kind} comment)")
                except ApiError as e:
                    entry["s"] = "failed"; entry["e"] = str(e)[:200]; C["errors"] += 1
                    log(f"private reply FAILED: {e}")
                    alert(state, "dm-failing", "Auto-DMs are failing",
                          "A private-reply DM to a comment failed with:\n\n`" + str(e)[:300] + "`\n\nIf this says "
                          "permission / not allowed / user cannot be found for every comment, Meta is blocking this app "
                          "from messaging people who are not on the app.")
            if not already_public and public:
                try:
                    if not DRY:
                        call(f"{cid}/replies", {"message": public}, "POST")
                except ApiError as e:
                    C["errors"] += 1; log(f"public reply failed: {e}")
            if not DRY:
                state["comments"][cid] = entry


# ---------- 2. follow gate, read from the DM threads (off until Meta app review) ----------

def handle_gate(conf: dict, state: dict) -> None:
    if not conf["follow_gate"]:
        return
    rules = [r for r in conf["resources"].values() if r.get("marker")]
    convs = paged(f"{UID}/conversations", {"platform": "instagram", "limit": 10,
                  "fields": "participants,updated_time,messages.limit(6){id,from,message,created_time}"}, 40)
    for conv in convs:
        if time.time() - ts(conv["updated_time"]) > 7 * 86400:
            continue
        msgs = sorted(conv.get("messages", {}).get("data", []), key=lambda m: m["created_time"])
        if not msgs or msgs[-1].get("from", {}).get("id") == UID:
            continue
        ours = [m for m in msgs if m.get("from", {}).get("id") == UID]
        if not ours:
            continue
        last_ours = (ours[-1].get("message") or "").lower()
        wanted = [r for r in rules if any(a.lower() in last_ours for a in [r["name"]] + r.get("aliases", []))
                  and r["marker"].lower() not in last_ours]
        if not wanted or msgs[-1]["id"] in state.setdefault("handled_msgs", []):
            continue
        other = next((p for p in conv.get("participants", {}).get("data", []) if p.get("id") != UID), {})
        try:
            prof = call(other["id"], {"fields": "name,is_user_follow_business"})
            follows, name = prof.get("is_user_follow_business"), first_name(prof.get("name") or other.get("username"))
        except ApiError as e:
            follows, name = None, first_name(other.get("username"))
            log(f"follow check unavailable ({e}); sending anyway")
        try:
            r = wanted[0]
            if follows is False:
                greet = f"{name}, " if name else ""
                send_dm({"id": other["id"]}, greet + fresh(state, conf["reminders"], "remind", 3, thing=r["name"]))
                C["reminders"] += 1; log("replied but not following yet: reminder sent")
            else:
                send_dm({"id": other["id"]}, build_dm(conf, state, bank_opener(state, conf, name, "", r["name"]), r, ""))
                C["sent"] += 1; log(f"follows={follows}: resource sent")
            state["handled_msgs"].append(msgs[-1]["id"]); del state["handled_msgs"][:-500]
        except ApiError as e:
            C["errors"] += 1; log(f"gate DM FAILED: {e}")


# ---------- self-test with made-up comments (safe to print: no real people) ----------

def selftest(conf: dict) -> int:
    state = {}
    samples = [("SAFE", "priya_reddy", "safety_card", "WhatsApp settings reel", False),
               ("Super useful bro, sending to my mom", "ravi.k_99", "safety_card", "family code word reel", False),
               ("Does this work on iPhone?", "techfan_2024", "voice_note", "voice note reel", False),
               ("🔥🔥", "anjali__s", "pf_missed_call", "PF balance reel", False),
               ("@kiran_m look at this", "deepu.writes", "chatgpt_codes", "ChatGPT codes reel", False),
               ("Thanks again!", "priya_reddy", "safety_card", "WhatsApp settings reel", True),
               ("Promote your page on @growthhub, DM us", "growthhub", "safety_card", "WhatsApp settings reel", False)]
    for text, user, rid, topic, repeat in samples:
        res = conf["resources"][rid]
        print("=" * 60, f"\nCOMMENT ({'repeat' if repeat else 'first'} on {topic}): {text}")
        if SPAM.search(text):
            print("-> skipped as spam (pattern)"); continue
        name = first_name(user)
        w = writer(conf, state, {"topic": topic, "thing": res["name"], "name": name, "text": text, "repeat": repeat})
        if w and w.get("skip"):
            print("-> skipped by the writer"); continue
        kind = kind_of(text, res)
        pub = w["public"] if w else bank_public(state, conf, res, "repeat" if repeat else kind)
        print(f"[{'writer' if w else 'banks'} | {kind}] PUBLIC: {pub}")
        if not repeat:
            op = w["opener"] if w else bank_opener(state, conf, name, topic, res["name"], kind)
            dm = build_dm(conf, state, op, res, "")
            print(f"DM ({len(dm)} chars):\n{dm}")
    return 0


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="keep checking every 60 s for this many seconds")
    ap.add_argument("--selftest", action="store_true", help="print replies for made-up comments; touches no account")
    args = ap.parse_args(); DRY = args.dry_run or args.selftest
    conf = json.loads(RULES.read_text(encoding="utf-8"))
    if args.selftest:
        return selftest(conf)
    if not TOKEN:
        log("IG_TOKEN secret is missing"); return 1
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    state.setdefault("comments", {})
    state["comments"] = {k: v for k, v in state["comments"].items() if time.time() - v.get("t", 0) < 30 * 86400}
    state["dm_users"] = {k: v for k, v in state.get("dm_users", {}).items() if time.time() - v < 30 * 86400}
    ok, end = True, time.time() + args.loop
    while True:
        try:
            scan_comments(conf, state)
            handle_gate(conf, state)
        except Exception as e:
            ok = False; log(f"pass failed: {type(e).__name__}: {e}")
        if not DRY:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            state["last_run"] = time.time()
            STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
        if time.time() + 60 > end:
            break
        time.sleep(60)
    log("summary " + json.dumps(C) + f", {len(state['comments'])} comments on file")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
