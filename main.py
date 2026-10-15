import sys

if __name__ == "__main__":
    # Служебные режимы (нужны собранному exe: запускает сам себя вместо python):
    #   --handles [--out файл] COM5 …   поиск процессов, держащих COM-порт
    #   --selftest --out файл           самопроверка без окна
    if sys.argv[1:2] == ["--handles"]:
        from piothelper import handles
        handles.main(sys.argv[2:])
    elif sys.argv[1:2] == ["--selftest"]:
        from piothelper import selftest
        selftest.run(sys.argv[sys.argv.index("--out") + 1])
    else:
        from piothelper.gui import run
        run()
