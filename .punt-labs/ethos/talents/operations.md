# Operations

Release management, cross-repo propagation, developer tooling.

## Release Process

- Punt Labs release pipeline: preflight, bump, build, PR, tag, CI,
  GitHub release, post-release, propagation, verification
- Cross-repo coordination: marketplace, install-all.sh, website,
  punt-kit — each updated via PR with CI gates
- Version management: semver, changelog maintenance, install SHA
  tracking
- End-to-end verification: binary works, website updated, marketplace
  pointing to correct ref

## Developer Tooling

- Plugin development workflow: make dev/undev for cache symlinks
- Hook architecture: thin shell gates delegating to compiled handlers
- Two-channel display: compact panel summary + full context for models
- Quality gates: vet, staticcheck, shellcheck, markdownlint, tests
  with -race

## Automation

- Playbook execution (punt:auto) for repeatable multi-step processes
- Bead-driven work tracking across sessions
- Review cycle automation: local agents → PR → Copilot/Bugbot → fix →
  repeat until clean
- GPG commit signing, GitHub identity management, Vercel team access
