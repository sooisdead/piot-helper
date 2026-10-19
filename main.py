import sys

if __name__ == "__main__":
    # Служебные режимы (нужны собранному exe: запускает сам себя вместо python):
    #   --handles [--out файл] COM5 …   поиск процессов, держащих COM-порт
    #   --selftest --out файл           самопроверка без окна
    #   --tray                          запуск из автозапуска: окно свёрнуто в значок (если включена фоновая проверка)
    try:
        if sys.argv[1:2] == ["--handles"]:
            from piothelper import handles
            handles.main(sys.argv[2:])
        elif sys.argv[1:2] == ["--selftest"]:
            from piothelper import selftest
            selftest.run(sys.argv[sys.argv.index("--out") + 1])
        else:
            from piothelper import errorlog
            errorlog.install_hooks()
            from piothelper.gui import run
            run(start_hidden="--tray" in sys.argv[1:])
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — любая ошибка при запуске должна быть видна
        from piothelper import errorlog
        errorlog.fatal(exc)
