import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from piothelper import checks, diagnose, fixes, knowledge, logs, portowner

LOCAL_TZ = timezone(timedelta(hours=5))


def _logcfg(cfg, path):
    cfg["paths"] = [{"name": "лог", "kind": "log", "path": str(path)}]
    cfg["recent_hours"] = 48
    return cfg


def _write(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _stamp(minutes_ago=0):
    return f"{datetime.now() - timedelta(minutes=minutes_ago):%Y-%m-%d %H:%M:%S}"


# ---------- смена ----------

def _shift_line(delta, state="opened"):
    exp = (datetime.now(LOCAL_TZ) + delta).isoformat(timespec="seconds")
    return '{"shiftStatus":{"expiredTime":"' + exp + '","number":7,"state":"' + state + '"}}'


@pytest.mark.parametrize("delta,state,level,needle", [
    (timedelta(hours=10), "opened", "ok", "до конца 24 ч"),
    (timedelta(minutes=30), "opened", "warn", "закончится"),
    (timedelta(hours=-3), "opened", "fail", "истекла"),
    (timedelta(hours=1), "closed", "info", "закрыта"),
    (timedelta(hours=1), "expired", "fail", "истекла"),
])
def test_shift_states(monkeypatch, cfg, tmp_path, delta, state, level, needle):
    monkeypatch.setattr(logs, "file_age_minutes", lambda p: 5)
    f = _write(tmp_path, "fptr10.log", ["шум", _shift_line(delta, state)])
    res = diagnose.check_shift(_logcfg(cfg, f))
    assert res[0].level == level and needle in res[0].title


def test_shift_uses_the_last_record(monkeypatch, cfg, tmp_path):
    monkeypatch.setattr(logs, "file_age_minutes", lambda p: 5)
    f = _write(tmp_path, "fptr10.log", [_shift_line(timedelta(hours=-3)), _shift_line(timedelta(hours=10))])
    assert diagnose.check_shift(_logcfg(cfg, f))[0].level == "ok"


def test_shift_not_judged_from_stale_log(monkeypatch, cfg, tmp_path):
    monkeypatch.setattr(logs, "file_age_minutes", lambda p: 600)
    f = _write(tmp_path, "fptr10.log", [_shift_line(timedelta(hours=-3))])
    assert diagnose.check_shift(_logcfg(cfg, f))[0].level == "info"


def test_shift_without_data_is_info(cfg, tmp_path):
    f = _write(tmp_path, "fptr10.log", ["ничего про смену"])
    assert diagnose.check_shift(_logcfg(cfg, f))[0].level == "info"


# ---------- правила в логах ----------

def _lic_line(days_left):
    until = date.today() + timedelta(days=days_left)
    return f"{_stamp()}\tU\tABCDE-12345-FGHIJ - Лицензия просрочена Для решения проблемы выдана последняя лицензия сроком до {until:%d.%m.%Y}."


@pytest.mark.parametrize("days,level", [(1, "fail"), (0, "fail"), (10, "warn")])
def test_license_grace_period_levels(cfg, tmp_path, days, level):
    f = _write(tmp_path, "lic.log", [_lic_line(days)])
    finding = next(x for x in diagnose.check_logs(_logcfg(cfg, f)) if "Лицензия просрочена" in x.title)
    assert finding.level == level and f"осталось дн.: {days}" in finding.title


def test_fresh_transient_error_is_active_old_one_is_resolved(cfg, tmp_path):
    fresh = _write(tmp_path, "a.log", [f"{_stamp(1)} ERR Ошибка 514: Не удалось получить ответ от ГИС МТ"])
    old = _write(tmp_path, "b.log", [f"{_stamp(180)} ERR Ошибка 514: Не удалось получить ответ от ГИС МТ"])
    assert diagnose.check_logs(_logcfg(cfg, fresh))[0].level == "fail"
    res = diagnose.check_logs(_logcfg(cfg, old))[0]
    assert res.level == "info" and "было" in res.title


def test_persistent_state_is_not_downgraded_by_age(cfg, tmp_path):
    f = _write(tmp_path, "lm.log", [f"{_stamp(300)} INF Статус ЛМ ЧЗ 2.0 получен: версия=2.6.1-7, статус=not_configured, режим=active"])
    assert diagnose.check_logs(_logcfg(cfg, f))[0].level == "warn"


def test_old_incident_outside_window_is_info(cfg, tmp_path):
    old = f"{datetime.now() - timedelta(hours=200):%Y-%m-%d %H:%M:%S} ERR Ошибка 12029: не удалось установить соединение"
    f = _write(tmp_path, "old.log", [old, f"{_stamp()} INF всё хорошо"])
    res = diagnose.check_logs(_logcfg(cfg, f))
    assert res[0].level == "info" and "было раньше" in res[0].title


def test_summary_counts_ignore_info_lines_with_error_words(cfg, tmp_path):
    f = _write(tmp_path, "g.log", [f'{{"level":"info","time":"{_stamp()}","msg":"error_description пуст, error: 0"}}'])
    level, text = checks.log_summary({"name": "g", "kind": "log", "path": str(f)}, cfg)
    assert level == "ok" and "ошибок 0" in text


# ---------- версия ЕСМ ----------

def _fake_ps(monkeypatch, version, exe):
    def fake(script, arg="", timeout=60):
        if "Win32_Service" in script:
            return json.dumps({"Name": "esm-orchestrator", "PathName": f'"{exe}" --service'})
        return version
    monkeypatch.setattr(fixes, "ps", fake)


@pytest.mark.parametrize("version,level", [("1.5.2.0", "warn"), ("1.6.4.0", "ok"), ("1, 7, 0, 3", "ok")])
def test_esm_version(monkeypatch, cfg, tmp_path, version, level):
    exe = tmp_path / "esm.exe"
    exe.write_text("x")
    _fake_ps(monkeypatch, version, exe)
    assert diagnose.check_esm_version(cfg)[0].level == level


def test_esm_version_absent_services(monkeypatch, cfg):
    monkeypatch.setattr(fixes, "ps", lambda *a, **k: "")
    assert diagnose.check_esm_version(cfg) == []


# ---------- внешние адреса ----------

def _probe_with(monkeypatch, unreachable=()):
    monkeypatch.setattr(diagnose, "_probe", lambda host, port, timeout=3.0: (False, "таймаут") if host in unreachable else (True, ""))


def test_external_all_reachable(monkeypatch, cfg):
    _probe_with(monkeypatch)
    assert {f.level for f in diagnose.check_external(cfg)} == {"ok"}


def test_external_needs_only_one_cdn_server(monkeypatch, cfg):
    _probe_with(monkeypatch, unreachable={f"cdn{n:02d}.crpt.ru" for n in range(1, 11)})
    res = {f.title.split(":")[0]: f.level for f in diagnose.check_external(cfg)}
    assert res["Серверы проверки кодов (ЦРПТ)"] == "info" and res["Регистрация ТС ПИоТ (ЦРПТ)"] == "ok"


def test_external_license_hosts_are_all_required(monkeypatch, cfg):
    _probe_with(monkeypatch, unreachable={"api.ao-esp.ru"})
    res = {f.title.split(":")[0]: f.level for f in diagnose.check_external(cfg)}
    assert res["Лицензирование ЕСП"] == "fail"


def test_external_total_outage_is_single_finding(monkeypatch, cfg):
    _probe_with(monkeypatch, unreachable={h for hosts in knowledge.EXTERNAL_HOSTS.values() for h, _ in hosts})
    res = diagnose.check_external(cfg)
    assert len(res) == 1 and res[0].level == "fail" and "интернета" in res[0].detail


# ---------- конфиг ЕСМ ----------

def test_esm_config_timeout_fix_makes_backup_and_preserves_bom(cfg, tmp_path):
    root = tmp_path / "ESP" / "ESM"
    root.mkdir(parents=True)
    f = root / "config_ЗН_ККТ.yml"
    f.write_bytes(b"\xef\xbb\xbf" + "server:\n  cdnCodesCheckTimeout: 1500\n  name: тест\n".encode("utf-8"))
    cfg["esm_config_roots"] = [str(tmp_path)]
    finding = diagnose.check_esm_config(cfg)[0]
    assert finding.level == "warn" and finding.fix is not None
    finding.fix.run()
    data = f.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf") and b"cdnCodesCheckTimeout: 2500" in data and "тест".encode() in data
    assert len(list(root.glob("*.bak-*"))) == 1
    assert diagnose.check_esm_config(cfg)[0].level == "ok"


# ---------- порты ----------

@pytest.mark.parametrize("text,expected", [("5555", ("", 5555)), ("192.168.1.50:5555", ("192.168.1.50", 5555))])
def test_parse_endpoint(text, expected):
    assert portowner.parse_endpoint(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "70000", "0", "300.1.1.1:80", "host:80"])
def test_parse_endpoint_rejects_garbage(bad):
    with pytest.raises(ValueError):
        portowner.parse_endpoint(bad)


def test_missing_com_port_state():
    assert portowner.com_state("COM9999") == "порта нет"


def test_handle_scanner_runs_without_error():
    res = portowner.find_com_owners(["NUL"])
    assert "error" not in res and res["scanned"] > 0 and "NUL" in res["owners"]
