"""The application under test must not inherit the dashboard runtime's own environment variables."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_as  # noqa: E402

BUNDLE = os.path.join(os.sep, "opt", "dash")
INSIDE = os.path.join(BUNDLE, "_internal")


def _env(**extra):
    base = {
        "USERNAME": "qa",
        "PATH": os.pathsep.join([os.path.join(os.sep, "usr", "bin"), INSIDE, os.path.join(os.sep, "opt", "tools")]),
        "TEMP": os.path.join(os.sep, "tmp"),
        # exactly the six variables seen on a real machine that the dashboard added
        "QT_PLUGIN_PATH": os.path.join(INSIDE, "PyQt6", "Qt6", "plugins"),
        "QML2_IMPORT_PATH": "qml",
        "_PYI_ARCHIVE_FILE": "x",
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "DLIB_NO_VF_DEFAULTS": "1",
        "DLIB_SILENCE": "1",
    }
    base.update(extra)
    return base


def test_runtime_variables_are_removed_and_normal_ones_kept():
    env, removed = run_as.clean_environment(_env(ADO_PAT="secret"), own_dirs=[BUNDLE])
    for name in ("QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
                 "DLIB_NO_VF_DEFAULTS", "DLIB_SILENCE", "ADO_PAT"):
        assert name not in env and name in removed
    assert env["USERNAME"] == "qa" and env["TEMP"] == os.path.join(os.sep, "tmp")


def test_path_entries_inside_the_dashboard_folder_are_dropped():
    env, removed = run_as.clean_environment(_env(), own_dirs=[BUNDLE])
    assert INSIDE not in env["PATH"].split(os.pathsep)
    assert os.path.join(os.sep, "opt", "tools") in env["PATH"].split(os.pathsep)
    assert any(r.startswith("PATH") for r in removed)


def test_a_variable_pointing_into_the_dashboard_folder_is_dropped_but_others_are_kept():
    env, _ = run_as.clean_environment(_env(MYCFG=os.path.join(INSIDE, "cfg"), QT_SCALE_FACTOR="1.5"), own_dirs=[BUNDLE])
    assert "MYCFG" not in env
    assert env["QT_SCALE_FACTOR"] == "1.5"  # a user's own Qt setting is not ours to remove


def test_config_can_remove_more_and_set_variables():
    env, removed = run_as.clean_environment(_env(MAS_DEBUG="1", MAS_X="2"), remove=["mas_*"], set_vars={"MAS_MODE": "test"},
                                            own_dirs=[BUNDLE])
    assert "MAS_DEBUG" not in env and "MAS_X" not in env and env["MAS_MODE"] == "test"
    assert "MAS_DEBUG" in removed


def test_the_real_environment_is_left_untouched():
    before = dict(os.environ)
    run_as.clean_environment(own_dirs=[BUNDLE])
    assert dict(os.environ) == before
