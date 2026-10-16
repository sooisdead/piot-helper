import os
import time

import pytest

from piothelper import checks, config, discover, fixes, restart

SERVICES = [("atol-fptr-rpc-server", "ATOL Fiscal Printer Remote Server"), ("atol-grpc-service", "ATOL: Fptr grpc service"),
            ("esm-cm-12345678901234", "ESM: Control Module"), ("esm-lm-controller", "ESM: Local Module Controller"),
            ("esm-orchestrator", "ESM: Orchestrator"), ("fdsvc", "Frontol and Drivers service")]


@pytest.fixture
def fake(monkeypatch):
    class Env:
        state = {n: "Running" for n, _ in SERVICES}
        calls: list = []
        fail_stop: set = set()
        fail_start: set = set()
        problems: list = []

    env = Env()
    monkeypatch.setattr(checks, "all_services", lambda: [{"name": n, "display": d, "status": env.state[n], "start": "Automatic"} for n, d in SERVICES])

    def stop(n):
        env.calls.append(("stop", n))
        if n in env.fail_stop:
            raise RuntimeError("не остановилась")
        env.state[n] = "Stopped"

    def start(n):
        env.calls.append(("start", n))
        if n in env.fail_start:
            raise RuntimeError("не запустилась")
        env.state[n] = "Running"

    monkeypatch.setattr(fixes, "stop_service", stop)
    monkeypatch.setattr(fixes, "start_service", start)
    monkeypatch.setattr(restart.time, "sleep", lambda s: None)
    monkeypatch.setattr(restart, "_fresh_problems", lambda cfg, since: list(env.problems))
    return env


def test_chain_resolves_in_start_order(cfg, fake):
    chain, missing = restart.resolve_chain(cfg)
    assert [s["name"] for s in chain] == ["atol-grpc-service", "esm-cm-12345678901234", "esm-lm-controller", "esm-orchestrator"]
    assert missing == []


def test_missing_patterns_are_reported(cfg, fake):
    cfg["restart_chain"] = ["Fptr grpc", "Несуществующая служба"]
    chain, missing = restart.resolve_chain(cfg)
    assert len(chain) == 1 and missing == ["Несуществующая служба"]


def test_stop_reverse_then_start_forward_and_success_verdict(cfg, fake):
    chain, _ = restart.resolve_chain(cfg)
    out = restart.run_chain(chain, cfg, lambda m: None)
    order = [(a, n.split("-")[0] + "-" + n.split("-")[1]) for a, n in fake.calls]
    assert [n for a, n in fake.calls if a == "stop"] == ["esm-orchestrator", "esm-lm-controller", "esm-cm-12345678901234", "atol-grpc-service"]
    assert [n for a, n in fake.calls if a == "start"] == ["atol-grpc-service", "esm-cm-12345678901234", "esm-lm-controller", "esm-orchestrator"]
    assert [a for a, _ in fake.calls] == ["stop"] * 4 + ["start"] * 4
    assert "Связь восстановлена" in out


def test_failed_stop_restores_already_stopped_services(cfg, fake):
    fake.fail_stop = {"esm-cm-12345678901234"}
    chain, _ = restart.resolve_chain(cfg)
    with pytest.raises(RuntimeError, match="запущены обратно"):
        restart.run_chain(chain, cfg, lambda m: None)
    assert all(v == "Running" for v in fake.state.values())


def test_failed_start_is_reported_without_verification(cfg, fake):
    fake.fail_start = {"esm-orchestrator"}
    chain, _ = restart.resolve_chain(cfg)
    out = restart.run_chain(chain, cfg, lambda m: None)
    assert "НЕ ЗАПУЩЕНА: ESM: Orchestrator" in out and "завершён с ошибками" in out


def test_persistent_connection_problems_give_negative_verdict(cfg, fake, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(restart.time, "time", lambda: clock.__setitem__("t", clock["t"] + 12.0) or clock["t"])
    fake.problems = ["esm-cm.log: 2026.10.15 15:50:01 ERR Client is not found"]
    chain, _ = restart.resolve_chain(cfg)
    out = restart.run_chain(chain, cfg, lambda m: None)
    assert "ещё" in out or "пока сообщает" in out
    assert "Client is not found" in out and "Связь восстановлена" not in out


# ---------- автопоиск ----------

@pytest.mark.parametrize("text,product", [
    ("ESP", "ТС ПИоТ"), ("ProgramData\\ESP\\ESM", "ТС ПИоТ"), ("Единый сервисный модуль", "ТС ПИоТ"),
    ("AtolLicSvc", "Атол"), ("Frontol 6", "Frontol"), ("Respond", None), ("Despooler", None),
    ("Служба инвентаризации и оценки совместимости", None),
])
def test_product_of(cfg, text, product):
    assert discover.product_of(text, cfg) == product


def test_markers_find_esm_folder_by_certificates(cfg, tmp_path):
    esm = tmp_path / "ProgramData" / "ESP" / "ESM" / "um"
    esm.mkdir(parents=True)
    (esm / "ca.crt").write_text("x")
    (esm / "server.crt").write_text("x")
    (tmp_path / "ProgramData" / "Other").mkdir()
    found = list(discover._from_markers(cfg, roots=[str(tmp_path / "ProgramData")]))
    assert [p for _, p, _ in found] == [str(esm)]


def test_markers_find_esm_folder_by_config_file(cfg, tmp_path):
    d = tmp_path / "ESP" / "cfg"
    d.mkdir(parents=True)
    (d / "config_ЗН_ККТ.yml").write_text("a: 1")
    assert len(list(discover._from_markers(cfg, roots=[str(tmp_path)]))) == 1


def test_known_paths_only_existing(cfg, tmp_path):
    cfg["known_paths"] = [str(tmp_path), r"C:\нет\такого\пути"]
    assert [p for _, p, _ in discover._from_known(cfg)] == [str(tmp_path)]


def test_find_log_dirs_ignores_txt_and_goes_top_most(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "license.txt").write_text("x")
    logs = tmp_path / "data" / "logs"
    logs.mkdir(parents=True)
    (logs / "app.log").write_text("x")
    found = [p for p, _ in discover._find_log_dirs(str(tmp_path))]
    assert found == [str(logs)]


def test_find_recent_lists_fresh_folders(monkeypatch, tmp_path):
    fresh = tmp_path / "NewTool"
    (fresh / "logs").mkdir(parents=True)
    monkeypatch.setattr(discover, "_scan_roots", lambda: [(str(tmp_path), 3)])
    found = discover.find_recent(2)
    assert str(fresh) in [r["path"] for r in found]
    assert all(r["product"] == "Недавнее" and r["kind"] == "dir" for r in found)


def test_find_recent_ignores_folders_older_than_window(monkeypatch, tmp_path):
    (tmp_path / "NewTool").mkdir()
    monkeypatch.setattr(discover, "_scan_roots", lambda: [(str(tmp_path), 3)])
    real_now = time.time()
    monkeypatch.setattr(discover.time, "time", lambda: real_now + 100 * 3600)  # «сейчас» — через 100 часов
    assert discover.find_recent(2) == []
