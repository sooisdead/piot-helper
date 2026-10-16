import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from piothelper import diagnose, logs, stats, timeline


def _ts(minutes_ago=0, fmt="%Y-%m-%d %H:%M:%S.123"):
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime(fmt)


def _logcfg(cfg, *paths):
    cfg["paths"] = [{"name": p.stem, "kind": "log", "path": str(p)} for p in paths]
    return cfg


# ---------- время с поясом ----------

def test_utc_and_offset_timestamps_are_converted_to_local():
    expect = datetime(2026, 10, 15, 10, 43, 38, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    assert logs.line_time("[error] 2026-10-15T10:43:38.360000Z yenisei") == expect
    plus5 = datetime(2026, 10, 15, 15, 43, 38, tzinfo=timezone(timedelta(hours=5))).astimezone().replace(tzinfo=None)
    assert logs.line_time('"expiredTime":"2026-10-15T15:43:38+05:00"') == plus5
    assert logs.line_time("2026.10.15 15:43:38.123 INF без пояса") == datetime(2026, 10, 15, 15, 43, 38)


# ---------- хронология ----------

def test_timeline_sorts_filters_and_collapses_repeats(cfg, tmp_path):
    a = tmp_path / "esm.log"
    a.write_text("\n".join([
        f"{_ts(30)} INF всё хорошо",
        f"{_ts(20)} ERR Ошибка соединения с сервером",
        f"{_ts(19)} ERR Ошибка соединения с сервером",
        f"{_ts(18)} ERR Ошибка соединения с сервером",
        f"{_ts(10)} WRN Медленный ответ",
    ]), encoding="utf-8")
    events = timeline.collect(_logcfg(cfg, a), 2, "warn")
    assert [e.level for e in events] == ["error", "warn"]
    assert events[0].count == 3 and events[0].last_time > events[0].time
    assert len(timeline.collect(_logcfg(cfg, a), 2, "info")) == 3   # info + свёрнутая ошибка + warn
    assert [e.level for e in timeline.collect(_logcfg(cfg, a), 2, "error")] == ["error"]


def test_alternating_repeats_collapse_but_a_long_pause_starts_a_new_event(cfg, tmp_path):
    f = tmp_path / "cm.log"
    lines = []
    for minute in (90, 89, 88):  # серия 1: сообщения A и B чередуются
        lines += [f"{_ts(minute)} ERR Не удалось подключиться к серверу {minute}", f"{_ts(minute)} WRN Повтор попытки {minute}"]
    lines += [f"{_ts(10)} ERR Не удалось подключиться к серверу 7"]   # тот же текст спустя ~80 минут
    f.write_text("\n".join(lines), encoding="utf-8")
    events = timeline.collect(_logcfg(cfg, f), 3, "warn")
    assert [(e.level, e.count) for e in events] == [("error", 3), ("warn", 3), ("error", 1)]


def test_timeline_merges_several_logs_in_time_order(cfg, tmp_path):
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    a.write_text(f"{_ts(10)} ERR из первого\n{_ts(2)} ERR из первого позже", encoding="utf-8")
    b.write_text(f"{_ts(5)} ERR из второго", encoding="utf-8")
    texts = [e.text.split(" ERR ", 1)[-1] for e in timeline.collect(_logcfg(cfg, a, b), 2, "warn")]
    assert texts == ["из первого", "из второго", "из первого позже"]


def test_timeline_window_and_needle(cfg, tmp_path):
    f = tmp_path / "x.log"
    f.write_text(f"{_ts(300)} ERR давняя\n{_ts(5)} ERR свежая про лицензию\n{_ts(4)} ERR свежая про сеть", encoding="utf-8")
    assert len(timeline.collect(_logcfg(cfg, f), 1, "warn")) == 2
    only = timeline.collect(_logcfg(cfg, f), 1, "warn", needle="ЛИЦЕНЗ")
    assert len(only) == 1 and "лицензию" in only[0].text


def test_timeline_skips_files_not_modified_in_window(cfg, tmp_path):
    f = tmp_path / "old.log"
    f.write_text(f"{_ts(5)} ERR свежая запись в давно не менявшемся файле", encoding="utf-8")
    old = time.time() - 100 * 3600
    os.utime(f, (old, old))
    assert timeline.collect(_logcfg(cfg, f), 2, "warn") == []


def test_timeline_marks_known_errors_and_raises_their_level(cfg, tmp_path):
    f = tmp_path / "cm.log"
    f.write_text(f"{_ts(1)} INF rpc error: code = NotFound desc = Client is not found", encoding="utf-8")
    ev = timeline.collect(_logcfg(cfg, f), 1, "warn")
    assert len(ev) == 1 and ev[0].level == "warn" and "gRPC" in ev[0].rule


def test_format_events_text(cfg, tmp_path):
    f = tmp_path / "x.log"
    f.write_text("\n".join(f"{_ts(3 - i)} ERR сбой" for i in range(3)), encoding="utf-8")
    text = timeline.format_events(timeline.collect(_logcfg(cfg, f), 1, "warn"))
    assert "ERROR" in text and "(×3)" in text and "сбой" in text


# ---------- статистика проверок ----------

def _ok(ms, minutes_ago=1):
    return f"{_ts(minutes_ago)} DBG codes/check online ok host=https://cdn05.crpt.ru:19101 elapsed={ms}ms failureCount=0"


def _fail(minutes_ago=1):
    return f"{_ts(minutes_ago)} DBG codes/check online failed host=https://cdn05.crpt.ru:19101 elapsed=1500.2ms failureCount=1"


def test_stats_counts_percentiles_and_slow(cfg, tmp_path):
    f = tmp_path / "esm-cm.log"
    f.write_text("\n".join([_ok(50), _ok(60), _ok(70), _ok(80), _ok(1200), _fail()]), encoding="utf-8")
    s = stats.summarize(stats.collect(_logcfg(cfg, f), 1), 1500)
    assert (s.total, s.ok, s.failed) == (6, 5, 1)
    assert s.p50 == 70 and s.worst == 1200 and s.slow == 1   # 1200 > 2/3 от 1500
    assert "успешно 83%" in stats.describe(s)


def test_stats_from_events_log_when_main_log_has_no_checks(cfg, tmp_path):
    f = tmp_path / "esm-cm-events.log"
    f.write_text(f'{_ts(2)} INF Отправлено сообщение ID: 63. Тип: km_chk. Сообщение: {{"event_code":"km_chk","event_status":0,'
                 '"event_results":{"check_type_online":{"status":0,"host":"cdn05.crpt.ru","elapsed_time":56,"http_code":200}}}. '
                 f"Дата: 2026-10-15 10:42:05", encoding="utf-8")
    checks = stats.collect(_logcfg(cfg, f), 1)
    assert len(checks) == 1 and checks[0].ok and checks[0].ms == 56


def test_stats_ignore_other_codes_check_lines(cfg, tmp_path):
    f = tmp_path / "x.log"
    f.write_text("\n".join([
        f"{_ts(1)} INF Получен запрос POST /api/v1/codes/check",
        f'{_ts(1)} DBG Ответ v1/codes/check response={{"codesResponse":[{{"code":0,"errorCode":0}}]}}',
        f"{_ts(1)} DBG Старая проверка",
    ]), encoding="utf-8")
    assert stats.collect(_logcfg(cfg, f), 1) == []


def test_empty_stats_describe():
    assert "не найдено" in stats.describe(stats.summarize([]))


@pytest.mark.parametrize("lines,level", [
    ([_ok(60), _ok(70)], "ok"),
    ([_ok(60), _ok(1300), _ok(1400)], "warn"),   # p95 близко к таймауту 1500
    ([_ok(60)] * 5 + [_fail()], "warn"),           # 1 из 6 (~17%) — предупреждение
    ([_fail(), _ok(70), _ok(70)], "fail"),         # 1 из 3 (33%) — ≥ 20%, это уже сбой
    ([], "info"),
])
def test_mark_stats_finding_levels(cfg, tmp_path, lines, level):
    f = tmp_path / "esm-cm.log"
    f.write_text("\n".join(lines) or "пусто", encoding="utf-8")
    cfg["esm_config_roots"] = []
    assert diagnose.check_mark_stats(_logcfg(cfg, f))[0].level == level


def test_timeout_is_read_from_esm_config(cfg, tmp_path):
    conf_dir = tmp_path / "ESP"
    conf_dir.mkdir()
    (conf_dir / "config_ЗН_ККТ.yml").write_text("cdnCodesCheckTimeout: 5000\n", encoding="utf-8")
    f = tmp_path / "esm-cm.log"
    f.write_text("\n".join([_ok(1300), _ok(1400)]), encoding="utf-8")   # при таймауте 5000 это не медленно
    cfg["esm_config_roots"] = [str(conf_dir)]
    assert diagnose.check_mark_stats(_logcfg(cfg, f))[0].level == "ok"
