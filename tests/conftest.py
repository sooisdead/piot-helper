"""Общие настройки тестов: каждый тест работает в своей временной «папке программы»."""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    """config.json, rules_*.json и fixes.log пишутся во временную папку, а не рядом с исходниками."""
    import importlib
    import pkgutil

    import piothelper
    from piothelper import config, knowledge
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    # app_dir импортируют по имени в разных модулях: подменяем везде, чтобы ничего не писалось в папку проекта
    for info in pkgutil.iter_modules(piothelper.__path__):
        if info.name in ("gui",):  # окно здесь не нужно
            continue
        module = importlib.import_module(f"piothelper.{info.name}")
        if hasattr(module, "app_dir"):
            monkeypatch.setattr(module, "app_dir", lambda: tmp_path)
    knowledge.reload_rules()
    yield tmp_path
    monkeypatch.undo()
    knowledge.reload_rules()


@pytest.fixture
def cfg():
    from piothelper import config
    return config.load()
