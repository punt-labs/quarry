"""Tests for quarry.scrub — secret and profanity redaction."""

from __future__ import annotations

import pytest

from quarry.scrub import ScrubConfig, scrub, scrub_and_log

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scrub(text: str, **kw: object) -> tuple[str, dict[str, int]]:
    """Run the scrubber with config overrides."""
    cfg = ScrubConfig(**kw)  # type: ignore[arg-type]
    return scrub(text, cfg)


# ---------------------------------------------------------------------------
# Secret patterns: positive + negative for each category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category,positive,negative",
    [
        (
            "gh-pat",
            "ghp_" + "A" * 40,
            "ghp_too_short",
        ),
        (
            "gh-pat",
            "ghs_" + "x" * 50,
            "ghx_" + "x" * 40,
        ),
        (
            "aws-access-key",
            "AKIA" + "ABCDEFGH12345678",
            "AKIA" + "abc",
        ),
        (
            "anthropic-key",
            "sk-ant-api03-" + "a" * 40,
            "sk-ant-",
        ),
        (
            "openai-key",
            "sk-" + "p" * 48,
            "sk-short",
        ),
        (
            "bearer",
            "Authorization: Bearer " + "x" * 30,
            "Bearer x",
        ),
        (
            "jwt",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEFghiJKLmnoPQRstuVWX",
            "eyJhbGc",
        ),
        (
            "slack-token",
            "xoxb-1234567890-abcdef0123",
            "xoxz-not-a-slack-token-prefix",
        ),
    ],
)
def test_secret_categories(
    category: str,
    positive: str,
    negative: str,
) -> None:
    out_pos, counts_pos = _scrub(f"prefix {positive} suffix")
    assert f"[REDACTED:{category}]" in out_pos
    assert positive not in out_pos
    assert counts_pos.get(category, 0) >= 1

    out_neg, _counts_neg = _scrub(f"prefix {negative} suffix")
    assert f"[REDACTED:{category}]" not in out_neg
    assert negative in out_neg


def test_anthropic_does_not_clash_with_openai() -> None:
    """sk-ant-... is classified as anthropic, not openai."""
    text = "key: sk-ant-api03-" + "z" * 40
    out, counts = _scrub(text)
    assert "[REDACTED:anthropic-key]" in out
    assert counts.get("openai-key", 0) == 0


def test_openai_modern_project_key_format() -> None:
    """Modern OpenAI project keys (sk-proj-...) include hyphens."""
    key = "sk-proj-Abcdef-1234567890_HIJKLMN-OPQR_STUVwxyz0123abcd"
    text = f"OPENAI_KEY={key}"
    out, counts = _scrub(text)
    assert key not in out
    assert counts.get("openai-key", 0) >= 1
    assert "[REDACTED:openai-key]" in out


def test_aws_secret_only_when_labeled() -> None:
    """40-char base64 strings only redact when the line names AWS."""
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # noqa: S105
    labeled = f"aws_secret_access_key={secret}"
    out_labeled, counts_labeled = _scrub(labeled)
    assert "[REDACTED:" in out_labeled
    assert secret not in out_labeled
    assert (
        counts_labeled.get("aws-secret-key", 0) + counts_labeled.get("env-secret", 0)
    ) >= 1

    unlabeled = f"hash: {secret}"
    out_unlabeled, _ = _scrub(unlabeled)
    assert "[REDACTED:aws-secret-key]" not in out_unlabeled
    assert secret in out_unlabeled


# ---------------------------------------------------------------------------
# PEM / GPG block redaction
# ---------------------------------------------------------------------------


def test_pem_private_key_block_redacted_whole() -> None:
    block = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAtw3D4tLdF9q8\n"
        "n5XYZmoreBase64HereOverManyLines\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after\n"
    )
    out, counts = _scrub(block)
    assert "[REDACTED:pem-private-key]" in out
    assert "MIIEpAIBAAKCAQEAtw3D4tLdF9q8" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "before" in out
    assert "after" in out
    assert counts.get("pem-private-key", 0) == 1


def test_pem_block_variants() -> None:
    for label in (
        "PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "EC PRIVATE KEY",
    ):
        block = f"-----BEGIN {label}-----\nAAAA-base64-body-AAAA\n-----END {label}-----"
        out, _ = _scrub(block)
        assert "[REDACTED:pem-private-key]" in out, f"failed for {label!r}"
        assert "AAAA-base64-body-AAAA" not in out


