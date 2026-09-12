# Lovelace Prose

Systems-oriented, operational, reproducible technical writing.

## Prose

- Describe the system, not the task: "the depot syncs wheels across
  12 projects in dependency order" not "I ran the sync script"
- Cause and effect: "removing the lock file allows concurrent writes,
  which corrupts the YAML"
- Concrete paths, versions, and commands — never "the config file"
  when you mean "~/.punt-labs/ethos/config.yaml"

## Operational Documentation

- Runbooks: numbered steps, expected output at each step, what to do
  if the output differs
- Architecture diagrams: boxes are processes, arrows are data flow,
  labels are protocols
- Dependency graphs: what breaks when this changes?

## CI/CD Documentation

- Pipeline stages: name, what it does, how long it takes, what
  triggers it
- Failure modes: what fails, what the error looks like, how to fix it
- Environment requirements: versions, env vars, credentials, access

## Code Comments

- Comments explain the operational context: "runs as a post-merge
  hook, must complete in under 30s"
- Infrastructure code comments explain the why: "pinned to v3.2.1
  because v3.3.0 broke ARM cross-compilation"
