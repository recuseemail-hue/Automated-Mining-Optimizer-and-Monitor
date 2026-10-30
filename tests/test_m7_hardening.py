"""Services, config validation, secrets, task prompts, sysadmin helpers."""
from pathlib import Path

from agent import secrets as sec
from agent.claude_runner import TOOLS, TASKS_DIR
from agent.config import validate
from agent.services import footprint, plan, systemd_unit, task_xml


def test_service_definitions(cfg):
    p = plan(cfg)
    assert [x["name"] for x in p] == ["agent", "web"]
    xml = task_xml("agent", r"C:\rig\.venv\Scripts\rigctl.exe", "agent", r"C:\rig")
    for needle in ("<BootTrigger>", "S-1-5-18", "<RestartOnFailure>", "<Count>999</Count>", "<ExecutionTimeLimit>PT0S",
                   "<Priority>7</Priority>", "<Arguments>agent</Arguments>"):
        assert needle in xml
    u = systemd_unit(cfg, "web")
    assert "Restart=always" in u and "Nice=10" in u and "MemoryMax=512M" in u and u.strip().endswith("WantedBy=multi-user.target")
    cfg.data["host"]["role"] = "worker"
    assert [x["name"] for x in plan(cfg)] == ["agent"]


def test_config_validation(cfg):
    assert [e for e in validate(cfg) if not e.startswith("note:")] == []
    cfg.data["clamps"]["default"]["power_limit_pct"] = [30, 100]
    cfg.data["watchdog"]["core_c_crit"] = 70
    cfg.data["miner"]["coin"] = "DOGE"
    cfg.data["gpus"].append({"uuid": "GPU-SIM-0000"})
    errs = validate(cfg)
    assert any("floor below 40%" in e for e in errs) and any("core_c_max < watchdog" in e for e in errs)
    assert any("not in the coins catalog" in e for e in errs) and any("duplicate" in e for e in errs)


def test_secrets_file_store_and_redaction(cfg, tmp_path, monkeypatch):
    f = tmp_path / "secrets.env"
    sec.configure(f)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    where = sec.set_secret_store("NTFY_TOPIC", "topic-abcdef-123456")
    assert where == str(f) and (f.stat().st_mode & 0o777) == 0o600
    assert sec.get_secret("NTFY_TOPIC") == "topic-abcdef-123456"
    sec.set_secret_store("NTFY_TOPIC", "topic-zzz-999999")
    assert f.read_text().count("NTFY_TOPIC=") == 1 and sec.get_secret("NTFY_TOPIC") == "topic-zzz-999999"
    assert "topic-zzz-999999" not in sec.redact("token is topic-zzz-999999 ok")


def test_every_task_has_a_prompt_and_tools():
    for task in TOOLS:
        p = TASKS_DIR / f"{task}.md"
        assert p.is_file() and "## Output" in p.read_text(encoding="utf-8"), task
        assert any(t.startswith("Bash(rigctl") for t in TOOLS[task])
        assert not any(t.startswith("Bash(rigctl act ") for t in TOOLS["hourly"])      # hourly cannot call act directly


def test_sysadmin_log_change(cfg, tmp_path, monkeypatch):
    from agent import sysadmin
    doc = tmp_path / "system-changes.md"
    monkeypatch.setattr(sysadmin, "DOC", doc)
    line = sysadmin.log_change(cfg, "claude", "installed driver 566.03", "rollback via Device Manager")
    assert line.startswith("| ") and "installed driver 566.03" in doc.read_text()
    sysadmin.log_change(cfg, "user", "a | b", "c")
    assert "| a / b | c |" in doc.read_text() and doc.read_text().count("\n| 20") == 2


def test_footprint_runs():
    rows = footprint()
    assert isinstance(rows, list)
