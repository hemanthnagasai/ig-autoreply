"""Posted steps in the cloud: run by the autoreply workflow every few minutes, before comments are answered.

For each NEW post on @hemanth_chakka (newer than `since` in posted.json, and not already listed there):
  1. Auto-DM: add its shortcode entry to rules.json `posts` (resource = the `match` entry or "Comment XYZ" keyword
     that already covers its caption, else `default_resource`).
  2. Website: read "Reel ID: Rxx" from the caption, find the site file with that reelId and `draft: true` in the
     private site repo (hemanthnagasai/aiwithhemanth, pushed with the SITE_DEPLOY_KEY deploy key), make it live
     (drop `draft: true`, set `date:` to the posted IST time, add `reel: <link>`), push main (Vercel deploys), and
     check the live page answers 200. No Reel ID or no draft file: a GitHub issue says so; nothing is guessed.
  3. Record it in posted.json (shortcode, grid number, Reel ID, slug, time, resource, site result). This repo is
     public: only those fields, never stats or notes. The laptop reads this list to rename folders and update docs.

The workflow commits rules.json + posted.json. Usage: python posted.py [--dry-run] [--fake JSON]
  --fake '{"permalink": "...", "caption": "...", "timestamp": "..."}' adds a made-up newest post (with --dry-run only).
"""
import argparse, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
RULES, LOG = ROOT / "rules.json", ROOT / "posted.json"
HOST = os.environ.get("IG_HOST", "https://graph.facebook.com") + "/" + os.environ.get("IG_VERSION", "v23.0")
TOKEN, USER_ID = os.environ.get("IG_TOKEN", ""), os.environ.get("IG_USER_ID", "17841414351865744")
GH_TOKEN, REPO = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "hemanthnagasai/ig-autoreply")
SITE_REPO = "git@github.com:hemanthnagasai/aiwithhemanth.git"
SITE_URL = "https://techwithhemanth.vercel.app/library/{slug}/"
LIB = "src/content/library"
IST = timezone(timedelta(hours=5, minutes=30))
DRY = False


def log(msg: str) -> None:  # public logs: no comment text, no usernames
    print(f"{datetime.now(timezone.utc):%H:%M:%S}Z posted: {'[dry] ' if DRY else ''}{msg}", flush=True)


def get(url: str, headers: dict | None = None) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


def ig(path: str, **params) -> dict:
    params["access_token"] = TOKEN
    try:
        return get(f"{HOST}/{path}?" + urllib.parse.urlencode(params))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Instagram HTTP {e.code}") from None


def clock(dt: datetime, day: bool = False) -> str:
    """'26 Nov 7:30 pm' (or 'Thu 26 Nov 7:30 pm') in IST, the way the handover writes times."""
    d = dt.astimezone(IST)
    return f"{d:%a} " * day + f"{d.day} {d:%b} {d.hour % 12 or 12}:{d:%M} {'am' if d.hour < 12 else 'pm'}"


def shortcode(permalink: str) -> str:
    m = re.search(r"/(?:reel|reels|p|tv)/([^/?#]+)", permalink or "")
    return m.group(1) if m else ""


def caption_head(caption: str) -> str:  # same rule as autoreply.py
    lines = [l.strip() for l in (caption or "").splitlines() if l.strip() and not re.match(r"(?i)comment\s", l.strip())]
    return " ".join(lines[:2]).lower()


def pick_resource(conf: dict, caption: str) -> tuple[str, str]:
    """(resource key, topic) the way autoreply.py would resolve this caption without a shortcode entry."""
    head, res = caption_head(caption), conf["resources"]
    for p in conf.get("posts", []):
        if not p.get("shortcode") and p.get("resource") in res and any(m in head for m in p.get("match", [])):
            return p["resource"], p.get("topic", "")
    for kw in re.findall(r"(?i:\bcomment)\s+[\"'“]?([A-Z]{3,12})\b", caption or ""):
        for k, r in res.items():
            if r.get("keyword") == kw:
                return k, ""
    return conf.get("default_resource", "site_home"), ""


def add_rules_entry(entry: dict) -> None:
    """Insert one line after the last shortcode line of rules.json `posts`, keeping the file's hand formatting."""
    text = RULES.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    last = max(i for i, l in enumerate(lines) if l.lstrip().startswith('{"shortcode":'))
    lines.insert(last + 1, "  " + json.dumps(entry, ensure_ascii=False) + ",\n")
    new = "".join(lines)
    json.loads(new)  # never write a broken rules.json
    if DRY:
        log("rules.json + " + json.dumps(entry, ensure_ascii=False))
    else:
        RULES.write_text(new, encoding="utf-8")


def issue(title: str, body: str) -> None:
    if DRY or not GH_TOKEN:
        log(f"issue: {title}"); return
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/issues", method="POST",
                                 data=json.dumps({"title": title, "body": body}).encode(),
                                 headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log(f"issue failed: {type(e).__name__}")


