import zipfile

import pytest

from piothelper import history


def _titles(items):
    return sorted(t for t, *_ in items)


def test_new_resolved_worse_better():
    old = [("fail", "Служба «A» не запущена"), ("warn", "Смена закончится через 30 мин"), ("warn", "Порт 8000 медленный"),
           ("fail", "Лицензия просрочена"), ("ok", "Время точное")]
    new = [("warn", "Смена закончится через 30 мин"), ("fail", "Порт 8000 медленный"), ("warn", "Лицензия просрочена"),
           ("fail", "Новая беда"), ("ok", "Служба «A» работает"), ("ok", "Время точное")]
    d = history.compare(old, new)
    assert _titles(d.new) == ["Новая беда"]
    assert _titles(d.worse) == ["Порт 8000 медленный"]
    assert _titles(d.better) == ["Лицензия просрочена"]
    assert _titles(d.resolved) == ["Служба «A» не запущена"]
    assert d.unchanged_problems == 1


def test_numbers_and_masking_tags_are_not_differences():
    old = [("warn", "Лицензия просрочена (за 48 ч: 8) — осталось дн.: 2 [KEY1]")]
    new = [("warn", "Лицензия просрочена (за 48 ч: 20) — осталось дн.: 1 [KEY7]")]
    d = history.compare(old, new)
    assert d.empty and d.unchanged_problems == 1


def test_ok_and_info_changes_are_ignored():
    d = history.compare([("ok", "A"), ("info", "B")], [("info", "A"), ("ok", "B"), ("ok", "C")])
    assert d.empty and d.unchanged_problems == 0


def test_problem_disappearing_counts_as_resolved():
    d = history.compare([("fail", "Порт закрыт")], [])
    assert d.resolved == [("Порт закрыт", "fail")]


def test_format_diff_sections():
    d = history.compare([("fail", "Старая")], [("warn", "Новая")])
    text = history.format_diff(d)
    assert "Новые проблемы" in text and "Исправлено" in text and "Новая" in text and "Старая" in text


def test_format_diff_when_all_clear():
    assert "Проблем нет" in history.format_diff(history.compare([], []))


def test_snapshots_roundtrip_and_cleanup():
    for i in range(history.KEEP + 5):
        history.save_snapshot([("fail", f"Проблема {i}")])
    files = sorted(history.history_dir().glob("*.json"))
    assert len(files) <= history.KEEP
    when, items = history.latest_snapshot()
    assert when is not None and items and items[0][0] == "fail"


def test_latest_snapshot_when_none():
    assert history.latest_snapshot() == (None, [])


def test_snapshot_accepts_findings_objects():
    from piothelper.diagnose import Finding
    history.save_snapshot([Finding("warn", "Заголовок", "подробности")])
    assert history.latest_snapshot()[1] == [("warn", "Заголовок")]


def test_parse_report_text_skips_detail_lines():
    text = "Результат\n\n[FAIL] Первая\n        подробности\n\n[ OK ] Вторая\n[WARN] Третья\n[INFO] Четвёртая"
    assert history.parse_report_text(text) == [("fail", "Первая"), ("ok", "Вторая"), ("warn", "Третья"), ("info", "Четвёртая")]


def _zip(path, **files):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name.replace("_", ".", 1) if name.endswith("_txt") else name, text)
    return path


def test_read_report_prefers_diagnostics_then_summary(tmp_path):
    new = tmp_path / "new.zip"
    with zipfile.ZipFile(new, "w") as z:
        z.writestr("summary.txt", "[FAIL] из summary")
        z.writestr("diagnostics.txt", "[WARN] из diagnostics")
    old = tmp_path / "old.zip"
    with zipfile.ZipFile(old, "w") as z:
        z.writestr("summary.txt", "[FAIL] только summary")
    assert history.read_report(new) == [("warn", "из diagnostics")]
    assert history.read_report(old) == [("fail", "только summary")]


def test_read_report_rejects_foreign_archive(tmp_path):
    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "hi")
    with pytest.raises(ValueError, match="не отчёт"):
        history.read_report(p)


def test_compare_two_real_style_reports(tmp_path):
    a, b = tmp_path / "a.zip", tmp_path / "b.zip"
    with zipfile.ZipFile(a, "w") as z:
        z.writestr("diagnostics.txt", "[FAIL] ЛМ ЧЗ [SN1]: not_configured\n        детали\n[WARN] Смена закончится через 20 мин")
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("diagnostics.txt", "[WARN] Смена закончится через 5 мин\n[FAIL] Новая [SN9] проблема")
    d = history.compare(history.read_report(a), history.read_report(b))
    assert _titles(d.new) == ["Новая [SN9] проблема"] and _titles(d.resolved) == ["ЛМ ЧЗ [SN1]: not_configured"]
    assert d.unchanged_problems == 1
