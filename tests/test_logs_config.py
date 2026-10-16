import json
import os
import time
from datetime import datetime, timedelta

import pytest

from piothelper import config, logs


# ---------- классификация строк ----------

@pytest.mark.parametrize("line,level", [
    ('{"level":"info","msg":"запрос обработан с ошибкой: NotFound"}', "info"),   # доверяем явному уровню
    ('{"level":"error","msg":"boom"}', "error"),
    ("2026.10.15 14:52:29.516 ERR Response error=\"x\"", "error"),
    ("2026.10.15 14:52:29.516 WRN медленно", "warn"),
    ("2026.10.15 14:52:29.516 DBG [ERR] GET failed", "info"),
    ('      "error" : 0', "info"),
    ('"description" : "Ошибок нет"', "info"),
    ("fatal: что-то сломалось", "error"),
    ("warning: диск почти полон", "warn"),
    ("[error] 2026-10-15T10:43:38.360000Z yenisei@127.0.0.1 forbidden", "error"),
    ("обычная строка без ничего", "info"),
])
def test_classifier(cfg, line, level):
    assert logs.Classifier(cfg).level(line) == level


# ---------- время и окно ----------

def test_line_time_formats():
    assert logs.line_time("2026-10-15 15:32:40\tU\tтекст") == datetime(2026, 10, 15, 15, 32, 40)
    assert logs.line_time('"time":"2026.09.02 08:54:28.861"') == datetime(2026, 9, 2, 8, 54, 28)
    assert logs.line_time("без времени") is None
    assert logs.line_time("2026-13-45 99:99:99") is None


def test_recent_lines_inherits_time_for_continuation_lines():
    now = datetime.now()
    old, new = (now - timedelta(hours=100)), (now - timedelta(minutes=5))
    lines = [f"{old:%Y-%m-%d %H:%M:%S} старая", "  продолжение старой", f"{new:%Y-%m-%d %H:%M:%S} свежая", "  продолжение свежей"]
    recent, has_ts = logs.recent_lines(lines, 48)
    assert has_ts and recent == lines[2:]


def test_recent_lines_without_timestamps_returns_everything():
    recent, has_ts = logs.recent_lines(["a", "b"], 1)
    assert not has_ts and recent == ["a", "b"]


# ---------- чтение файлов ----------

def test_read_tail_encodings(tmp_path):
    (tmp_path / "u8.log").write_bytes("строка один\nстрока два".encode("utf-8-sig"))
    (tmp_path / "cp.log").write_bytes("строка один\nстрока два".encode("cp1251"))
    (tmp_path / "u16.log").write_bytes("строка один\nстрока два".encode("utf-16"))
    for name in ("u8.log", "cp.log", "u16.log"):
        assert logs.read_tail(tmp_path / name, 10) == ["строка один", "строка два"], name


def test_read_tail_cuts_to_tail_and_drops_broken_first_line(tmp_path):
    p = tmp_path / "big.log"
    p.write_text("\n".join(f"line{i:05d}" for i in range(5000)), encoding="utf-8")
    out = logs.read_tail(p, 1)  # 1 КБ
    assert out[-1] == "line04999" and all(len(x) == 9 for x in out)


def test_list_log_files_filters_and_sorts_by_freshness(tmp_path):
    old, new, other = tmp_path / "a.log", tmp_path / "b.log", tmp_path / "readme.md"
    for p in (old, new, other):
        p.write_text("x")
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    assert logs.list_log_files(str(tmp_path)) == [new, old]
    assert logs.list_log_files(str(new)) == [new]
    assert logs.list_log_files("") == [] and logs.list_log_files(str(tmp_path / "нет")) == []


# ---------- настройки ----------

@pytest.mark.parametrize("text,words,expected", [
    ("ProgramData\\ESP\\ESM", ["=esp"], True),
    ("Respond", ["=esp"], False),
    ("Resmon", ["=esm"], False),
    ("AtolLicSvc", ["atol"], True),
    ("Служба инвентаризации", ["=инвента"], False),
    ("Inventa ESM", ["=inventa"], True),
    ("что угодно", [], False),
])
def test_keyword_match(text, words, expected):
    assert config.keyword_match(text, words) is expected


def test_old_config_gets_new_default_words_without_losing_user_data(tmp_path):
    old = {"service_patterns": ["piot"], "search_keywords": {"ТС ПИоТ": ["piot"]}, "paths": [{"name": "x", "kind": "dir", "path": "C:\\x"}],
           "ports": [{"name": "p", "host": "h", "port": 1}]}
    config.CONFIG_PATH.write_text(json.dumps(old), encoding="utf-8")
    c = config.load()
    assert "=esm" in c["service_patterns"] and c["service_patterns"][0] == "piot"
    assert "=esp" in c["search_keywords"]["ТС ПИоТ"]
    assert c["paths"] == old["paths"] and c["ports"] == old["ports"]
    assert c["defaults_version"] == config.DEFAULT_CONFIG["defaults_version"]


def test_save_and_load_roundtrip():
    c = config.load()
    c["recent_hours"] = 7
    config.save(c)
    assert config.load()["recent_hours"] == 7


def test_config_with_bom_is_read():
    """PowerShell 5 и старый Блокнот сохраняют JSON с BOM: настройки не должны молча сбрасываться."""
    config.CONFIG_PATH.write_bytes(b"\xef\xbb\xbf" + json.dumps({"monitor_enabled": True, "recent_hours": 3}).encode("utf-8"))
    c = config.load()
    assert c["monitor_enabled"] is True and c["recent_hours"] == 3


def test_broken_config_falls_back_to_defaults():
    config.CONFIG_PATH.write_text("{сломан", encoding="utf-8")
    assert config.load()["recent_hours"] == config.DEFAULT_CONFIG["recent_hours"]


def test_powershell_path_is_resolved():
    assert config.POWERSHELL.lower().endswith("powershell.exe") or config.POWERSHELL == "powershell"