def test_gpg_private_key_block_redacted_whole() -> None:
    block = (
        "header\n"
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n"
        "Version: GnuPG v2\n"
        "lQOYBGZ12345base64body\n"
        "-----END PGP PRIVATE KEY BLOCK-----\n"
        "footer\n"
    )
    out, counts = _scrub(block)
    assert "[REDACTED:gpg-private-key]" in out
    assert "lQOYBGZ12345base64body" not in out
    assert "header" in out
    assert "footer" in out
    assert counts.get("gpg-private-key", 0) == 1


# ---------------------------------------------------------------------------
# env-secret
# ---------------------------------------------------------------------------


def test_env_secret_keeps_variable_name() -> None:
    cases = [
        "GH_TOKEN=ghp_realtokenvaluehere1234567890abcdef",
        "export API_SECRET=supersecret123456",
        "MY_PASSWORD='quotedvalue123'",
        'MY_PASSPHRASE="another secret"',
    ]
    for line in cases:
        out, counts = _scrub(line)
        assert "[REDACTED:" in out, f"no redaction in {line!r}"
        for visible in (
            "GH_TOKEN",
            "API_SECRET",
            "MY_PASSWORD",
            "MY_PASSPHRASE",
        ):
            if visible in line:
                assert visible in out, f"{visible} stripped from {line!r}"
        for hidden in (
            "ghp_realtokenvaluehere1234567890abcdef",
            "supersecret123456",
            "quotedvalue123",
            "another secret",
        ):
            if hidden in line:
                assert hidden not in out, f"{hidden!r} leaked from {line!r}"
        assert (counts.get("env-secret", 0) + counts.get("gh-pat", 0)) >= 1


def test_env_secret_does_not_match_unrelated_assignments() -> None:
    text = "COUNT=42\nPATH=/usr/bin\nNAME=alice\n"
    out, counts = _scrub(text)
    assert out == text
    assert counts.get("env-secret", 0) == 0


def test_env_secret_command_substitution_redacted() -> None:
    line = (
        "export GH_TOKEN=$(security find-generic-password"
        ' -a "jfreeman" -s "GITHUB_CLAUDE_PAT" -w)'
    )
    out, counts = _scrub(line)
    assert "[REDACTED:env-secret]" in out
    assert "security find-generic-password" not in out
    assert out.startswith("export GH_TOKEN=")
    assert counts.get("env-secret", 0) >= 1


@pytest.mark.parametrize(
    "label,text",
    [
        (
            "export-on-prior-line",
            "some text export\nMY_TOKEN_NAME\nbar=baz\n",
        ),
        (
            "name-then-newline-then-equals",
            "MY_TOKEN\n=value\n",
        ),
        (
            "equals-then-newline-then-value",
            "MY_TOKEN=\nplain text on next line\n",
        ),
    ],
)
def test_env_secret_does_not_span_newlines(
    label: str,
    text: str,
) -> None:
    out, counts = _scrub(text)
    assert out == text, f"{label}: scrubber rewrote text it should not have"
    assert counts.get("env-secret", 0) == 0, f"{label}: spurious env-secret hit"


def test_env_secret_redacts_crlf_lines() -> None:
    text = "GH_TOKEN=ghp_realtokenvaluehere1234567890abcdef\r\nNEXT=line\r\n"
    out, counts = _scrub(text)
    assert "ghp_realtokenvaluehere1234567890abcdef" not in out
    assert (counts.get("env-secret", 0) + counts.get("gh-pat", 0)) >= 1
    assert out.count("\r\n") == text.count("\r\n")
    assert out.count("\n") == out.count("\r\n")
    assert "NEXT=line" in out


def test_git_config_key_n_not_treated_as_secret() -> None:
    text = (
        "export GIT_CONFIG_KEY_0=commit.gpgsign\n"
        "export GIT_CONFIG_VALUE_0=true\n"
        "export GIT_CONFIG_KEY_1=user.signingkey\n"
    )
    out, counts = _scrub(text)
    assert out == text
    assert counts.get("env-secret", 0) == 0


# ---------------------------------------------------------------------------
# Profanity
# ---------------------------------------------------------------------------


