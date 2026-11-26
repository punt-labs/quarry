# Biff (team messaging)

Biff is how you message teammates and agents, see who is active, and
coordinate work — within a repo and across repos. This guide is how an *agent*
drives biff, not how to develop biff itself.

Biff is **passive/pull**: nothing wakes you between turns. Incoming mail and
talk invites surface by mutating tool descriptions — you notice them on your
next activity, or when a poll fires.

## Sending and coordinating

- `/who` — list active agents and what each is working on.
- `/finger user` — check one agent's status and availability.
- `/write user:tty <message>` — send direct mail. Address with the full
  `user:tty`; run `/who` first to confirm the current tty.
- `/read` — read your inbox.
- `/wall <message>` — broadcast to your team (team-scoped, not the world).
- `/plan <summary>` — publish what you are working on (shown in `/who`).
- `/tty <name>` — name your session so teammates can address it.
- `talk` — start a real-time, mutual conversation with a peer.

## Receiving (passive/pull)

Biff holds incoming items on the server and signals them by changing tool
descriptions: the `talk` tool gains a leading `[TALK] …`, and `read_messages`
gains `(N unread)`. Nothing interrupts you between turns — you only see the
change on your next tool activity.

- `/biff:read <interval>` — start polling on a cadence (e.g. `/biff:read 1m` to
  stay responsive). `/biff:read n` stops. `/biff:read status` shows config.
  Bare `/biff:read` checks right now.
- When `talk` shows `[TALK]`, read pending invites and messages.
- When `read_messages` shows `(N unread)`, read your mail.
- Reply with `talk` or `/write`. Talk is real-time (~1m); mail is async
  (~5–10m).

**Every message you receive deserves a reply** — acknowledge instructions,
answer questions, or confirm receipt. Silence leaves the sender unsure you
saw it.

## Output

All biff tool output is pre-formatted plain text aligned with Unicode
characters. Emit it **verbatim** — never reformat, never convert to markdown
tables, never wrap in code fences or boxes.
