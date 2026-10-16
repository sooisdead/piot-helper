"""Маскирование: на вымышленных данных проверяем, что чувствительное исчезает, а нужное для разбора остаётся."""
import base64
import re
import zipfile

import pytest

from piothelper import checks, diagnose, mask, report

SERIAL, FN = "12345678901234", "9999888877776666"
INN12, INN10 = "123456789012", "1234567890"
KEY = "ABCDE-12345-FGHIJ"
KM = "0104600000000000" + "21" + "AbC123xyz!" + "\x1d93ab12"
KM_B64 = base64.b64encode(KM.encode()).decode()
EMAIL, UUID = "kassir@example.com", "123e4567-e89b-12d3-a456-426614174000"
EXTRA = ["MYSTORE-PC", "ivanov"]

SENSITIVE = [SERIAL, FN, INN12, INN10, KEY, "AbC123xyz", KM_B64[:24], EMAIL, UUID, "8.8.8.8", "MYSTORE-PC", "ivanov",
             "s3cr3tValue", "hunter2x", "abcDEF123token456"]
KEPT = ["127.0.0.1", "192.168.1.5", "20261015144416", "1792060923741", "10.10.8.24"]

SAMPLE = "\n".join([
    f"serial={SERIAL} fn:{FN} db_{SERIAL}.db",
    f'"inn":"{INN12}"',
    f'"Vatin" : "{INN12}"',
    f"ИНН {INN10}, ecr_inn:{INN12}",
    '{\\"vatin\\":\\"' + INN12 + '\\"}',
    "Authorization: Bearer abcDEF123token456",
    '"token":"s3cr3tValue"',
    '{\\"password\\":\\"hunter2x\\"}',
    f"cis: {KM}",
    f"imc {KM_B64}",
    f"{KEY} - Лицензия просрочена",
    f"mail {EMAIL} id {UUID}",
    "connect 8.8.8.8:443 from 192.168.1.5 via 127.0.0.1 driver 10.10.8.24",
    r"C:\Users\ivanov\AppData\Roaming and host MYSTORE-PC",
    "stamp 20261015144416 epoch 1792060923741",
])


@pytest.fixture
def masked():
    m = mask.Masker(EXTRA)
    m.learn(SAMPLE)
    return m, m.text(SAMPLE)


@pytest.mark.parametrize("needle", SENSITIVE)
def test_sensitive_values_are_gone(masked, needle):
    assert needle.lower() not in masked[1].lower()


@pytest.mark.parametrize("kept", KEPT)
def test_useful_values_are_kept(masked, kept):
    assert kept in masked[1]


def test_same_value_same_tag_different_value_different_tag():
    m = mask.Masker()
    out = m.text(f"a {SERIAL} b {SERIAL} c 98765432109876")
    tags = re.findall(r"\[SN\d+\]", out)
    assert tags[0] == tags[1] and tags[0] != tags[2]


def test_tags_are_safe_in_file_names():
    m = mask.Masker(EXTRA)
    name = m.text(f"logs/x/1_fptr10-{SERIAL}.log.tail.txt")
    assert not re.search(r'[<>:"|?*]', name.split("/", 1)[1])


def test_inn_learned_from_one_file_masks_bare_occurrences_in_another():
    m = mask.Masker()
    m.learn(f'"inn":"{INN12}"')
    assert INN12 not in m.text(f"some text {INN12} elsewhere")


def test_masker_stats_mentions_categories():
    m = mask.Masker()
    m.text(f"{KEY} {SERIAL}")
    assert "лицензионных ключей: 1" in m.stats()


def test_nothing_found_is_reported():
    assert mask.Masker().stats() == "ничего не найдено"


# ---------- готовый архив ----------

def _cfg(cfg, tmp_path, masking=True):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / f"fptr10-{SERIAL}.log").write_text(SAMPLE, encoding="utf-8")
    cfg["paths"] = [{"name": "Логи ivanov", "kind": "log", "path": str(logdir)}]
    cfg["mask_extra"] = EXTRA
    cfg["mask_report"] = masking
    return cfg


def _stub(monkeypatch):
    monkeypatch.setattr(checks, "run_summary", lambda cfg: [("ok", r"Папка C:\Users\ivanov\x MYSTORE-PC")])
    monkeypatch.setattr(diagnose, "run", lambda cfg: [])


def test_report_zip_has_no_leaks_and_safe_names(monkeypatch, cfg, tmp_path):
    _stub(monkeypatch)
    out = tmp_path / "r.zip"
    stats = report.build_report(_cfg(cfg, tmp_path), out)
    z = zipfile.ZipFile(out)
    assert stats and not [n for n in z.namelist() if re.search(r'[<>:"|?*]', n)]
    blob = " ".join(z.namelist()) + " ".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
    for needle in SENSITIVE:
        assert needle.lower() not in blob.lower(), needle
    assert "ЗАМАСКИРОВАН" in z.read("summary.txt").decode("utf-8")


def test_unmasked_report_keeps_data_and_warns(monkeypatch, cfg, tmp_path):
    _stub(monkeypatch)
    out = tmp_path / "r.zip"
    assert report.build_report(_cfg(cfg, tmp_path, masking=False), out) == ""
    z = zipfile.ZipFile(out)
    assert "НЕ замаскирован" in z.read("summary.txt").decode("utf-8")
    assert any(SERIAL in n for n in z.namelist())
