"""Просроченный товар: сканирования кодов с истёкшим сроком (по логам ЕСМ), версия Frontol, напоминание о правах кассиров."""
import json
from datetime import date, datetime, timedelta

import pytest

from piothelper import config, diagnose, fixes, knowledge


def _ts(minutes_ago=0):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.123")


def _resp(expire, minutes_ago=1, codes=1):
    """Ответ проверки кода так, как его пишет ЕСМ: один и тот же ответ в логе дважды (HTTP-тело и ответ v1)."""
    items = ",".join(f'{{"cis":"01046000000000002100000{i}","expireDate":"{expire}T00:00:00.000Z","found":true}}' for i in range(codes))
    return [f"{_ts(minutes_ago)} DBG Получен HTTP-ответ. Тело {{\"code\":0,\"codes\":[{items}]}}",
            f"{_ts(minutes_ago)} DBG Ответ v1/codes/check response={{\"codesResponse\":[{items}]}}"]


def _cfg(cfg, tmp_path, lines):
    f = tmp_path / "esm-cm.log"
    f.write_text("\n".join(lines), encoding="utf-8")
    cfg["paths"] = [{"name": "esm", "kind": "log", "path": str(f)}]
    return cfg


def _days(n):
    return (date.today() + timedelta(days=n)).isoformat()


def test_expired_code_is_reported_without_printing_the_code(cfg, tmp_path):
    res = diagnose.check_expired_goods(_cfg(cfg, tmp_path, _resp(_days(-3))))
    assert res[0].level == "warn" and "1 за 24 ч" in res[0].title
    assert "0104600000" not in res[0].title + res[0].detail          # сам код маркировки наружу не попадает
    assert "Это не значит, что товар продан" in res[0].detail


def test_valid_codes_are_ok(cfg, tmp_path):
    res = diagnose.check_expired_goods(_cfg(cfg, tmp_path, _resp(_days(200))))
    assert res[0].level == "ok"


def test_duplicate_log_lines_count_once_and_distinct_checks_count_separately(cfg, tmp_path):
    lines = _resp(_days(-1), minutes_ago=5) + _resp(_days(-2), minutes_ago=2) + _resp(_days(100), minutes_ago=1)
    res = diagnose.check_expired_goods(_cfg(cfg, tmp_path, lines))
    assert res[0].level == "warn" and "2 за 24 ч" in res[0].title


def test_expiry_on_todays_date_is_still_valid(cfg, tmp_path):
    assert diagnose.check_expired_goods(_cfg(cfg, tmp_path, _resp(_days(0))))[0].level == "ok"


def test_old_checks_outside_the_window_are_ignored(cfg, tmp_path):
    lines = _resp(_days(-10), minutes_ago=60 * 30)  # 30 часов назад при окне 24 ч
    assert diagnose.check_expired_goods(_cfg(cfg, tmp_path, lines))[0].level == "info"


def test_no_data_is_info(cfg, tmp_path):
    assert diagnose.check_expired_goods(_cfg(cfg, tmp_path, [f"{_ts()} INF ничего про коды"]))[0].level == "info"


def test_garbage_dates_do_not_crash(cfg, tmp_path):
    lines = [f'{_ts()} DBG {{"cis":"x","expireDate":"2026-13-45"}}']
    assert diagnose.check_expired_goods(_cfg(cfg, tmp_path, lines))[0].level == "info"


# ---------- права кассиров ----------

def test_rights_reminder_names_the_exact_rights_and_path(cfg):
    f = diagnose.check_frontol_rights(cfg)[0]
    assert f.level == "info" and f.fix is not None and not f.fix.needs_admin
    for right in knowledge.FRONTOL_RIGHTS:
        assert right in f.detail
    assert "Системные справочники" in f.detail and "Пользователи" in f.detail
    assert "не читает и не меняет базу Frontol" in f.detail


def test_confirming_rights_silences_the_reminder_and_persists(cfg):
    finding = diagnose.check_frontol_rights(cfg)[0]
    assert "Отмечено" in finding.fix.run()
    assert cfg["frontol_rights_confirmed"] == date.today().isoformat()
    assert config.load()["frontol_rights_confirmed"] == date.today().isoformat()   # сохранилось на диск
    again = diagnose.check_frontol_rights(cfg)[0]
    assert again.level == "ok" and again.fix is None


def test_reminder_returns_after_the_review_period(cfg):
    cfg["frontol_rights_confirmed"] = (date.today() - timedelta(days=cfg["rights_review_days"] + 1)).isoformat()
    assert diagnose.check_frontol_rights(cfg)[0].level == "info"


@pytest.mark.parametrize("bad", ["", "вчера", "2026-99-99"])
def test_broken_confirmation_date_is_treated_as_not_confirmed(cfg, bad):
    cfg["frontol_rights_confirmed"] = bad
    assert diagnose.check_frontol_rights(cfg)[0].level == "info"


# ---------- версия Frontol ----------

def _frontol_cfg(cfg, tmp_path, monkeypatch, version):
    (tmp_path / "frontol.exe").write_bytes(b"MZ" + b"\0" * 200)
    cfg["paths"] = [{"name": "Папка Frontol", "kind": "dir", "path": str(tmp_path)}]
    monkeypatch.setattr(fixes, "ps", lambda *a, **k: version)
    monkeypatch.setattr(diagnose, "pe_machine", lambda p: "x86")
    return cfg


@pytest.mark.parametrize("version,warns", [("6.28.8.1", True), ("6.29.1.0", False), ("6.30.0.5", False)])
def test_frontol_expiry_protection_version(cfg, tmp_path, monkeypatch, version, warns):
    res = diagnose.check_versions(_frontol_cfg(cfg, tmp_path, monkeypatch, version))
    hits = [f for f in res if "просроченного маркированного" in f.title]
    assert bool(hits) is warns and (not hits or hits[0].level == "warn")


def test_expired_rule_matches_and_is_in_bundled_rules():
    assert any(r.key == "expired" for r in knowledge.RULES)
    doc = json.loads(__import__("pathlib").Path(knowledge.__file__).with_name("rules.json").read_text(encoding="utf-8"))
    assert doc["rules_version"] >= 2
