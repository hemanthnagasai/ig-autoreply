# ig-autoreply

Comment keyword -> DM auto-reply for @hemanth_chakka, running on GitHub Actions. Live since 19 Nov 2026.

## What happens

Someone comments a keyword (today `SAFE` or `CODE`) on any recent post. About 2-4 minutes later:

1. They get **one DM** (Instagram's "private reply" to a comment) with the resource:
   a varied greeting (their first name when the username clearly gives one), sometimes a line naming the reel
   they commented on, a varied intro, the resource **word for word**, and the owner's closing line
   "Hope you will follow me for such more updates on time".
2. A short public reply goes under their comment ("Sent! Check your DMs", 10 rotating versions per keyword).

Every DM is composed fresh from the text banks in `rules.json`; recent greetings and whole messages are never repeated.
Instagram caps a DM at 1000 characters; messages stay under ~930 (a comment with two keywords gets the first one).

## Why there is no follow gate

Tested 19 Nov 2026 with a real comment: Meta lets this app (a personal developer app, not reviewed) send exactly **one
DM per comment**. The follow check (`is_user_follow_business`) fails with error 230 and any second message fails with
error 10 "outside of allowed window", even right after the person replies. A follow gate needs Meta app review
(Advanced Access for instagram_manage_messages + messaging webhooks). The gate code is still here: `follow_gate: true`
in `rules.json` turns it back on once the app is approved.

## Files

- `autoreply.py`: the script (Python standard library only). `python autoreply.py --dry-run` sends nothing.
- `rules.json`: keywords, DM texts, text banks, reel topics, ignored keywords. **Edit, commit, push** to change wording.
- `.github/workflows/autoreply.yml`: the schedule.

## Running

- Each run checks every 60 s for ~4 minutes (`--loop 240`), then saves its state in the Actions cache.
- GitHub's timer (`*/5`) is best effort and did not fire at all in the first hour on 19 Nov, so each run ends with a
  **backup trigger**: if no timed run happened in the last 12 minutes, it starts the next run itself.
- A keepalive commit on the 1st and 15th stops GitHub pausing the schedule after 60 days without activity.
- Run by hand: Actions tab > autoreply > Run workflow (tick "Dry run" to test). Each run's log ends with a summary line
  (comments seen, matched, sent, errors). Logs never print usernames or comment text (the repo is public).

## Adding a keyword for a new reel

New posts with an existing keyword need nothing: every run scans the recent posts. For a new keyword (owner's yes first):
add a rule (`keyword`, `name`, `aliases`, `marker` = a phrase only the resource contains, `public_replies`, `dm`,
`outros` (currently empty: the owner's line closes every DM)) and a `topics` entry whose `match` words appear in the
reel caption's first two lines; then commit and push before the post goes live. If a caption asks for a keyword that
`rules.json` does not know ("Comment XYZ" in capitals), the run opens one GitHub issue so the owner gets an email.
Old one-off asks answered by hand go in `ignore_keywords` (CLAUDE, YES, NO).

## Alerts and fixes

- One GitHub issue (email) if a DM fails or a caption has an unknown keyword.
- Token: repository secret `IG_TOKEN` (Meta system-user token from the ig-graph setup; does not expire). To replace it,
  the owner runs in PowerShell:
  `(Get-Content "$HOME\.ig-graph\config.json" | ConvertFrom-Json).token | gh secret set IG_TOKEN --repo hemanthnagasai/ig-autoreply`
- Never run two senders at once (they keep separate state and would DM twice): ootto's auto-reply is OFF since 19 Nov,
  and the old laptop copy (Windows task "IG AutoReply SAFE-CODE", folder `E:/Instagram/autopilot/autoreply/`) is disabled.
- Comments already answered by the account (by hand or ootto) are skipped; new comments wait `ootto_grace_minutes` (2).
- Instagram allows a private reply only within 7 days of a comment; older comments are skipped (`max_comment_age_days` 6).
