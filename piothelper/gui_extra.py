"""Вкладки «Хронология» и «Настройки» (фоновая проверка, правила, о программе)."""
import os
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk

from . import __version__, autostart, checks, diagnose, knowledge, portowner, schedule, scheduled, stats, timeline
from .config import app_dir

REPO_URL = "https://github.com/sooisdead/piot-helper"
LEVEL_COLORS = {"error": "#cf222e", "warn": "#9a6700", "info": "#57606a"}
LEVEL_CHOICES = {"Предупреждения и ошибки": "warn", "Только ошибки": "error", "Всё": "info"}
LEVEL_TEXT = {"error": "ошибка", "warn": "предупр.", "info": "инфо"}
MAX_EVENTS = 5000


class TimelineTab(ttk.Frame):
    """Все логи одной лентой по времени + статистика проверок марок."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.events: list[timeline.Event] = []
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(bar, text="Период (ч)").pack(side="left")
        self.hours = tk.StringVar(value=str(app.cfg.get("stats_hours", 24)))
        ttk.Entry(bar, textvariable=self.hours, width=5).pack(side="left", padx=(4, 10))
        ttk.Label(bar, text="Показывать").pack(side="left")
        self.level = tk.StringVar(value="Предупреждения и ошибки")
        ttk.Combobox(bar, textvariable=self.level, values=list(LEVEL_CHOICES), state="readonly", width=24).pack(side="left", padx=(4, 10))
        ttk.Label(bar, text="Фильтр").pack(side="left")
        self.needle = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.needle, width=24)
        e.pack(side="left", padx=(4, 10))
        e.bind("<Return>", lambda ev: self.build())
        self.build_btn = ttk.Button(bar, text="Построить", command=self.build)
        self.build_btn.pack(side="left")
        ttk.Button(bar, text="Сохранить в файл…", command=self.save).pack(side="left", padx=6)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="left", padx=6)

        self.stats_label = ttk.Label(self, text="", wraplength=950, justify="left")
        self.stats_label.pack(fill="x", padx=8, pady=(2, 4))

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        top = ttk.Frame(paned)
        cols = ("time", "level", "source", "text")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse")
        for c, t, w in [("time", "Время", 130), ("level", "Уровень", 80), ("source", "Источник", 230), ("text", "Сообщение", 560)]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w)
        ys = ttk.Scrollbar(top, command=self.tree.yview)
        self.tree.config(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        for lvl, color in LEVEL_COLORS.items():
            self.tree.tag_configure(lvl, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", lambda ev: self.on_select())
        paned.add(top, weight=3)
        self.detail = tk.Text(paned, height=7, wrap="word", font=("Consolas", 9), state="disabled")
        paned.add(self.detail, weight=1)

    def _hours(self) -> float | None:
        try:
            h = float(self.hours.get().replace(",", "."))
            assert h > 0
            return h
        except (ValueError, AssertionError):
            messagebox.showwarning("Хронология", "Укажите период в часах, например 24")
            return None

    def build(self):
        hours = self._hours()
        if hours is None:
            return
        level, needle, cfg = LEVEL_CHOICES[self.level.get()], self.needle.get(), self.app.cfg
        self.build_btn.config(state="disabled")
        self.status.config(text="Строю ленту…")

        def job():
            events = timeline.collect(cfg, hours, level, needle, limit=MAX_EVENTS)
            st = stats.summarize(stats.collect(cfg, hours), diagnose.esm_timeout(cfg))
            return events, st, hours

        self.app.bg(job, self._show)

    def _show(self, res, err):
        self.build_btn.config(state="normal")
        self.tree.delete(*self.tree.get_children())
        if err:
            self.status.config(text=f"Ошибка: {err}")
            return
        self.events, st, hours = res
        for i, e in enumerate(self.events):
            when = f"{e.time:%d.%m %H:%M:%S}" + (f"–{e.last_time:%H:%M}" if e.count > 1 else "")
            text = e.text.replace("\t", " ")[:300] + (f"   (×{e.count})" if e.count > 1 else "")
            self.tree.insert("", "end", iid=str(i), tags=(e.level,), values=(when, LEVEL_TEXT[e.level], e.source, text))
        children = self.tree.get_children()
        if children:
            self.tree.see(children[-1])
        shown = f"Событий: {len(self.events)}" + (f" (показаны последние {MAX_EVENTS})" if len(self.events) >= MAX_EVENTS else "")
        self.status.config(text=shown)
        self.stats_label.config(text=f"Проверки марок за {hours:g} ч: {stats.describe(st)}")
        self._set_detail("Выберите событие, чтобы увидеть полный текст и совет по известной ошибке.")

    def _set_detail(self, text: str):
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", text)
        self.detail.config(state="disabled")

    def on_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        e = self.events[int(sel[0])]
        text = f"{e.time:%d.%m.%Y %H:%M:%S}  {e.source}\n\n{e.text}"
        if e.count > 1:
            text += f"\n\nПовторялось {e.count} раз до {e.last_time:%H:%M:%S}."
        if e.rule:
            rule = next((r for r in knowledge.RULES if r.title == e.rule), None)
            text += f"\n\nИзвестная ошибка: {e.rule}" + (f"\n{rule.advice}" if rule and rule.advice else "")
        self._set_detail(text)

    def save(self):
        if not self.events:
            messagebox.showinfo("Хронология", "Сначала постройте ленту.")
            return
        out = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="timeline.txt", filetypes=[("Текст", "*.txt")])
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(timeline.format_events(self.events))
            self.status.config(text=f"Сохранено: {out}")


class SettingsTab(ttk.Frame):
    """Фоновая проверка, правила разбора, сведения о программе."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        cfg = app.cfg

        bg = ttk.LabelFrame(self, text="Фоновая проверка")
        bg.pack(fill="x", padx=10, pady=(10, 6))
        self.enabled = tk.BooleanVar(value=cfg.get("monitor_enabled", False))
        ttk.Checkbutton(bg, text="Проверять в фоне и показывать уведомления о новых проблемах (значок в области уведомлений)",
                        variable=self.enabled, command=self.on_monitor_changed).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 2))
        ttk.Label(bg, text="Раз в").grid(row=1, column=0, sticky="e", padx=(28, 4))
        self.minutes = tk.IntVar(value=cfg.get("monitor_minutes", 15))
        spin = ttk.Spinbox(bg, from_=5, to=240, textvariable=self.minutes, width=5, command=self.on_monitor_changed)
        spin.grid(row=1, column=1, sticky="w")
        spin.bind("<FocusOut>", lambda e: self.on_monitor_changed())
        ttk.Label(bg, text="мин. (не реже 5)").grid(row=1, column=2, sticky="w", padx=4)
        self.notify_warn = tk.BooleanVar(value=cfg.get("monitor_notify_warn", False))
        ttk.Checkbutton(bg, text="Уведомлять и о предупреждениях (по умолчанию только об ошибках)", variable=self.notify_warn,
                        command=self.on_monitor_changed).grid(row=2, column=0, columnspan=3, sticky="w", padx=28, pady=2)
        self.autostart = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(bg, text="Запускать вместе с Windows (свёрнуто в значок)", variable=self.autostart,
                        command=self.on_autostart).grid(row=3, column=0, columnspan=3, sticky="w", padx=28, pady=2)
        self.monitor_status = ttk.Label(bg, text="Фоновая проверка выключена.")
        self.monitor_status.grid(row=4, column=0, columnspan=3, sticky="w", padx=28, pady=(2, 8))
        ttk.Button(bg, text="Проверить сейчас", command=app.monitor_run_now).grid(row=1, column=3, padx=12)

        night = ttk.LabelFrame(self, text="Ночной перезапуск служб ПИоТ")
        night.pack(fill="x", padx=10, pady=6)
        self.night_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(night, text="Перезапускать службы ЕСМ каждый день в", variable=self.night_on,
                        command=self.on_nightly_toggle).grid(row=0, column=0, sticky="w", padx=8, pady=(6, 2))
        self.night_time = tk.StringVar(value=cfg.get("scheduled_restart_time", "05:00"))
        ttk.Entry(night, textvariable=self.night_time, width=7).grid(row=0, column=1, sticky="w")
        ttk.Button(night, text="Сменить время", command=self.on_nightly_time).grid(row=0, column=2, padx=6)
        ttk.Button(night, text="Проверить без перезапуска", command=self.test_nightly).grid(row=0, column=3)
        self.night_status = ttk.Label(night, text="Проверяю планировщик…", wraplength=900, justify="left")
        self.night_status.grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 2))
        ttk.Label(night, wraplength=900, justify="left", text=(
            "Перезапускаются только службы ЕСМ (драйвер АТОЛ не затрагивается). Перед перезапуском программа ждёт паузы в "
            "продажах (до часа); если продажи не прекратились, эту ночь пропускает. Задачу выполняет Планировщик Windows от имени "
            "SYSTEM, окно программы для этого открыто быть не должно. Нужны права администратора. Это обходной приём, а не "
            "исправление: причины ошибок (лицензия, сеть, ЛМ ЧЗ) он не устраняет.")).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 8))
        app.after(600, self.refresh_nightly)

        rules = ttk.LabelFrame(self, text="Правила разбора логов")
        rules.pack(fill="x", padx=10, pady=6)
        self.rules_label = ttk.Label(rules, text="", wraplength=900, justify="left")
        self.rules_label.pack(anchor="w", padx=8, pady=(6, 2))
        rb = ttk.Frame(rules)
        rb.pack(anchor="w", padx=8, pady=(2, 8))
        self.update_btn = ttk.Button(rb, text="Обновить правила (GitHub)", command=self.update_rules)
        self.update_btn.pack(side="left")
        ttk.Button(rb, text="Перечитать правила", command=self.reload_rules).pack(side="left", padx=6)
        ttk.Button(rb, text="Открыть папку программы", command=lambda: os.startfile(app_dir())).pack(side="left")  # noqa: S606
        self.refresh_rules()

        about = ttk.LabelFrame(self, text="О программе")
        about.pack(fill="x", padx=10, pady=6)
        ttk.Label(about, text=f"PIoT Helper {__version__} — диагностика связки ТС ПИоТ (ЕСМ) + Frontol 6 + ККМ АТОЛ.\n"
                              "Лицензия GPL-3.0. Проект неофициальный, использовать на свой риск.", justify="left").pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Button(about, text="Страница проекта на GitHub", command=lambda: webbrowser.open(REPO_URL)).pack(anchor="w", padx=8, pady=(2, 8))

    # ----- фоновая проверка -----

    def on_monitor_changed(self):
        try:
            minutes = max(5, min(240, int(self.minutes.get())))
        except (ValueError, tk.TclError):
            minutes = 15
        self.minutes.set(minutes)
        cfg = self.app.cfg
        cfg.update(monitor_enabled=self.enabled.get(), monitor_minutes=minutes, monitor_notify_warn=self.notify_warn.get())
        self.app.save()
        self.app.apply_monitor_settings()

    def on_autostart(self):
        want = self.autostart.get()
        if want and not messagebox.askyesno(
                "Автозапуск",
                "Программа добавит запись в раздел реестра текущего пользователя:\n"
                "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\n\n"
                "и будет запускаться вместе с Windows свёрнутой в значок. Права администратора не нужны, "
                "отключить можно здесь же. Продолжить?"):
            self.autostart.set(False)
            return
        try:
            autostart.set_enabled(want)
        except OSError as e:
            messagebox.showerror("Автозапуск", f"Не удалось изменить автозапуск:\n{e}")
            self.autostart.set(not want)
            return
        if want and not self.enabled.get():
            self.enabled.set(True)  # автозапуск без фоновой проверки не имеет смысла
            self.on_monitor_changed()

    def set_monitor_status(self, text: str):
        self.monitor_status.config(text=text)

    # ----- ночной перезапуск -----

    def refresh_nightly(self):
        self.app.bg(schedule.status, self._nightly_shown)

    def _nightly_shown(self, info, err):
        if err:
            self.night_status.config(text=f"Не удалось прочитать Планировщик Windows: {err}")
            return
        self.night_on.set(info is not None)
        if info is None:
            self.night_status.config(text="Выключен.")
            return
        if info["time"]:
            self.night_time.set(info["time"])
        text = f"Включён: каждый день в {info['time'] or self.night_time.get()}."
        if info["next_run"]:
            text += f" Следующий запуск: {info['next_run'].replace('T', ' ')[:16]}."
        if info["last_run"]:
            ok = info["last_result"] in (0, None)
            text += f" Последний запуск: {info['last_run'].replace('T', ' ')[:16]}" + ("." if ok else f", код {info['last_result']:#x}.")
        state = scheduled.load_state()
        if state:
            text += f" Итог: {state['message']}" + (" (пропущено)" if state.get("skipped") else "") + f" [{state['time'].replace('T', ' ')[:16]}]."
        if info["program"] and not schedule.points_to_this_program(info):
            text += (f"\nВНИМАНИЕ: задача запускает другой файл ({info['program']}). Если программу перенесли, выключите и снова "
                     "включите ночной перезапуск, иначе он не сработает.")
        self.night_status.config(text=text)

    def _need_admin(self) -> bool:
        if portowner.is_admin():
            return False
        if messagebox.askyesno("Нужны права администратора",
                               "Создание и удаление задачи Планировщика требует прав администратора.\n"
                               "Перезапустить программу от администратора?"):
            portowner.relaunch_as_admin()
            self.app.destroy()
        return True

    def _apply_nightly(self, install: bool):
        time_text = self.night_time.get().strip()

        def work():
            return schedule.install(time_text) if install else schedule.remove()

        def done(msg, err):
            if err:
                messagebox.showerror("Ночной перезапуск", f"Не удалось изменить задачу:\n{err}")
            elif install:
                self.app.cfg["scheduled_restart_time"] = time_text
                self.app.save()
            self.refresh_nightly()
        self.app.bg(work, done)

    def on_nightly_toggle(self):
        want = self.night_on.get()
        if want and not schedule.valid_time(self.night_time.get()):
            messagebox.showwarning("Ночной перезапуск", "Укажите время в формате ЧЧ:ММ, например 05:00")
            self.night_on.set(False)
            return
        if self._need_admin():
            self.night_on.set(not want)
            return
        if want and not messagebox.askyesno(
                "Ночной перезапуск",
                f"Будет создана задача Планировщика Windows: каждый день в {self.night_time.get().strip()} перезапускать службы ЕСМ "
                "(Control Module, Local Module Controller, Orchestrator). Драйвер АТОЛ не затрагивается.\n\n"
                "Если в это время идут проверки марок, программа подождёт паузы до часа и при продажах пропустит эту ночь. "
                "Во время перезапуска (1–2 минуты) касса не сможет проверять марки.\n\nПродолжить?"):
            self.night_on.set(False)
            return
        self._apply_nightly(want)

    def on_nightly_time(self):
        if not schedule.valid_time(self.night_time.get()):
            messagebox.showwarning("Ночной перезапуск", "Укажите время в формате ЧЧ:ММ, например 05:00")
            return
        if not self.night_on.get():
            messagebox.showinfo("Ночной перезапуск", "Сначала включите ночной перезапуск галочкой.")
            return
        if not self._need_admin():
            self._apply_nightly(True)

    def test_nightly(self):
        """Репетиция: находит службы и смотрит, идут ли продажи, но ничего не перезапускает."""
        def work():
            lines: list[str] = []
            code = scheduled.run(dry_run=True, cfg=self.app.cfg, say=lines.append, lock=False)
            return code, lines

        def done(res, err):
            if err:
                messagebox.showerror("Проверка ночного перезапуска", str(err))
                return
            code, lines = res
            meaning = {0: "Всё готово: перезапуск пройдёт.", 3: "Сейчас идут продажи: в ночной запуск программа подождёт.",
                       2: "Службы не найдены: проверьте scheduled_restart_chain в config.json."}.get(code, f"Код результата {code}.")
            messagebox.showinfo("Проверка ночного перезапуска (службы не тронуты)", meaning + "\n\n" + "\n".join(lines))
        self.app.bg(work, done)

    # ----- правила -----

    def refresh_rules(self):
        info = knowledge.RULES_INFO
        source = "скачанные" if info["update"] else "встроенные"
        text = f"Версия правил: {info['version']} ({source}) · правил: {len(knowledge.RULES)}"
        if info["user"]:
            text += f" · ваших (rules_user.json): {info['user']}"
        if info["errors"]:
            text += "\nПроблемы в файлах правил: " + "; ".join(info["errors"][:3])
        self.rules_label.config(text=text)

    def reload_rules(self):
        knowledge.reload_rules()
        self.refresh_rules()

    def update_rules(self):
        self.update_btn.config(state="disabled")
        self.rules_label.config(text="Скачиваю правила с GitHub…")
        url = self.app.cfg.get("rules_url", knowledge.DEFAULT_RULES_URL)

        def done(msg, err):
            self.update_btn.config(state="normal")
            self.refresh_rules()
            if err:
                hint = ""
                if "CERTIFICATE_VERIFY_FAILED" in str(err):
                    hint = ("\n\nНа этом компьютере нет актуальных корневых сертификатов (так бывает на Windows 8.1 и старее "
                            "без обновлений). Установите обновления Windows либо скачайте rules.json вручную и положите "
                            "рядом с программой как rules_update.json.")
                messagebox.showerror("Обновление правил", f"Не удалось обновить правила:\n{err}{hint}")
            else:
                messagebox.showinfo("Обновление правил", msg)
        self.app.bg(lambda: knowledge.update_rules(url), done)
