"""config.yaml problems must produce a readable message, never a crash dump."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main_window import ConfigError, load_config  # noqa: E402

GOOD = 'ado:\n  organization: "o"\n  project: "p"\n  pat: "%s"\n'


def _write(tmp_path, text, encoding="utf-8"):
    path = tmp_path / "config.yaml"
    path.write_bytes(text.encode(encoding))
    return path


def test_valid_config(tmp_path):
    assert load_config(_write(tmp_path, GOOD % "abc"))["ado"]["pat"] == "abc"


def test_notepad_bom_is_tolerated(tmp_path):
    assert load_config(_write(tmp_path, GOOD % "abc", "utf-8-sig"))["ado"]["organization"] == "o"


def test_missing_and_empty_file(tmp_path):
    assert load_config(tmp_path / "nope.yaml") == {}
    assert load_config(_write(tmp_path, "")) == {}


def test_token_pasted_after_closing_quotes_is_explained_without_leaking_it(tmp_path):
    secret = "q7x2k9secretpatvalue4812"
    path = _write(tmp_path, f'ado:\n  organization: "o"\n  project: "p"\n  pat: ""      {secret}\n')
    with pytest.raises(ConfigError) as err:
        load_config(path)
    message = str(err.value)
    assert "line 4" in message and "BETWEEN the quotes" in message and "ADO_PAT" in message
    assert secret not in message


def test_tab_and_bad_backslash_paths_are_reported(tmp_path):
    with pytest.raises(ConfigError, match="formatting mistake"):
        load_config(_write(tmp_path, "app:\n\tdefault_executable: x\n"))
    with pytest.raises(ConfigError, match="formatting mistake"):
        load_config(_write(tmp_path, 'app:\n  default_executable: "C:\\Users\\qa\\a.exe"\n'))


def test_not_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="must contain settings"):
        load_config(_write(tmp_path, "- just\n- a list\n"))


def test_single_quoted_windows_path_keeps_backslashes(tmp_path):
    cfg = load_config(_write(tmp_path, "app:\n  default_executable: 'C:\\Program Files\\App\\app.exe'\n"))
    assert cfg["app"]["default_executable"] == "C:\\Program Files\\App\\app.exe"
