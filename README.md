# ig-autoreply

Comment keyword -> DM auto-reply for an Instagram creator account, run every 5 minutes by GitHub Actions.

Someone comments a keyword (see `rules.json`, e.g. `SAFE` or `CODE`) on any recent post. They get a private-reply DM
and a short public reply. With `follow_gate` on, the first DM asks them to follow and reply SEND; the resource arrives
when they reply and Instagram confirms they follow.

- `autoreply.py`: the script (Python standard library only).
- `rules.json`: keywords, public replies and DM texts. Edit and push to change them.
- `.github/workflows/autoreply.yml`: the schedule. Run it by hand from the Actions tab (with "Dry run" to test).

Setup: add a repository secret `IG_TOKEN` (a Meta system-user token with instagram_basic, instagram_manage_comments,
instagram_manage_messages). The token never lives in this repo.

Privacy: logs print only counts, never usernames or comment text. The run state (comment ids and hashed usernames)
lives in the Actions cache, not in git.
