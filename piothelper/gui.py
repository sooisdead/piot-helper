"""Окно программы: вкладки Сводка, Пути, Службы, Порты, Логи."""
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from datetime import datetime

from . import (__version__, checks, config, diagnose, discover, errorlog, fixes, history, knowledge, logs, monitor,
               portowner, report, restart, tray)
from .gui_extra import SettingsTab, TimelineTab

COLORS = {checks.OK: "#1a7f37", checks.WARN: "#9a6700", checks.FAIL: "#cf222e", checks.INFO: "#57606a"}
ICONS = {checks.OK: "✔", checks.WARN: "⚠", checks.FAIL: "✖", checks.INFO: "ℹ"}
MAX_SHOWN_LINES = 3000


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"PIoT Helper {__version__} — диагностика ТС ПИоТ / Frontol / Атол")
        self.geometry("1000x640")
        self.cfg = config.load()
        self._q: queue.Queue = queue.Queue()
        self.after(100, self._poll)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.summary_tab = SummaryTab(nb, self)
        self.fix_tab = FixTab(nb, self)
        self.paths_tab = PathsTab(nb, self)
        self.services_tab = ServicesTab(nb, self)
        self.ports_tab = PortsTab(nb, self)
        self.owner_tab = PortOwnerTab(nb, self)
        self.logs_tab = LogsTab(nb, self)
        self.timeline_tab = TimelineTab(nb, self)
        self.settings_tab = SettingsTab(nb, self)
        for tab, title in [
            (self.summary_tab, "Сводка"), (self.fix_tab, "Исправления"), (self.timeline_tab, "Хронология"),
            (self.paths_tab, "Пути"), (self.services_tab, "Службы"), (self.ports_tab, "Порты"),
            (self.owner_tab, "Порт ККМ"), (self.logs_tab, "Логи"), (self.settings_tab, "Настройки"),
        ]:
            nb.add(tab, text=title)

        self.tray: tray.Tray | None = None
        self.monitor = monitor.Monitor(lambda: self.cfg, self._monitor_notify, self._monitor_result)
        self._hint_shown = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.apply_monitor_settings()

    def save(self):
        config.save(self.cfg)

    # ----- фоновая проверка и значок в трее -----

    def apply_monitor_settings(self):
        """Включает или выключает фоновую проверку и значок согласно настройкам."""
        if self.cfg.get("monitor_enabled"):
            if self.tray is None:
                icon = tray.Tray(lambda: self.ui(self.show_window), self.monitor_run_now, lambda: self.ui(self.shutdown),
                                 "PIoT Helper: проверка в фоне")
                if icon.start():
                    self.tray = icon
                else:
                    self.settings_tab.set_monitor_status("Значок в области уведомлений создать не удалось: уведомлений не будет.")
            self.monitor.start()
            self.settings_tab.set_monitor_status("Фоновая проверка включена: первая проверка через ~20 секунд.")
        else:
            self.monitor.stop()
            if self.tray is not None:
                self.tray.stop()
                self.tray = None
            self.settings_tab.set_monitor_status("Фоновая проверка выключена.")

    def monitor_run_now(self):
        if self.monitor.running:
            self.monitor.run_now()
        else:
            messagebox.showinfo("Фоновая проверка", "Включите фоновую проверку на вкладке «Настройки».")

    def _monitor_notify(self, title: str, text: str, level: str):
        if self.tray is not None:
            self.tray.notify(title, text, level)

    def _monitor_result(self, findings, level: str):
        problems = sum(f.level in ("fail", "warn") for f in findings)
        tip = "PIoT Helper: проблем нет" if not problems else f"PIoT Helper: проблем — {problems}"
        when = datetime.now().strftime("%H:%M")
        if self.tray is not None:
            self.tray.set_state(level, tip)
        self.ui(lambda: self.settings_tab.set_monitor_status(f"Последняя фоновая проверка в {when}: {tip.split(': ', 1)[1]}."))

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def on_close(self):
        """Крестик: при включённой фоновой проверке окно прячется в значок, иначе программа закрывается."""
        if self.tray is not None and self.cfg.get("monitor_enabled"):
            self.withdraw()
            if not self._hint_shown:
                self._hint_shown = True
                self.tray.notify("PIoT Helper", "Программа продолжает проверять кассу в фоне. Выход: правый клик по значку.", "ok")
            return
        self.shutdown()

    def shutdown(self):
        """Полный выход: остановить фоновую проверку и значок, закрыть окно."""
        self.monitor.stop()
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        self.destroy()

    def bg(self, fn, done):
        """Выполнить fn в потоке, результат (или исключение) передать в done в потоке UI."""
        def work():
            try:
                self._q.put((done, fn(), None))
            except Exception as e:  # noqa: BLE001
                errorlog.log(e, "фоновая задача")  # в окне ошибку покажет done, а в журнале останется полный след
                self._q.put((done, None, e))
        threading.Thread(target=work, daemon=True).start()

    def ui(self, fn):
        """Выполнить fn в потоке UI из фонового потока (например, вывод хода работы)."""
        self._q.put((lambda res, err: fn(), None, None))

    def _poll(self):
        try:
            while True:
                done, res, err = self._q.get_nowait()
                try:
                    done(res, err)
                except Exception as e:  # noqa: BLE001 — один сбойный обработчик не должен останавливать опрос очереди
                    errorlog.log(e, "обработчик результата")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def report_callback_exception(self, exc, val, tb):
        """Необработанная ошибка в обработчике окна: в журнал и одно понятное сообщение вместо молчания."""
        short = errorlog.log(val, "обработчик окна") or str(val)
        if not getattr(self, "_error_shown", False):
            self._error_shown = True
            messagebox.showerror("PIoT Helper", f"Произошла ошибка:\n{short}\n\nПодробности записаны в файл:\n{errorlog.path()}")
            self.after(5000, lambda: setattr(self, "_error_shown", False))  # не заваливаем окнами при повторах


class SummaryTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)
        self.run_btn = ttk.Button(bar, text="Проверить всё", command=self.run)
        self.run_btn.pack(side="left")
        ttk.Button(bar, text="Собрать отчёт для поддержки (.zip)", command=self.make_report).pack(side="left", padx=8)
        self.mask = tk.BooleanVar(value=app.cfg.get("mask_report", True))
        ttk.Checkbutton(bar, text="Маскировать личные данные", variable=self.mask).pack(side="left")
        ttk.Button(bar, text="Сравнить два отчёта…", command=self.compare_reports).pack(side="left", padx=8)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left", padx=8)
        self.text = tk.Text(self, wrap="word", font=("Consolas", 10), state="disabled")
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for lvl, color in COLORS.items():
            self.text.tag_config(lvl, foreground=color)

    def run(self):
        self.run_btn.config(state="disabled")
        self.status.config(text="Проверяю…")
        self.app.bg(lambda: checks.run_summary(self.app.cfg), self._show)

    def _show(self, rows, err):
        self.run_btn.config(state="normal")
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        if err:
            self.text.insert("end", f"Ошибка проверки: {err}\n", checks.FAIL)
        else:
            for lvl, line in rows:
                self.text.insert("end", f"{ICONS[lvl]} {line}\n", lvl)
            bad = sum(1 for lvl, _ in rows if lvl == checks.FAIL)
            warn = sum(1 for lvl, _ in rows if lvl == checks.WARN)
            self.status.config(text=f"Проблем: {bad}, предупреждений: {warn}")
        self.text.config(state="disabled")

    def compare_reports(self):
        """Выбрать два архива отчётов (старый и новый) и показать, что изменилось между ними."""
        types = [("Отчёт PIoT Helper", "*.zip")]
        old = filedialog.askopenfilename(title="Старый отчёт (раньше по времени)", filetypes=types)
        if not old:
            return
        new = filedialog.askopenfilename(title="Новый отчёт (позже по времени)", filetypes=types)
        if not new:
            return
        try:
            diff = history.compare(history.read_report(old), history.read_report(new))
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror("Сравнение отчётов", f"Не удалось прочитать отчёт:\n{e}")
            return
        text = history.format_diff(diff, f"отчётом «{Path(old).name}»", f"отчётом «{Path(new).name}»")
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", text + "\n")
        self.text.config(state="disabled")
        self.status.config(text="Сравнение отчётов")

    def make_report(self):
        out = filedialog.asksaveasfilename(
            defaultextension=".zip", initialfile="piot-report.zip", filetypes=[("ZIP", "*.zip")])
        if not out:
            return
        self.status.config(text="Собираю отчёт… (диагностика занимает до минуты)")
        masked = self.mask.get()
        self.app.cfg["mask_report"] = masked
        self.app.save()

        def done(stats, err):
            if err:
                messagebox.showerror("Отчёт", f"Не удалось собрать отчёт:\n{err}")
                self.status.config(text="")
                return
            self.status.config(text=f"Отчёт сохранён: {out}")
            if masked:
                messagebox.showinfo(
                    "Отчёт замаскирован",
                    f"Заменено — {stats}.\n\nНазвания организаций и адреса автоматически не определяются: "
                    "добавьте их в mask_extra в config.json и просмотрите отчёт перед отправкой.")
            else:
                messagebox.showwarning("Отчёт не замаскирован",
                                       "В отчёте лицензионные ключи, заводские номера и другие данные. "
                                       "Не публикуйте его в открытом доступе.")
        self.app.bg(lambda: report.build_report(self.app.cfg, Path(out), masked), done)


