# ig-autoreply

Comment -> DM auto-reply for @hemanth_chakka, running on GitHub Actions. Live since 19 Nov 2026; any-comment mode
since 19 Nov 2026 evening.

## What happens

Someone comments **anything** on a post (a keyword, "send", "super useful", a question, an emoji, a friend's tag).
After that comment's own random wait of 2-7 minutes:

1. They get **one DM** (Instagram's "private reply" to a comment) carrying **that post's own resource**, never another
   post's: the WhatsApp reel gives the family safety card, the voice-note reel the voice-note steps, the ChatGPT codes
   reel the codes, and so on. The DM is: an opener written for their comment (greeting, their first name when the
   username clearly gives one, a line reacting to what they said) + the resource **word for word** + the post's bonus
   line if it has one + the owner's closing line "Hope you will follow me for such more updates on time".
2. A short public reply goes under their comment, reacting to what they said (skipped if the account already replied).

Rules that keep it human:
- **Written fresh each time.** The opener and the public reply come from a small model on GitHub Models
  (`openai/gpt-4.1-mini`, free with the workflow's own `GITHUB_TOKEN`, permission `models: read`). It is told to write
  as Hemanth types on his phone, vary wording and length, add no facts, numbers, links or promises, and not answer
  questions itself. Every answer is checked (length, no links/digits/@/#, no em dashes, no bot phrases like "Thank you
  for your comment", not a repeat of a recent line). If the check fails or the model is down, the text banks in
  `rules.json` are used instead (greetings, reel lines, intros, and public replies by comment type: thanks, question,
  emoji, tag, other, repeat).
- **One DM per person per post.** A second comment from the same person on the same post gets only a short public
  reply.
- **Spam, promotions, bots and abuse get nothing** (a pattern check, then the model's judgement).
- **The resource never changes.** Facts and steps stay exactly as written in `rules.json`.

## Which resource a post gives

`rules.json` -> `posts`, checked in this order:
1. `shortcode`: the part after `/reel/` (or `/p/`) in the post's link. Every post on the grid is listed this way.
2. `match`: words found in the caption's first two lines (a line starting with "Comment" does not count). This is how
   queued posts work before they have a link: family code word, power-cut scam, real-or-AI photo, PF missed call,
   ChatGPT codes, the WhatsApp carousel.
3. The caption's "Comment XYZ" keyword (capitals) -> the resource with that `keyword` (SAFE, CODE).

A post none of these cover keeps its comments **waiting** (Instagram allows a DM up to 7 days after a comment) and
opens one GitHub issue (an email). Add its entry, push, and the waiting comments are answered on the next run.

## Adding a new post

- Uses an existing resource (a SAFE reel, a CODE reel) and its caption has "Comment SAFE"/"Comment CODE": nothing to do.
- Gives a new resource (owner's yes first): add it under `resources` (`name`, `dm`; plus `keyword` and
  `public_replies` if the caption asks for a keyword) and a `posts` entry (`match` words from the caption hook, or the
  `shortcode` once it is live, `topic`, `resource`, optional `extra` bonus line). Keep `dm` under ~750 characters
  (Instagram caps a DM at 1000; the opener and closing line need room). Commit and push before the post goes live.
- After the post is live, adding its `shortcode` entry is the surest match.

## Files

- `autoreply.py`: the script (Python standard library only).
  `python autoreply.py --dry-run` reads the account and sends nothing; `python autoreply.py --selftest` prints
  replies for made-up comments (no account, safe to share).
- `rules.json`: resources, posts, text banks, writer settings, switch-over time. **Edit, commit, push** to change it.
- `.github/workflows/autoreply.yml`: the schedule.

## Running

- Each run checks every 60 s for ~4 minutes (`--loop 240`), then saves its state in the Actions cache.
- GitHub's timer (`*/5`) is best effort, so each run ends with a **backup trigger**: if no timed run happened in the
  last 12 minutes, it starts the next run itself.
- A keepalive commit on the 1st and 15th stops GitHub pausing the schedule after 60 days without activity.
- Run by hand: Actions tab > autoreply > Run workflow. Tick "Dry run" to read without sending, or "Self-test" to see
  sample replies the writer makes for made-up comments. Each run's log ends with a summary line (comments seen,
  eligible, sent, public only, spam skipped, waiting for a resource, writer vs banks, errors).
- `any_comment_since` (19 Nov 13:45 UTC): comments older than the switch-over are never touched, so nobody from before
  gets a late or second DM.

## Why there is no follow gate

Tested 19 Nov 2026 with a real comment: Meta lets this app (a personal developer app, not reviewed) send exactly **one
DM per comment**. The follow check (`is_user_follow_business`) fails with error 230 and any second message fails with
error 10 "outside of allowed window", even right after the person replies. A follow gate needs Meta app review
(Advanced Access for instagram_manage_messages + messaging webhooks). The gate code is still here: `follow_gate: true`
turns it back on once the app is approved.

## Privacy

The repo and its logs are public: logs print counts only, never usernames or comment text. The cached state holds
comment ids, SHA-256 hashes and the last few written lines with first names replaced by `{name}`. The writer model
(GitHub Models) sees the comment text and a first name, both already public on Instagram.

## Alerts and fixes

- One GitHub issue (email) if a DM fails, a post with comments has no resource, or a caption asks for an unknown keyword.
- Token: repository secret `IG_TOKEN` (Meta system-user token from the ig-graph setup; does not expire). To replace it,
  the owner runs in PowerShell:
  `(Get-Content "$HOME\.ig-graph\config.json" | ConvertFrom-Json).token | gh secret set IG_TOKEN --repo hemanthnagasai/ig-autoreply`
- Never run two senders at once (they keep separate state and would DM twice): ootto's auto-reply is OFF since 19 Nov,
  and the old laptop copy (Windows task "IG AutoReply SAFE-CODE", folder `E:/Instagram/autopilot/autoreply/`) is disabled.
- Instagram allows a private reply only within 7 days of a comment; older comments are skipped (`max_comment_age_days` 6).
