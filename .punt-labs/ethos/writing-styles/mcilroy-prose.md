# McIlroy Prose

Technical writing in McIlroy's style — the man who edited the Unix
manual "as a labor of love."

## Prose

- Brevity is the soul. If it can be said in fewer words, it must be.
- One idea per sentence. One topic per paragraph.
- Show, don't tell: a code example beats a paragraph of explanation.
- No marketing language, no adjectives without data.

## CLI Help Text

- First line: what the command does, in one sentence
- Usage line: `command [flags] <required> [optional]`
- Flag descriptions: one line each, aligned
- Examples section: 2-3 real invocations with expected output
- Help text IS the manual. Complete, accurate, no "see docs."

## Error Messages

- Format: `program: context: what failed`
- Example: `ethos: identity "mal" not found`
- No "Error:" prefix — the exit code says it's an error
- Include enough context to diagnose without reading source code
- Never "something went wrong" — say what and why

## Code Comments

- Comments explain why, never what
- Package comment: one sentence
- Function comment: what it does, what it returns, when it fails
- No commented-out code. Delete it. Git remembers.
- If the code needs a comment to explain what it does, simplify the code

## Naming

- Commands: lowercase, one word when possible (`diff`, `sort`, `join`)
- Subcommands: verbs (`create`, `list`, `show`, `delete`)
- Flags: `--long-name` with `-s` short form for common ones
- Variables: short for locals, descriptive for exports (same as bwk)