def git(cwd: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def site_live(site: str, reel_id: str, permalink: str, posted: datetime, grid: int) -> tuple[str, str]:
    """Make the draft site entry with this Reel ID live. Returns (slug, result)."""
    hits = [p for p in Path(site, LIB).glob("*.md")
            if re.search(rf"(?m)^reelId:\s*{re.escape(reel_id)}\s*$", p.read_text(encoding="utf-8"))]
    if len(hits) != 1:
        return "", f"no single site file with reelId {reel_id} ({len(hits)} found)"
    f = hits[0]; slug = f.stem; text = f.read_text(encoding="utf-8")
    if not re.search(r"(?m)^draft:\s*true\s*$", text):
        return slug, "already live"
    when = posted.astimezone(IST).strftime("%Y-%m-%dT%H:%M:00")
    new = re.sub(r"(?m)^draft:\s*true\s*\n", "", text, count=1)
    new = re.sub(r"(?m)^reel:.*\n", "", new)
    new, n = re.subn(r"(?m)^date:.*$", f"date: {when}\nreel: {permalink}", new, count=1)
    if not n:
        return slug, "no date: line in the front matter"
    if DRY:
        log(f"site {LIB}/{f.name}: draft off, date {when}, reel {permalink}"); return slug, "dry run"
    f.write_text(new, encoding="utf-8")
    title = re.search(r'(?m)^\s+en:\s*"?(.*?)"?\s*$', text)
    msg = (f"{reel_id} {title.group(1) if title else slug}: live (grid {grid}, posted "
           f"{clock(posted)} IST)\n\nMade live by ig-autoreply posted.py (cloud).")
    git(site, "add", str(f.relative_to(site)))
    git(site, "commit", "-q", "-m", msg)
    for _ in range(3):
        try:
            git(site, "push", "-q", "origin", "HEAD:main"); break
        except subprocess.CalledProcessError:
            git(site, "pull", "-q", "--rebase", "origin", "main")
    else:
        return slug, "push failed"
    url = SITE_URL.format(slug=slug)
    for _ in range(20):  # Vercel usually deploys in 1-2 minutes
        time.sleep(15)
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                if r.status == 200:
                    return slug, "live"
        except Exception:
            pass
    return slug, "pushed, page not up after 5 min"


def main() -> int:
    global DRY
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--fake")
    a = ap.parse_args(); DRY = a.dry_run or bool(a.fake)
    if not TOKEN:
        log("IG_TOKEN missing"); return 1
    conf = json.loads(RULES.read_text(encoding="utf-8"))
    book = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else {"since": "2026-11-26T00:00:00+05:30", "posts": []}
    since = datetime.fromisoformat(book["since"])
    known = {p["shortcode"] for p in book["posts"]} | {p["shortcode"] for p in conf.get("posts", []) if p.get("shortcode")}
    media = ig(f"{USER_ID}/media", fields="id,permalink,caption,timestamp,media_type", limit=10).get("data", [])
    count = int(ig(USER_ID, fields="media_count").get("media_count", 0))
    if a.fake:
        media.insert(0, json.loads(a.fake)); count += 1
    for i, m in enumerate(media):
        m["grid"] = count - i  # newest first: the newest post is grid = media_count
    new = [m for m in reversed(media) if shortcode(m.get("permalink")) not in known
           and datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z") > since]
    if not DRY and not book.get("site_key_ok"):  # once: prove the deploy key may push to the site repo
        try:
            d = tempfile.mkdtemp(); git(d, "clone", "-q", "--depth", "1", SITE_REPO, ".")
            git(d, "push", "--dry-run", "-q", "origin", "HEAD:refs/heads/main")
            book["site_key_ok"] = datetime.now(IST).isoformat(timespec="minutes"); log("site deploy key can push")
        except Exception as e:
            book["site_key_ok"] = ""; log(f"site deploy key check failed: {type(e).__name__}")
            issue("Cloud posted steps: the site deploy key cannot push",
                  "posted.py could not push to hemanthnagasai/aiwithhemanth with SITE_DEPLOY_KEY. Website updates after posting will not happen until this is fixed.")
        LOG.write_text(json.dumps(book, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if not new:
        log("no new post"); return 0
    site = None
    for m in new:
        code, cap, grid = shortcode(m["permalink"]), m.get("caption", ""), m["grid"]
        posted = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        rid = re.search(r"\bR(\d{1,3})\b", cap)
        reel_id = f"R{rid.group(1)}" if rid else ""
        res, topic = pick_resource(conf, cap)
        stamp = clock(posted, day=True)
        add_rules_entry({"shortcode": code, "topic": f"{topic or caption_head(cap)[:60]} ({reel_id or 'no Reel ID'}, grid {grid}, posted {stamp})",
                         "resource": res})
        slug, result = "", "no Reel ID in caption"
        if reel_id:
            try:
                if site is None and not DRY:
                    site = tempfile.mkdtemp()
                    git(site, "clone", "-q", "--depth", "20", SITE_REPO, ".")
                    git(site, "config", "user.name", "github-actions[bot]")
                    git(site, "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
                if DRY and site is None:  # dry run reads the laptop's copy when given, else skips the site
                    site = os.environ.get("SITE_DIR", "")
                slug, result = site_live(site, reel_id, m["permalink"], posted, grid) if site else ("", "dry run: no SITE_DIR")
            except Exception as e:
                result = f"site step failed: {type(e).__name__}"
        if result not in ("live", "already live", "dry run"):
            issue(f"Posted {code} (grid {grid}): website not updated",
                  f"New post {m['permalink']} ({reel_id or 'no Reel ID'}). Website: {result}. "
                  f"Make its site entry live by hand (or add 'Reel ID: Rxx' to the caption). Auto-DM entry added: `{res}`.")
        book["posts"].append({"shortcode": code, "grid": grid, "reel_id": reel_id, "slug": slug,
                              "posted_ist": posted.astimezone(IST).isoformat(timespec="minutes"),
                              "resource": res, "site": result})
        log(f"{code} grid {grid} {reel_id or '-'} -> DM {res}, site {result}")
    if not DRY:
        LOG.write_text(json.dumps(book, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