class EntryDialog(tk.Toplevel):
    """Диалог добавления/изменения записи пути."""

    def __init__(self, parent, entry=None):
        super().__init__(parent)
        self.title("Путь")
        self.resizable(False, False)
        self.result = None
        entry = entry or {"name": "", "kind": "log", "path": ""}
        self.name = tk.StringVar(value=entry["name"])
        self.kind = tk.StringVar(value=config.KINDS[entry["kind"]])
        self.path = tk.StringVar(value=entry["path"])
        frm = ttk.Frame(self, padding=12)
        frm.pack()
        ttk.Label(frm, text="Название").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.name, width=50).grid(row=0, column=1, columnspan=2, pady=3)
        ttk.Label(frm, text="Тип").grid(row=1, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=self.kind, values=list(config.KINDS.values()), state="readonly",
                     width=47).grid(row=1, column=1, columnspan=2, pady=3)
        ttk.Label(frm, text="Путь").grid(row=2, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.path, width=38).grid(row=2, column=1, pady=3)
        btns = ttk.Frame(frm)
        btns.grid(row=2, column=2, padx=(4, 0))
        ttk.Button(btns, text="Папка…", width=8, command=self.pick_dir).pack(side="left")
        ttk.Button(btns, text="Файл…", width=7, command=self.pick_file).pack(side="left", padx=(2, 0))
        ttk.Button(frm, text="OK", command=self.ok).grid(row=3, column=1, sticky="e", pady=(10, 0))
        ttk.Button(frm, text="Отмена", command=self.destroy).grid(row=3, column=2, pady=(10, 0))
        self.transient(parent)
        self.grab_set()

    def pick_dir(self):
        d = filedialog.askdirectory(parent=self)
        if d:
            self.path.set(os.path.normpath(d))

    def pick_file(self):
        f = filedialog.askopenfilename(parent=self)
        if f:
            self.path.set(os.path.normpath(f))

    def ok(self):
        if not self.name.get().strip():
            messagebox.showwarning("Путь", "Укажите название", parent=self)
            return
        kind = next(k for k, v in config.KINDS.items() if v == self.kind.get())
        self.result = {"name": self.name.get().strip(), "kind": kind, "path": self.path.get().strip().strip('"')}
        self.destroy()


class PathsTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)
        for text, cmd in [("Автопоиск", self.autosearch), ("Добавить", self.add), ("Изменить", self.edit),
                          ("Удалить", self.delete), ("Открыть в проводнике", self.open_explorer),
                          ("Обновить", self.refresh)]:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        cols = ("name", "kind", "path", "state")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, t, w in [("name", "Название", 200), ("kind", "Тип", 130), ("path", "Путь", 400), ("state", "Состояние", 220)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree.bind("<Double-1>", lambda e: self.edit())
        for lvl, color in COLORS.items():
            self.tree.tag_configure(lvl, foreground=color)
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(self.app.cfg["paths"]):
            lvl, text = checks.path_status(e)
            self.tree.insert("", "end", iid=str(i), tags=(lvl,),
                             values=(e["name"], config.KINDS[e["kind"]], e["path"] or "—", f"{ICONS[lvl]} {text}"))
        if hasattr(self.app, "logs_tab"):  # при первом построении вкладка логов ещё не создана
            self.app.logs_tab.reload_sources()

    def _selected(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def autosearch(self):
        DiscoverDialog(self, self.app, self.add_found)

    def add_found(self, items: list[dict]) -> int:
        """Подставляет найденное в пустые строки с подходящим названием, остальное добавляет новыми. Возвращает число."""
        paths = self.app.cfg["paths"]
        added = 0
        for it in items:
            if any(e["path"].strip().lower() == it["path"].lower() for e in paths):
                continue
            if it["product"] == "Недавнее":  # продукт неизвестен — подпись по имени папки
                paths.append({"name": f"Папка: {Path(it['path']).name}", "kind": it["kind"], "path": it["path"]})
                added += 1
                continue
            base = ("Логи " if it["kind"] == "log" else "Папка ") + it["product"]
            empty = next((e for e in paths if e["name"] == base and not e["path"].strip()), None)
            if empty:
                empty["path"] = it["path"]
            else:
                paths.append({"name": f"{base} ({Path(it['path']).name})", "kind": it["kind"], "path": it["path"]})
            added += 1
        self.app.save()
        self.refresh()
        return added

    def add(self):
        dlg = EntryDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self.app.cfg["paths"].append(dlg.result)
            self.app.save()
            self.refresh()

    def edit(self):
        i = self._selected()
        if i is None:
            return
        dlg = EntryDialog(self, self.app.cfg["paths"][i])
        self.wait_window(dlg)
        if dlg.result:
            self.app.cfg["paths"][i] = dlg.result
            self.app.save()
            self.refresh()

    def delete(self):
        i = self._selected()
        if i is not None and messagebox.askyesno("Удалить", f"Удалить «{self.app.cfg['paths'][i]['name']}»?"):
            del self.app.cfg["paths"][i]
            self.app.save()
            self.refresh()

    def open_explorer(self):
        i = self._selected()
        if i is None:
            return
        p = Path(self.app.cfg["paths"][i]["path"])
        if p.exists():
            os.startfile(p if p.is_dir() else p.parent)  # noqa: S606 — только Windows


class ServicesTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Label(bar, text="Искать по словам (через запятую):").pack(side="left")
        self.patterns = tk.StringVar(value=", ".join(app.cfg["service_patterns"]))
        ttk.Entry(bar, textvariable=self.patterns, width=50).pack(side="left", padx=6)
        ttk.Button(bar, text="Найти", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="services.msc", command=lambda: os.startfile("services.msc")).pack(side="left", padx=6)  # noqa: S606
        self.info = ttk.Label(bar, text="")
        self.info.pack(side="left", padx=6)
        cols = ("display", "name", "status", "start")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, t, w in [("display", "Описание", 360), ("name", "Имя", 200), ("status", "Состояние", 120), ("start", "Запуск", 120)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree.tag_configure("run", foreground=COLORS[checks.OK])
        self.tree.tag_configure("stop", foreground=COLORS[checks.FAIL])

    def refresh(self):
        pats = [p.strip() for p in self.patterns.get().split(",") if p.strip()]
        self.app.cfg["service_patterns"] = pats
        self.app.save()
        self.info.config(text="Ищу…")
        self.app.bg(lambda: checks.list_services(pats), self._show)

    def _show(self, rows, err):
        self.tree.delete(*self.tree.get_children())
        if err:
            self.info.config(text=f"Ошибка: {err}")
            return
        self.info.config(text=f"Найдено: {len(rows)}")
        for s in rows:
            self.tree.insert("", "end", tags=("run" if s["status"] == "Running" else "stop",),
                             values=(s["display"], s["name"], s["status"], s["start"]))


class PortsTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)
        self.name, self.host, self.port = tk.StringVar(), tk.StringVar(value="127.0.0.1"), tk.StringVar()
        for label, var, w in [("Название", self.name, 22), ("Хост", self.host, 16), ("Порт", self.port, 7)]:
            ttk.Label(bar, text=label).pack(side="left")
            ttk.Entry(bar, textvariable=var, width=w).pack(side="left", padx=(3, 8))
        ttk.Button(bar, text="Добавить", command=self.add).pack(side="left")
        ttk.Button(bar, text="Удалить", command=self.delete).pack(side="left", padx=6)
        ttk.Button(bar, text="Проверить все", command=self.check_all).pack(side="left")
        ttk.Button(bar, text="Типовые порты ЕСМ/Frontol", command=self.add_typical).pack(side="left", padx=6)
        cols = ("name", "host", "port", "result")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, t, w in [("name", "Название", 260), ("host", "Хост", 180), ("port", "Порт", 80), ("result", "Результат", 260)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for lvl, color in COLORS.items():
            self.tree.tag_configure(lvl, foreground=color)
        self.reload()

    def reload(self):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.app.cfg["ports"]):
            self.tree.insert("", "end", iid=str(i), values=(p["name"], p["host"], p["port"], ""))

    def add(self):
        try:
            port = int(self.port.get())
            assert 0 < port < 65536
        except (ValueError, AssertionError):
            messagebox.showwarning("Порт", "Порт должен быть числом от 1 до 65535")
            return
        self.app.cfg["ports"].append({"name": self.name.get().strip() or f"порт {port}",
                                      "host": self.host.get().strip() or "127.0.0.1", "port": port})
        self.app.save()
        self.reload()

    def delete(self):
        sel = self.tree.selection()
        if sel:
            del self.app.cfg["ports"][int(sel[0])]
            self.app.save()
            self.reload()

    def add_typical(self):
        have = {(p["host"], p["port"]) for p in self.app.cfg["ports"]}
        for name, host, port in knowledge.TYPICAL_PORTS:
            if (host, port) not in have:
                self.app.cfg["ports"].append({"name": name, "host": host, "port": port})
        self.app.save()
        self.reload()

    def check_all(self):
        def work():
            return [checks.check_port(p["host"], p["port"]) for p in self.app.cfg["ports"]]

        def done(res, err):
            if err:
                messagebox.showerror("Порты", str(err))
                return
            for i, (ok, text) in enumerate(res):
                lvl = checks.OK if ok else checks.FAIL
                self.tree.set(str(i), "result", f"{ICONS[lvl]} {text}")
                self.tree.item(str(i), tags=(lvl,))
        self.app.bg(work, done)


class LogsTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self.files: list[Path] = []
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(8, 2))
        self.source = tk.StringVar()
        self.file = tk.StringVar()
        ttk.Label(bar, text="Источник").pack(side="left")
        self.source_cb = ttk.Combobox(bar, textvariable=self.source, state="readonly", width=26)
        self.source_cb.pack(side="left", padx=(4, 10))
        self.source_cb.bind("<<ComboboxSelected>>", lambda e: self.load_files())
        ttk.Label(bar, text="Файл").pack(side="left")
        self.file_cb = ttk.Combobox(bar, textvariable=self.file, state="readonly", width=44)
        self.file_cb.pack(side="left", padx=(4, 10))
        self.file_cb.bind("<<ComboboxSelected>>", lambda e: self.show())
        ttk.Button(bar, text="Обновить", command=self.load_files).pack(side="left")

        bar2 = ttk.Frame(self)
        bar2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(bar2, text="Фильтр").pack(side="left")
        self.filter = tk.StringVar()
        e = ttk.Entry(bar2, textvariable=self.filter, width=30)
        e.pack(side="left", padx=(4, 10))
        e.bind("<Return>", lambda ev: self.show())
        self.only_problems = tk.BooleanVar()
        ttk.Checkbutton(bar2, text="Только ошибки и предупреждения", variable=self.only_problems,
                        command=self.show).pack(side="left")
        self.auto = tk.BooleanVar()
        ttk.Checkbutton(bar2, text="Автообновление (3 с)", variable=self.auto, command=self._auto_tick).pack(side="left", padx=10)
        self.info = ttk.Label(bar2, text="")
        self.info.pack(side="left", padx=6)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.text = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled")
        ys = ttk.Scrollbar(frame, command=self.text.yview)
        xs = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.config(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side="right", fill="y")
        xs.pack(side="bottom", fill="x")
        self.text.pack(fill="both", expand=True)
        self.text.tag_config("error", foreground=COLORS[checks.FAIL])
        self.text.tag_config("warn", foreground=COLORS[checks.WARN])
        self.reload_sources()

    def reload_sources(self):
        names = [e["name"] for e in self.app.cfg["paths"] if e["kind"] == "log"]
        self.source_cb["values"] = names
        if self.source.get() not in names:
            self.source.set(names[0] if names else "")
        self.load_files()

    def load_files(self):
        entry = next((e for e in self.app.cfg["paths"] if e["name"] == self.source.get()), None)
        self.files = logs.list_log_files(entry["path"].strip()) if entry else []
        self.file_cb["values"] = [str(f) for f in self.files]
        self.file.set(str(self.files[0]) if self.files else "")
        self.show()

    def show(self):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        path = Path(self.file.get()) if self.file.get() else None
        if not path:
            self.text.insert("end", "Файлы логов не найдены. Проверьте путь на вкладке «Пути».")
            self.text.config(state="disabled")
            self.info.config(text="")
            return
        try:
            lines = logs.read_tail(path, self.app.cfg["tail_kb"])
        except OSError as e:
            self.text.insert("end", f"Не удалось прочитать файл: {e}")
            self.text.config(state="disabled")
            return
        cls = logs.Classifier(self.app.cfg)
        needle = self.filter.get().strip().lower()
        errs = warns = 0
        shown = []
        for ln in lines:
            lvl = cls.level(ln)
            errs += lvl == "error"
            warns += lvl == "warn"
            if needle and needle not in ln.lower():
                continue
            if self.only_problems.get() and lvl == "info":
                continue
            shown.append((ln, lvl))
        for ln, lvl in shown[-MAX_SHOWN_LINES:]:
            self.text.insert("end", ln + "\n", lvl)
        self.text.see("end")
        self.text.config(state="disabled")
        age = logs.file_age_minutes(path)
        self.info.config(text=f"ошибок: {errs}, предупреждений: {warns}, строк показано: {min(len(shown), MAX_SHOWN_LINES)}, "
                              f"последняя запись {age:.0f} мин назад")

    def _auto_tick(self):
        if self.auto.get():
            self.show()
            self.after(3000, self._auto_tick)


class FixTab(ttk.Frame):
    """Диагностика с предложением исправлений. Любое изменение — только после подтверждения."""

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self.findings: list[diagnose.Finding] = []
        self.note = ""
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)
        self.run_btn = ttk.Button(bar, text="Запустить диагностику", command=self.run)
        self.run_btn.pack(side="left")
        ttk.Button(bar, text="Выделить всё исправимое", command=self.select_fixable).pack(side="left", padx=6)
        self.fix_btn = ttk.Button(bar, text="Исправить выбранное", command=self.apply, state="disabled")
        self.fix_btn.pack(side="left")
        self.restart_btn = ttk.Button(bar, text="Перезапустить связку ПИоТ", command=self.restart_chain)
        self.restart_btn.pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left", padx=8)
        if not portowner.is_admin():
            ttk.Button(bar, text="Перезапустить от администратора", command=self.elevate).pack(side="right")

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        top = ttk.Frame(paned)
        cols = ("state", "title", "fix")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="extended")
        for c, t, w in [("state", "", 36), ("title", "Что найдено", 640), ("fix", "Исправление", 220)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center" if c == "state" else "w")
        ys = ttk.Scrollbar(top, command=self.tree.yview)
        self.tree.config(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        for lvl, color in COLORS.items():
            self.tree.tag_configure(lvl, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        paned.add(top, weight=3)
        self.detail = tk.Text(paned, height=8, wrap="word", font=("Consolas", 9), state="disabled")
        paned.add(self.detail, weight=1)

    def _set_detail(self, text: str):
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text)
        self.detail.config(state="disabled")

    def run(self):
        self.run_btn.config(state="disabled")
        self.fix_btn.config(state="disabled")
        self.status.config(text="Диагностика… (до минуты)")
        self.app.bg(lambda: diagnose.run(self.app.cfg), self._show)

    def _show(self, findings, err):
        self.run_btn.config(state="normal")
        self.tree.delete(*self.tree.get_children())
        if err:
            self.status.config(text=f"Ошибка: {err}")
            return
        self.findings = findings
        for i, f in enumerate(findings):
            self.tree.insert("", "end", iid=str(i), tags=(f.level,),
                             values=(ICONS[f.level], f.title, f.fix.label if f.fix else ""))
        bad = sum(f.level == checks.FAIL for f in findings)
        warn = sum(f.level == checks.WARN for f in findings)
        fixable = sum(f.fix is not None for f in findings)
        self.status.config(text=f"Проблем: {bad}, предупреждений: {warn}, можно исправить: {fixable}")
        # что изменилось с прошлой диагностики; текущий результат сохраняется как новая точка отсчёта
        prev_time, prev = history.latest_snapshot()
        diff_text = ""
        if prev_time:
            diff = history.compare(prev, [(f.level, f.title) for f in findings])
            diff_text = f"С прошлой диагностики ({prev_time:%d.%m %H:%M}). " + history.format_diff(diff)
        try:
            history.save_snapshot(findings)
        except OSError:
            pass
        head = self.note or ("Выберите строку, чтобы увидеть подробности. "
                             "Исправления выполняются только после подтверждения.")
        self._set_detail(head + (f"\n\n{diff_text}" if diff_text else ""))
        self.note = ""

    def on_select(self):
        sel = [self.findings[int(i)] for i in self.tree.selection()]
        self.fix_btn.config(state="normal" if any(f.fix for f in sel) else "disabled")
        if len(sel) == 1:
            f = sel[0]
            text = f.title + "\n\n" + (f.detail or "—")
            if f.fix:
                text += f"\n\nИсправление: {f.fix.description}" + ("\n(нужны права администратора)" if f.fix.needs_admin else "")
            self._set_detail(text)

    def select_fixable(self):
        self.tree.selection_set([str(i) for i, f in enumerate(self.findings) if f.fix])

    def elevate(self):
        if messagebox.askyesno("Права администратора", "Перезапустить программу от администратора?"):
            portowner.relaunch_as_admin()
            self.app.destroy()

    def apply(self):
        chosen = [self.findings[int(i)] for i in self.tree.selection() if self.findings[int(i)].fix]
        if not chosen:
            return
        needs_admin = any(f.fix.needs_admin for f in chosen)
        if needs_admin and not portowner.is_admin():
            if messagebox.askyesno("Нужны права администратора",
                                   "Часть исправлений меняет службы/сертификаты и требует прав администратора.\n"
                                   "Перезапустить программу от администратора?"):
                portowner.relaunch_as_admin()
                self.app.destroy()
            return
        plan = "\n\n".join(f"• {f.fix.description}" for f in chosen)
        if not messagebox.askyesno("Подтвердите исправления",
                                   f"Будет выполнено:\n\n{plan}\n\nДействия записываются в {fixes.log_path().name}. "
                                   "Не делайте этого во время продажи. Продолжить?", icon="warning"):
            return
        self.fix_btn.config(state="disabled")
        self.status.config(text="Исправляю…")

        def work():
            results = []
            for f in chosen:
                try:
                    results.append((True, f.fix.label, f.fix.run()))
                except Exception as e:  # noqa: BLE001
                    results.append((False, f.fix.label, str(e)))
            return results

        self.app.bg(work, self._applied)

    def restart_chain(self):
        self.restart_btn.config(state="disabled")
        self.status.config(text="Ищу службы связки…")
        self.app.bg(lambda: restart.resolve_chain(self.app.cfg), self._confirm_restart)

    def _confirm_restart(self, res, err):
        self.restart_btn.config(state="normal")
        self.status.config(text="")
        if err:
            messagebox.showerror("Перезапуск", f"Не удалось получить список служб:\n{err}")
            return
        chain, missing = res
        if not chain:
            messagebox.showwarning("Перезапуск", "Службы связки не найдены. Проверьте список restart_chain в config.json.")
            return
        if not portowner.is_admin():
            if messagebox.askyesno("Нужны права администратора",
                                   "Остановка и запуск служб требуют прав администратора.\n"
                                   "Перезапустить программу от администратора?"):
                portowner.relaunch_as_admin()
                self.app.destroy()
            return
        stop_order = "\n".join(f"  {i}. {s['display']}" for i, s in enumerate(reversed(chain), 1))
        start_order = "\n".join(f"  {i}. {s['display']}" for i, s in enumerate(chain, 1))
        note = f"\n\nНе найдены (будут пропущены): {', '.join(missing)}" if missing else ""
        if not messagebox.askyesno(
                "Перезапустить связку?",
                f"Остановка (по порядку):\n{stop_order}\n\nЗапуск (по порядку):\n{start_order}{note}\n\n"
                "Пока службы остановлены, касса не сможет пробивать чеки с маркировкой (обычно 1–2 минуты). "
                "Не делайте этого во время продажи. Продолжить?", icon="warning"):
            return
        self.restart_btn.config(state="disabled")
        self.run_btn.config(state="disabled")
        self.progress: list[str] = []

        def say(msg: str):
            self.progress.append(msg)
            self.app.ui(lambda: (self.status.config(text=msg), self._set_detail("\n".join(self.progress))))

        self.app.bg(lambda: restart.run_chain(chain, self.app.cfg, say), self._restarted)

    def _restarted(self, result, err):
        self.restart_btn.config(state="normal")
        self.run_btn.config(state="normal")
        text = f"Перезапуск не выполнен:\n{err}" if err else result
        self.status.config(text="")
        self.note = "Результат перезапуска:\n\n" + text
        self.run()  # диагностика после перезапуска; результат останется в окне деталей

    def _applied(self, results, err):
        if err:
            messagebox.showerror("Исправления", str(err))
            return
        self.note = "Результат исправлений:\n\n" + "\n\n".join(
            f"{'✔' if ok else '✖'} {label}: {msg}" for ok, label, msg in results)
        self.run()  # перепроверяем сразу после исправлений; результат покажет _show


class DiscoverDialog(tk.Toplevel):
    """Результаты автопоиска: выбрать нужные строки и добавить в список путей."""

    def __init__(self, parent, app: App, on_add):
        super().__init__(parent)
        self.title("Автопоиск путей")
        self.geometry("980x460")
        self.app, self.on_add, self.items = app, on_add, []
        self.status = ttk.Label(self, text="Ищу в реестре, службах и типовых папках…")
        self.status.pack(anchor="w", padx=8, pady=8)
        cols = ("product", "kind", "path", "source", "mtime")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        for c, t, w in [("product", "Продукт", 110), ("kind", "Тип", 110), ("path", "Путь", 360),
                        ("source", "Где найдено", 240), ("mtime", "Изменён", 120)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True, padx=8)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=8)
        self.add_btn = ttk.Button(bar, text="Добавить выбранные", command=self.add, state="disabled")
        self.add_btn.pack(side="left")
        ttk.Button(bar, text="Выбрать всё", command=lambda: self.tree.selection_set(self.tree.get_children())).pack(side="left", padx=6)
        ttk.Button(bar, text="Закрыть", command=self.destroy).pack(side="right")
        self.hours = tk.StringVar(value="24")
        ttk.Button(bar, text="Недавние папки", command=self.search_recent).pack(side="right", padx=6)
        ttk.Entry(bar, textvariable=self.hours, width=4).pack(side="right")
        ttk.Label(bar, text="Только что поставили, а не находит? Папки за последние (ч):").pack(side="right", padx=6)
        self.transient(parent)
        app.bg(lambda: discover.find_candidates(app.cfg), self._show)

    def search_recent(self):
        try:
            hours = float(self.hours.get().replace(",", "."))
            assert hours > 0
        except (ValueError, AssertionError):
            messagebox.showwarning("Автопоиск", "Укажите число часов, например 24", parent=self)
            return
        self.status.config(text="Ищу недавно созданные и изменённые папки…")
        self.add_btn.config(state="disabled")
        self.app.bg(lambda: discover.find_recent(hours), self._show)

    def _show(self, items, err):
        if not self.winfo_exists():
            return
        if err:
            self.status.config(text=f"Ошибка поиска: {err}")
            return
        self.tree.delete(*self.tree.get_children())
        self.items = items
        for i, it in enumerate(items):
            mt = datetime.fromtimestamp(it["mtime"]).strftime("%d.%m.%Y %H:%M") if it["mtime"] else ""
            self.tree.insert("", "end", iid=str(i), values=(
                it["product"], config.KINDS[it["kind"]], it["path"], it["source"], mt))
        if items:
            self.status.config(text=f"Найдено: {len(items)}. Выберите нужное (Ctrl/Shift для нескольких). "
                                    "Для логов ориентируйтесь на дату изменения: актуальный лог обновлялся недавно.")
            self.add_btn.config(state="normal")
        else:
            self.status.config(text="Ничего не найдено. Проверьте ключевые слова search_keywords в config.json "
                                    "или добавьте путь вручную.")

    def add(self):
        chosen = [self.items[int(i)] for i in self.tree.selection()]
        if not chosen:
            return
        n = self.on_add(chosen)
        messagebox.showinfo("Автопоиск", f"Добавлено: {n}", parent=self)
        self.destroy()


class PortOwnerTab(ttk.Frame):
    """Кто занимает порт ККМ: COM (состояние + процесс) и TCP (локальный порт или адрес ККМ)."""

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app

        com = ttk.LabelFrame(self, text="COM-порты (ККМ по USB/RS-232)")
        com.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        bar = ttk.Frame(com)
        bar.pack(fill="x", padx=6, pady=6)
        self.com_btn = ttk.Button(bar, text="Проверить COM-порты", command=self.check_com)
        self.com_btn.pack(side="left")
        ttk.Button(bar, text="Завершить процесс выбранного порта", command=self.kill_owner).pack(side="left", padx=6)
        self.com_info = ttk.Label(bar, text="")
        self.com_info.pack(side="left", padx=8)
        self.row_procs: dict[str, list[dict]] = {}
        if not portowner.is_admin():
            ttk.Button(bar, text="Перезапустить от администратора", command=self.elevate).pack(side="right")
        cols = ("port", "device", "state", "owner")
        self.com_tree = ttk.Treeview(com, columns=cols, show="headings", height=6)
        for c, t, w in [("port", "Порт", 70), ("device", "Устройство", 330), ("state", "Состояние", 140), ("owner", "Кто держит порт", 380)]:
            self.com_tree.heading(c, text=t)
            self.com_tree.column(c, width=w)
        self.com_tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for lvl, color in COLORS.items():
            self.com_tree.tag_configure(lvl, foreground=color)

        tcp = ttk.LabelFrame(self, text="TCP/IP (ККМ по сети, порт ПИоТ или драйвера)")
        tcp.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        bar = ttk.Frame(tcp)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Label(bar, text="Порт или IP:порт").pack(side="left")
        self.endpoint = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.endpoint, width=26)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda ev: self.check_tcp())
        ttk.Button(bar, text="Найти", command=self.check_tcp).pack(side="left")
        self.tcp_info = ttk.Label(bar, text="")
        self.tcp_info.pack(side="left", padx=8)
        cols = ("local", "remote", "state", "pid", "name", "path")
        self.tcp_tree = ttk.Treeview(tcp, columns=cols, show="headings", height=6)
        for c, t, w in [("local", "Локальный", 150), ("remote", "Удалённый", 150), ("state", "Состояние", 90),
                        ("pid", "PID", 60), ("name", "Процесс", 140), ("path", "Файл", 320)]:
            self.tcp_tree.heading(c, text=t)
            self.tcp_tree.column(c, width=w)
        self.tcp_tree.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    def elevate(self):
        if messagebox.askyesno("Права администратора",
                               "Без прав администратора видны только процессы вашего пользователя.\n"
                               "Перезапустить программу от администратора?"):
            portowner.relaunch_as_admin()
            self.app.destroy()

    def kill_owner(self):
        sel = self.com_tree.selection()
        procs = self.row_procs.get(sel[0], []) if sel else []
        if not procs:
            messagebox.showinfo("Порт ККМ", "Выберите порт, у которого определён процесс-владелец.")
            return
        listing = "\n".join(f"• {p['name']} (PID {p['pid']})\n  {p['path']}" for p in procs)
        if not messagebox.askyesno(
                "Завершить процесс?",
                f"Будет принудительно завершено:\n{listing}\n\nНесохранённые данные этой программы пропадут. "
                "Если это Frontol или драйвер во время смены — не завершайте. Продолжить?", icon="warning"):
            return

        def work():
            return [fixes.kill_process(p["pid"]) for p in procs]

        def done(res, err):
            messagebox.showerror("Ошибка", str(err)) if err else messagebox.showinfo("Готово", "\n".join(res))
            self.check_com()
        self.app.bg(work, done)

    def check_com(self):
        self.com_btn.config(state="disabled")
        self.com_info.config(text="Проверяю…")

        def work():
            ports = portowner.list_com_ports()
            for p in ports:
                p["state"] = portowner.com_state(p["port"])
            info = {"owners": {}, "admin": portowner.is_admin()}
            if ports:
                try:
                    info = portowner.find_com_owners([p["port"] for p in ports])
                except Exception as exc:  # noqa: BLE001 — показываем причину, а состояние портов всё равно выводим
                    info["error"] = str(exc)
            return ports, info

        self.app.bg(work, self._show_com)

    def _show_com(self, res, err):
        self.com_btn.config(state="normal")
        self.com_tree.delete(*self.com_tree.get_children())
        self.row_procs.clear()
        if err:
            self.com_info.config(text=f"Ошибка: {err}")
            return
        ports, info = res
        owners = info.get("owners", {})
        for p in ports:
            procs = owners.get(p["port"], [])
            busy = p["state"] == "занят"
            if procs:
                owner = "; ".join(f"{o['name']} (PID {o['pid']})" for o in procs)
            elif busy:
                owner = "процесс не определён" + ("" if info.get("admin") else " — запустите от администратора")
            else:
                owner = ""
            lvl = checks.OK if p["state"] == "свободен" else (checks.WARN if busy else checks.FAIL)
            if busy and procs:
                lvl = checks.WARN
            iid = self.com_tree.insert("", "end", tags=(lvl,), values=(p["port"], p["device"], p["state"], owner))
            self.row_procs[iid] = procs
        note = f"Портов: {len(ports)}."
        if info.get("error"):
            note += f" Определить процессы не удалось: {info['error']}"
        elif not info.get("admin"):
            note += " Права администратора не выданы: чужие процессы могут быть не видны."
        if not ports:
            note = "COM-портов в системе нет. ККМ по USB может быть подключена без виртуального COM."
        self.com_info.config(text=note)

    def check_tcp(self):
        try:
            host, port = portowner.parse_endpoint(self.endpoint.get())
        except ValueError as e:
            messagebox.showwarning("TCP", f"Укажите порт (5555) или IP:порт (192.168.1.50:5555).\n{e}")
            return
        self.tcp_info.config(text="Ищу…")
        self.app.bg(lambda: portowner.tcp_owners(host, port), self._show_tcp)

    def _show_tcp(self, rows, err):
        self.tcp_tree.delete(*self.tcp_tree.get_children())
        if err:
            self.tcp_info.config(text=f"Ошибка: {err}")
            return
        for r in rows:
            self.tcp_tree.insert("", "end", values=(r["Local"], r["Remote"], r["State"], r["Pid"], r.get("Name") or "—", r.get("Path") or ""))
        self.tcp_info.config(text=f"Соединений: {len(rows)}" if rows else "Никто не слушает и не подключён к этому порту")


def run(start_hidden: bool = False):
    """start_hidden: запуск из автозапуска (--tray). Окно прячется, только если включена фоновая проверка."""
    app = App()
    if start_hidden and app.tray is not None and app.cfg.get("monitor_enabled"):
        app.withdraw()
    app.mainloop()
