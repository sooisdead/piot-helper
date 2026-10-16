import json
import pathlib
import re
import urllib.request

import pytest

from piothelper import knowledge

CASES = json.loads((pathlib.Path(__file__).parent / "rule_cases.json").read_text(encoding="utf-8"))
BUNDLED = json.loads((pathlib.Path(knowledge.__file__).with_name("rules.json")).read_text(encoding="utf-8"))


def _rule(key):
    return next(r for r in knowledge.RULES if r.key == key)


def test_bundled_rules_are_valid():
    items, errors = knowledge.validate_rules_doc(BUNDLED)
    assert errors == []
    assert len(items) == len(BUNDLED["rules"])


def test_every_rule_has_cases_and_no_orphan_cases():
    keys = {r["key"] for r in BUNDLED["rules"]}
    assert keys == set(CASES["match"]), "у каждого правила должны быть примеры в tests/rule_cases.json"


@pytest.mark.parametrize("key,text", [(k, t) for k, texts in CASES["match"].items() for t in texts])
def test_rule_matches_its_examples(key, text):
    assert re.search(_rule(key).pattern, text, re.I), f"правило {key} не нашло: {text}"


@pytest.mark.parametrize("text", CASES["no_match"])
def test_no_rule_fires_on_noise(text):
    hits = [r.key for r in knowledge.RULES if re.search(r.pattern, text, re.I)]
    assert hits == [], f"ложное срабатывание {hits}: {text}"


# ---------- проверка документа правил ----------

def _doc(**over):
    rule = {"key": "t1", "pattern": "boom", "level": "warn", "title": "Тест", "advice": "Совет"}
    rule.update(over)
    return {"rules_version": 5, "rules": [rule]}


@pytest.mark.parametrize("bad", [
    {"level": "critical"}, {"pattern": "(a+)+$"}, {"pattern": "[unclosed"}, {"pattern": ""}, {"title": ""},
    {"key": "bad key!"}, {"pattern": "x" * 2000},
])
def test_validation_rejects_bad_rules(bad):
    items, errors = knowledge.validate_rules_doc(_doc(**bad))
    assert items == [] and errors


def test_validation_rejects_duplicate_keys_and_garbage():
    doc = _doc()
    doc["rules"].append(dict(doc["rules"][0]))
    assert knowledge.validate_rules_doc(doc)[1]
    assert knowledge.validate_rules_doc([])[1]
    assert knowledge.validate_rules_doc({"rules": "x"})[1]


def test_codes_helper_expands_and_ignores_timestamp_milliseconds():
    items, errors = knowledge.validate_rules_doc(_doc(pattern="", codes=[{"n": "777", "extra": "boom"}]))
    assert not errors
    rx = re.compile(items[0]["pattern"], re.I)
    assert rx.search("Ошибка 777: что-то") and rx.search("777 boom")
    assert not rx.search("14:14:28.777 INFO ok")


# ---------- слои правил ----------

def test_user_rules_add_replace_and_disable(isolated_app):
    user = {"rules": [
        {"key": "mine", "pattern": "мойсбой", "level": "fail", "title": "Мой сбой", "advice": "Вручную"},
        {"key": "514", "pattern": "переопределено", "level": "info", "title": "Новый 514", "advice": ""},
        {"key": "401", "disabled": True},
    ]}
    (isolated_app / "rules_user.json").write_text(json.dumps(user, ensure_ascii=False), encoding="utf-8")
    knowledge.reload_rules()
    keys = {r.key for r in knowledge.RULES}
    assert "mine" in keys and "401" not in keys
    assert _rule("514").title == "Новый 514"
    assert knowledge.RULES_INFO["user"] == 3


def test_user_rules_file_with_bom_is_read(isolated_app):
    user = {"rules": [{"key": "mine", "pattern": "мойсбой", "level": "fail", "title": "Мой сбой", "advice": ""}]}
    (isolated_app / "rules_user.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(user, ensure_ascii=False).encode("utf-8"))
    knowledge.reload_rules()
    assert "mine" in {r.key for r in knowledge.RULES} and not knowledge.RULES_INFO["errors"]


def test_broken_user_rules_do_not_break_loading(isolated_app):
    (isolated_app / "rules_user.json").write_text("{не json", encoding="utf-8")
    knowledge.reload_rules()
    assert len(knowledge.RULES) == len(BUNDLED["rules"])
    assert knowledge.RULES_INFO["errors"]


def test_newer_downloaded_rules_override_bundled_but_older_are_ignored(isolated_app):
    newer = _doc(key="fresh")
    newer["rules_version"] = knowledge.RULES_INFO["bundled"] + 1
    (isolated_app / "rules_update.json").write_text(json.dumps(newer), encoding="utf-8")
    knowledge.reload_rules()
    assert [r.key for r in knowledge.RULES] == ["fresh"]
    older = _doc(key="stale")
    older["rules_version"] = 0
    (isolated_app / "rules_update.json").write_text(json.dumps(older), encoding="utf-8")
    knowledge.reload_rules()
    assert len(knowledge.RULES) == len(BUNDLED["rules"])


# ---------- скачивание правил (сеть подменена) ----------

class FakeResponse:
    def __init__(self, body: bytes, url="https://raw.githubusercontent.com/x/y/main/rules.json"):
        self.body, self.url = body, url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def geturl(self):
        return self.url


def _fake_net(monkeypatch, response):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: response)


def test_update_installs_newer_rules(monkeypatch, isolated_app):
    doc = _doc(key="fresh")
    doc["rules_version"] = knowledge.RULES_INFO["bundled"] + 3
    _fake_net(monkeypatch, FakeResponse(json.dumps(doc).encode()))
    msg = knowledge.update_rules()
    assert "обновлены" in msg and (isolated_app / "rules_update.json").exists()
    assert [r.key for r in knowledge.RULES] == ["fresh"]


def test_update_skips_same_version(monkeypatch):
    doc = _doc()
    doc["rules_version"] = knowledge.RULES_INFO["bundled"]
    _fake_net(monkeypatch, FakeResponse(json.dumps(doc).encode()))
    assert "актуальны" in knowledge.update_rules()


@pytest.mark.parametrize("response,error", [
    (FakeResponse(b"{}", url="https://evil.example.com/rules.json"), "GitHub"),
    (FakeResponse(b"x" * (knowledge.MAX_RULES_BYTES + 10)), "большой"),
    (FakeResponse(json.dumps(_doc(level="boom")).encode()), "отклонён"),
])
def test_update_rejects_bad_downloads(monkeypatch, response, error):
    _fake_net(monkeypatch, response)
    with pytest.raises(ValueError, match=error):
        knowledge.update_rules()


def test_update_requires_https():
    with pytest.raises(ValueError, match="https"):
        knowledge.update_rules("http://raw.githubusercontent.com/x/rules.json")
