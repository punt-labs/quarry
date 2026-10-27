# Memory Types, Decay, and Lesson Preference

## Memory types (closed vocabulary)

`remember`'s `--memory-type` accepts exactly one of:

- **`fact`** — objective, verifiable information (a root cause, a ratified
  decision and its reason, a version pin, an address).
- **`observation`** — a neutral summary of an entity or system's state at a
  point in time.
- **`procedure`** — how-to knowledge: the steps for a repeatable task.
- **`opinion`** — a subjective assessment, held with some confidence, that
  you may want to revisit later.

`lesson` is a distinct type reserved for `learn` — never pass
`--memory-type lesson` to `remember`. An unrecognized value is rejected by
the daemon, not silently coerced.

## Decay (`remember`, not `learn`)

Ordinary memories decay with roughly a 30-day half-life: a fact remembered
a month ago ranks about half as strongly as the day it was written, for the
same query. This is deliberate — it lets corrections and updated gotchas
naturally outrank stale ones without deleting history, and it does not
apply to lessons, documents, or code, which do not decay.

## Why lessons outrank

A `learn`ed lesson carries a retrieval-preference boost over ordinary
documents and transcripts. The reasoning: a lesson is a distilled,
project-endorsed rule someone deliberately promoted above the noise of
raw transcripts and one-off facts — it should surface first when it's
relevant, the way a style guide should outrank a random commit message
that happens to mention the same words. The boost affects *ranking among
relevant results*, not relevance itself — an irrelevant lesson still loses
to a genuinely on-topic fact or document.

## Choosing `remember` vs. `learn`

Ask: "is this mine, or is this the project's?"

- Personal note-to-self, scoped to your own future recall → `remember`
  with your `agent_handle`.
- A rule anyone working on this project should follow → `learn`, unscoped,
  boosted.

When in doubt, `remember` first — a personal fact that turns out to matter
project-wide can always be promoted to a `learn` later; a `learn`ed lesson
that turns out to be personal opinion cannot be quietly un-boosted.
