from scripts.dogfood_fixture import redact_token


def test_redact_token_is_case_insensitive_and_preserves_prefix_casing():
    assert redact_token("token=abc123") == "token=[redacted]"
    assert redact_token("Token=abc123") == "Token=[redacted]"
    assert redact_token("TOKEN=abc123") == "TOKEN=[redacted]"
    assert redact_token("ToKeN=abc123") == "ToKeN=[redacted]"
