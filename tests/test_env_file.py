"""`.env` reader/writer used by the settings page."""

from __future__ import annotations

from media_ingestion.env_file import load_env, update_env


def test_update_creates_file(tmp_path):
    env = tmp_path / ".env"
    update_env(env, {"TOUMAI_LANGUAGES": '["fr"]', "TOUMAI_X": "1"})
    assert load_env(env) == {"TOUMAI_LANGUAGES": '["fr"]', "TOUMAI_X": "1"}


def test_update_preserves_comments_and_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# header\nOTHER=keep\nTOUMAI_X=old\n", encoding="utf-8")
    update_env(env, {"TOUMAI_X": "new", "TOUMAI_Y": "2"})
    text = env.read_text(encoding="utf-8")
    assert "# header" in text
    assert "OTHER=keep" in text
    assert "TOUMAI_X=new" in text and "TOUMAI_X=old" not in text
    assert "TOUMAI_Y=2" in text


def test_update_none_removes_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOUMAI_A=1\nTOUMAI_B=2\n", encoding="utf-8")
    update_env(env, {"TOUMAI_A": None})
    parsed = load_env(env)
    assert "TOUMAI_A" not in parsed and parsed["TOUMAI_B"] == "2"
