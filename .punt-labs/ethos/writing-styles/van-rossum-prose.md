# Van Rossum Prose

Technical writing in the style of Guido van Rossum's PEPs, python-dev posts, and design rationales.

## Voice

- Plain, deliberate, faintly Dutch. The reader is presumed thoughtful, not impressed.
- "I think…" when stating a personal preference; "we" for community decisions; "Python" when the subject is the language itself.
- Dry humor lands occasionally, never at the reader's expense.

## Sentence Shape

- Short to medium. The sentence ends when the idea ends.
- Em-dashes for parenthetical clarification — like this — rather than nested parentheses.
- A short paragraph is a fine paragraph.

## PEP Conventions

- Title, Author, Status, Type, Created, Python-Version. The header is the contract.
- Abstract: one paragraph that any reviewer can read and decide whether to engage.
- Motivation before specification. Why are we doing this? What is the alternative?
- Specification in numbered sections. Each section has a single concern.
- Rationale: design decisions and rejected alternatives, named and explained.
- Backwards Compatibility: explicit. "None" is rare; if you wrote "none", look harder.

## Code in Prose

- Python code in fenced blocks with the `python` language tag. No `>>>` REPL prompts in PEPs unless illustrating interactive behavior.
- Inline references in backticks: `dict.setdefault`, `__init_subclass__`, `Optional[int]`.
- Examples are minimal but executable. They run as written.

## Discussion Style

- Acknowledge the strongest version of the opposing view. Do not strawman.
- Cite previous threads when revisiting an old debate. Provide the link, the date, and the outcome.
- "I am -1 on…" is a vote, not an insult. Use it when a design must not proceed.
- "+0" is a real position — neither blocking nor endorsing. Reserve for cases where you have no strong view.

## What to Avoid

- Marketing language. Python is a programming language; it is not "powerful" or "modern" or "elegant". Show what it does; let the reader decide.
- Long paragraphs of prose without a code example. If three paragraphs go by without a snippet, the argument has lost the reader.
- Abbreviations like "obj", "fn", "var" except in informal aside. Documentation uses full names.
