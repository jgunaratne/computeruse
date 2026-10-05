"""Security-key forwarding detection for corporate SSO inside the sandboxed Chrome."""

from computeruse.computer.remote import chrome_command
from computeruse.computer.sso import CRD_HOST_SERVICES_SOCKET, security_key_forwarding


def test_remote_desktop_session_is_preferred_when_its_socket_exists(tmp_path):
    (tmp_path / CRD_HOST_SERVICES_SOCKET).touch()
    agent = tmp_path / "agent.sock"
    agent.touch()
    env = {"CHROME_REMOTE_DESKTOP_SESSION": "1", "XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": str(agent)}
    info = security_key_forwarding(env)
    assert info["transport"] == "remote-desktop" and info["available"] is True
    assert info["crd_session"] is True and info["ssh_auth_sock"] == str(agent)
    assert "Remote Desktop" in info["detail"]


def test_remote_desktop_requires_both_the_variable_and_the_socket(tmp_path):
    env = {"CHROME_REMOTE_DESKTOP_SESSION": "1", "XDG_RUNTIME_DIR": str(tmp_path)}  # no socket
    assert security_key_forwarding(env)["transport"] is None
    (tmp_path / CRD_HOST_SERVICES_SOCKET).touch()
    assert security_key_forwarding({"XDG_RUNTIME_DIR": str(tmp_path)})["transport"] is None  # no variable
    assert security_key_forwarding({**env, "CHROME_REMOTE_DESKTOP_SESSION": "0"})["transport"] is None
    assert security_key_forwarding(env)["transport"] == "remote-desktop"


def test_ssh_agent_fallback_and_nothing(tmp_path):
    agent = tmp_path / "S.sk-agent"
    agent.touch()
    info = security_key_forwarding({"SSH_AUTH_SOCK": str(agent)})
    assert info["transport"] == "ssh-agent" and info["ssh_auth_sock"] == str(agent) and info["crd_session"] is False
    assert str(agent) in info["detail"]

    none = security_key_forwarding({"SSH_AUTH_SOCK": str(tmp_path / "gone")})
    assert none["transport"] is None and none["available"] is False and none["ssh_auth_sock"] is None
    assert "security code" in none["detail"]
    assert security_key_forwarding({})["available"] is False


def test_chrome_keeps_its_extension_updater_for_policy_installed_extensions(tmp_path):
    """Enterprise policy delivers the security-key forwarding / device-trust extensions through the
    extension updater, which `--disable-background-networking` would switch off."""
    cmd = chrome_command("/usr/bin/google-chrome", (1280, 800), str(tmp_path))
    assert "--disable-background-networking" not in cmd
    assert f"--user-data-dir={tmp_path}" in cmd and "--disable-sync" in cmd


def test_backend_listing_and_doctor_report_security_key_forwarding(settings, monkeypatch, capsys):
    from computeruse.cli import cmd_doctor
    from computeruse.computer import registry as reg

    monkeypatch.setattr(reg, "find_chrome", lambda _binary: "/usr/bin/true")
    for var in ("CHROME_REMOTE_DESKTOP_SESSION", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(var, raising=False)
    sk = reg.BackendRegistry(settings).info("browser").options["security_key"]
    assert sk["available"] is False and sk["transport"] is None

    class Args:
        data_dir = str(settings.data_dir)
        verbose = False

    monkeypatch.setattr("computeruse.cli._settings", lambda _args: settings)
    cmd_doctor(Args())
    out = capsys.readouterr().out
    assert "security keys" in out and "No security-key forwarding detected" in out
