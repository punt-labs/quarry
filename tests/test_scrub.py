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


# ---------------------------------------------------------------------------
# PII: filesystem paths
# ---------------------------------------------------------------------------

_HOST = "Jims-MBP.local"


def _pii_scrub(text: str, **kw: object) -> tuple[str, dict[str, int]]:
    """Scrub with a fixed local hostname so results are machine-independent."""
    kw.setdefault("local_hostname", _HOST)
    return _scrub(text, **kw)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/Users/jfreeman/Coding/x", "~/Coding/x"),
        ("/home/alice/proj", "~/proj"),
        ("/Users/bob/", "~/"),
        ("/home/bob", "~"),
        ("path is /Users/jane/a and /home/kate/b", "path is ~/a and ~/b"),
    ],
)
def test_path_redaction_any_username(raw: str, expected: str) -> None:
    out, counts = _pii_scrub(raw)
    assert out == expected
    assert counts.get("path", 0) >= 1


def test_path_root_home_unchanged() -> None:
    """/root has no username to generalize — out of scope, left intact."""
    out, counts = _pii_scrub("logs under /root/x stay")
    assert out == "logs under /root/x stay"
    assert counts.get("path", 0) == 0


def test_path_case_sensitive_lowercase_users_unchanged() -> None:
    """Lowercase /users/ (e.g. inside a URL) must not be over-matched."""
    out, counts = _pii_scrub("https://site/users/list")
    assert out == "https://site/users/list"
    assert counts.get("path", 0) == 0


# ---------------------------------------------------------------------------
# PII: email addresses
# ---------------------------------------------------------------------------


def test_email_single_redacted() -> None:
    out, counts = _pii_scrub("reach me at jmf@pobox.com anytime")
    assert "jmf@pobox.com" not in out
    assert "[REDACTED:email]" in out
    assert counts.get("email", 0) == 1


def test_email_multiple_all_redacted() -> None:
    text = "a@b.co, c.d+tag@e-f.org, g_h@sub.example.io"
    out, counts = _pii_scrub(text)
    assert "@" not in out
    assert out.count("[REDACTED:email]") == 3
    assert counts.get("email", 0) == 3


@pytest.mark.parametrize(
    "raw,tail",
    [
        ("jmf@pobox.com.", "."),
        ("jmf@pobox.com. ", ". "),
        ("jmf@pobox.com...", "..."),
        ("jmf@pobox.com,", ","),
        ("jmf@pobox.com)", ")"),
        ("jmf@pobox.com", ""),
    ],
)
def test_email_redacted_before_trailing_punctuation(raw: str, tail: str) -> None:
    """A sentence-final address must redact — the trailing char is not part of it.

    Regression: the old ``(?![\\w.-])`` lookahead rejected a match when a period
    followed, leaking ``jmf@pobox.com.`` — the most common address context in prose.
    """
    out, counts = _pii_scrub(f"reach {raw} ok")
    assert "jmf@pobox.com" not in out
    assert out == f"reach [REDACTED:email]{tail} ok"
    assert counts.get("email", 0) == 1


def test_email_multi_label_tld_with_trailing_period() -> None:
    """A multi-label TLD still matches via backtracking, even with a trailing dot."""
    out, counts = _pii_scrub("write jmf@pobox.co.uk. now")
    assert "jmf@pobox.co.uk" not in out
    assert out == "write [REDACTED:email]. now"
    assert counts.get("email", 0) == 1


# ---------------------------------------------------------------------------
# PII: hostname (bounded scope — anti false positive)
# ---------------------------------------------------------------------------


def test_hostname_local_forms_redacted() -> None:
    out, counts = _pii_scrub(f"host {_HOST} and leaf Jims-MBP here")
    assert _HOST not in out
    assert "Jims-MBP" not in out
    assert out.count("[REDACTED:hostname]") == 2
    assert counts.get("hostname", 0) == 2


def test_hostname_bounded_does_not_touch_domains_or_modules() -> None:
    """A dotted token that is not the local host must survive untouched."""
    text = "see example.com and quarry.db.facade and github.com"
    out, counts = _pii_scrub(text)
    assert out == text
    assert counts.get("hostname", 0) == 0