def test_profanity_basic_replacement() -> None:
    out, counts = _scrub("this is damn annoying")
    assert "damn" not in out
    assert "[REDACTED:profanity]" in out
    assert counts.get("profanity", 0) >= 1


def test_profanity_word_boundary_safe() -> None:
    safe_tokens = [
        "class",
        "passing",
        "assist",
        "embassy",
        "harassed",
        "brass",
    ]
    for token in safe_tokens:
        out, counts = _scrub(f"the {token} is fine")
        assert token in out, f"{token} was incorrectly redacted"
        assert counts.get("profanity", 0) == 0, f"{token} bumped profanity count"


def test_profanity_case_insensitive() -> None:
    out, counts = _scrub("DAMN it all")
    assert "DAMN" not in out
    assert counts.get("profanity", 0) >= 1


def test_profanity_disabled_keeps_word() -> None:
    out, counts = _scrub(
        "this is damn annoying",
        scrub_profanity=False,
    )
    assert "damn" in out
    assert counts.get("profanity", 0) == 0


# ---------------------------------------------------------------------------
# Flag toggles and idempotence
# ---------------------------------------------------------------------------


def test_no_secrets_flag_disables_secret_scrubbing() -> None:
    text = "key: ghp_" + "B" * 40
    out, counts = _scrub(text, scrub_secrets=False)
    assert "ghp_" + "B" * 40 in out
    assert counts.get("gh-pat", 0) == 0


def test_idempotent_double_scrub() -> None:
    """Running the scrubber twice produces the same output."""
    text = (
        "GH_TOKEN=ghp_" + "C" * 40 + "\n"
        "AWS access: AKIAABCDEFGH12345678\n"
        "JWT: eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxIn0."
        "signature1234567890abc\n"
        "this is damn loud\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "body\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    once, _ = _scrub(text)
    twice, twice_counts = _scrub(once)
    assert twice == once, "scrubber not idempotent"
    assert sum(twice_counts.values()) == 0


def test_redaction_marker_not_re_redacted() -> None:
    text = "the value [REDACTED:gh-pat] was already scrubbed"
    out, counts = _scrub(text)
    assert out == text
    assert sum(counts.values()) == 0


def test_empty_input() -> None:
    out, counts = _scrub("")
    assert out == ""
    assert sum(counts.values()) == 0


def test_default_config_when_none() -> None:
    """scrub() with config=None uses defaults."""
    text = "key: ghp_" + "D" * 40
    out, counts = scrub(text, None)
    assert "[REDACTED:gh-pat]" in out
    assert counts.get("gh-pat", 0) >= 1


# ---------------------------------------------------------------------------
# scrub_and_log integration helper
# ---------------------------------------------------------------------------


def test_scrub_and_log_returns_scrubbed_text() -> None:
    secret = "ghp_" + "E" * 40
    result = scrub_and_log(f"token: {secret}", "test")
    assert "[REDACTED:gh-pat]" in result
    assert secret not in result


def test_scrub_and_log_no_redactions_no_log(caplog: pytest.LogCaptureFixture) -> None:
    result = scrub_and_log("clean text", "test")
    assert result == "clean text"
    assert "scrubbed" not in caplog.text


def test_scrub_and_log_logs_counts(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="quarry.scrub"):
        scrub_and_log("damn ghp_" + "F" * 40, "pre-compact")
    assert "pre-compact: scrubbed capture file" in caplog.text
    assert "gh-pat" in caplog.text
    assert "profanity" in caplog.text


# ---------------------------------------------------------------------------
# Markdown structure preservation
# ---------------------------------------------------------------------------


def test_markdown_structure_preserved() -> None:
    text = (
        "# Heading\n"
        "\n"
        "Some prose with no secrets.\n"
        "\n"
        "## Subheading\n"
        "\n"
        "```python\n"
        "def hello() -> None:\n"
        "    print('hi')\n"
        "```\n"
        "\n"
        "- bullet one\n"
        "- bullet two\n"
    )
    out, counts = _scrub(text)
    assert out == text
    assert sum(counts.values()) == 0


def test_secret_in_fenced_code_block_still_redacted() -> None:
    secret = "ghp_" + "I" * 40
    text = f"```\nexport TOKEN={secret}\n```\n"
    out, _ = _scrub(text)
    assert secret not in out
    assert "[REDACTED:" in out
