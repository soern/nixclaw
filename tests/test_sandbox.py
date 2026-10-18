"""sandbox.wrap(): privilege drop + limits only when running as root."""

from unittest.mock import patch

from src.nixclaw_agent import sandbox


def test_unwrapped_when_not_root():
    with patch("os.geteuid", return_value=1000):
        assert sandbox.wrap(["ffmpeg", "-i", "fd:"]) == ["ffmpeg", "-i", "fd:"]


def test_wrapped_as_root():
    with patch("os.geteuid", return_value=0), patch("shutil.which", return_value="/usr/bin/x"):
        argv = sandbox.wrap(["ffmpeg", "-i", "fd:"])
    assert argv[0] == "setpriv"
    assert f"--reuid={sandbox.SANDBOX_UID}" in argv
    assert "--no-new-privs" in argv and "--clear-groups" in argv
    assert "prlimit" in argv
    assert argv[-4:] == ["--", "ffmpeg", "-i", "fd:"]


def test_root_without_tools_runs_unwrapped():
    with patch("os.geteuid", return_value=0), patch("shutil.which", return_value=None):
        assert sandbox.wrap(["ffprobe"]) == ["ffprobe"]


def test_env_has_no_secrets():
    with patch.dict("os.environ", {"SOME_API_KEY": "secret"}):
        assert "SOME_API_KEY" not in sandbox.env()
