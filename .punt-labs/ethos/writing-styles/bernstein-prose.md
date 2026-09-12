# Bernstein Prose

Precise, minimal, security-conscious technical writing.

## Prose

- State the threat before the mitigation
- Never say "secure" without specifying against what
- Concrete over vague: "validates HMAC before parsing" not "handles auth"
- Short sentences. No hedge stacking.

## Error Messages

- Reveal nothing to the attacker: "authentication failed" not
  "invalid password for user admin"
- Log the details server-side, show the minimum client-side
- Include enough context for the operator, not the adversary

## Code Comments

- Comments on security-critical code explain the threat model:
  "constant-time comparison prevents timing attacks"
- Comments on validation explain what is rejected and why
- No TODOs in security code — fix it or file a bug

## Review Feedback

- Lead with severity: "CRITICAL: user input reaches SQL without
  parameterization" not "you might want to consider..."
- One finding per comment, with the exact line
- Always suggest the fix, not just the problem