def test_hostname_short_leaf_not_redacted_alone() -> None:
    """A <4 char leaf is not added as a form — it collides with common words."""
    # leaf "srv" (len 3) is guarded out; only the full dotted host is a form.
    out, counts = _pii_scrub("the srv is up", local_hostname="srv.corp.example.com")
    assert out == "the srv is up"
    assert counts.get("hostname", 0) == 0


def test_hostname_full_dotted_form_redacted() -> None:
    """The full hostname is always a form, even when its leaf is guarded out."""
    out, counts = _pii_scrub(
        "box srv.corp.example.com here", local_hostname="srv.corp.example.com"
    )
    assert "srv.corp.example.com" not in out
    assert "[REDACTED:hostname]" in out
    assert counts.get("hostname", 0) == 1


def test_hostname_case_insensitive_leaf_redacted() -> None:
    """DNS/mDNS is case-insensitive — a lowercased occurrence must still redact.

    Regression: without ``re.IGNORECASE`` a captured ``jims-macbook-pro`` leaked
    when the resolved host was ``Jims-MacBook-Pro.local``.
    """
    out, counts = _pii_scrub(
        "host jims-macbook-pro is up", local_hostname="Jims-MacBook-Pro.local"
    )
    assert "jims-macbook-pro" not in out
    assert "[REDACTED:hostname]" in out
    assert counts.get("hostname", 0) == 1


def test_email_precedes_hostname_no_local_part_leak() -> None:
    """A hostname inside an email domain is subsumed by whole-email redaction."""
    out, counts = _pii_scrub("login jim@Jims-MBP.local ok")
    assert out == "login [REDACTED:email] ok"
    assert "jim" not in out
    assert counts.get("email", 0) == 1
    assert counts.get("hostname", 0) == 0


# ---------------------------------------------------------------------------
# PII: security property, idempotency, corpus shape, toggles
# ---------------------------------------------------------------------------


def test_zero_pii_survives_mixed_fixture() -> None:
    """The security invariant: no path, email, or hostname leaks through."""
    text = f"path /Users/jfreeman/Coding/quarry\nmail jmf@pobox.com\nbox {_HOST}\n"
    out, counts = _pii_scrub(text)
    assert "/Users/" not in out
    assert "@" not in out
    assert _HOST not in out
    assert counts.get("path", 0) >= 1
    assert counts.get("email", 0) >= 1
    assert counts.get("hostname", 0) >= 1


def test_all_six_categories_fire_together() -> None:
    """Secrets + profanity are unweakened by the added PII passes."""
    text = (
        "GH=ghp_" + "A" * 40 + "\n"
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n"
        "damn this /Users/jane/x file\n"
        "mail jmf@pobox.com\n"
        f"host {_HOST}\n"
    )
    out, counts = _pii_scrub(text)
    expected = ("gh-pat", "aws-secret-key", "profanity", "path", "email", "hostname")
    for category in expected:
        assert counts.get(category, 0) >= 1, f"{category} did not fire"
    assert "/Users/" not in out
    assert "@" not in out


def test_pii_idempotent_over_five_categories() -> None:
    text = (
        "ghp_" + "C" * 40 + "\n"
        "/Users/jane/Coding/x\n"
        "jmf@pobox.com\n"
        f"{_HOST}\n"
        "this is damn loud\n"
    )
    once, _ = _pii_scrub(text)
    twice, twice_counts = _pii_scrub(once)
    assert twice == once, "PII scrubber not idempotent"
    assert sum(twice_counts.values()) == 0


def test_vox_corpus_paths_all_redacted() -> None:
    """598 /Users/jfreeman/ occurrences collapse to 0 paths and 598 ~/."""
    text = "/Users/jfreeman/repo/file.py\n" * 598
    out, counts = _pii_scrub(text)
    assert out.count("/Users/") == 0
    assert out.count("/home/") == 0
    assert out.count("~/") == 598
    assert counts.get("path", 0) == 598


def test_pii_disabled_keeps_everything() -> None:
    text = f"/Users/jane/x jmf@pobox.com {_HOST}"
    out, counts = _pii_scrub(text, scrub_pii=False)
    assert out == text
    assert counts.get("path", 0) == 0
    assert counts.get("email", 0) == 0
    assert counts.get("hostname", 0) == 0


def test_pii_empty_input_never_propagates() -> None:
    out, counts = _pii_scrub("")
    assert out == ""
    assert sum(counts.values()) == 0
