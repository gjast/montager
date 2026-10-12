"""Запускалка Монтажёра для Windows — из неё собирается Montager.exe.

Самой программы в exe нет. При каждом запуске запускалка читает список текущих версий
(manifest.json из последнего выпуска на GitHub) и докачивает то, что изменилось:

  runtime — Python и тяжёлые библиотеки (меняется редко);
  ffmpeg  — ffmpeg и ffprobe (меняется редко);
  app     — сама программа и быстро устаревающие библиотеки вроде yt-dlp (с каждым выпуском).

Наборы лежат в служебной папке (%LOCALAPPDATA%\\Montager), ролики и настройки — рядом с exe.
Без интернета запускается то, что уже установлено. Если вышла новая запускалка, exe
заменяет сам себя и перезапускается.

Только стандартная библиотека: чем меньше в exe, тем реже его придётся менять.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

LAUNCHER_VERSION = 1
REPO = "gjast/montager"
MANIFEST_URL = f"https://github.com/{REPO}/releases/latest/download/manifest.json"

PACKS = ("runtime", "ffmpeg", "app")
TITLES = {"runtime": "Python и библиотеки", "ffmpeg": "ffmpeg", "app": "программа",
          "launcher": "запускалка"}
CHECK_TIMEOUT = 10    # сек, сколько ждать список версий; дольше — запускаем то, что есть
ATTEMPTS = 3


class Failure(Exception):
    pass


def frozen():
    return bool(getattr(sys, "frozen", False))


def home_dir():
    """Папка для роликов и настроек: рядом с exe; если туда нельзя писать — в папке пользователя."""
    given = os.environ.get("MONTAGER_HOME")
    home = Path(given) if given else Path(sys.executable if frozen() else __file__).resolve().parent
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        home = Path.home() / "Montager"
        home.mkdir(parents=True, exist_ok=True)
    return home


def store_dir():
    given = os.environ.get("MONTAGER_STORE")
    if given:
        store = Path(given)
    elif os.name == "nt":
        store = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Montager"
    else:
        store = Path.home() / ".montager"
    store.mkdir(parents=True, exist_ok=True)
    return store


def say(text=""):
    print(text, flush=True)


def open_url(url, timeout):
    # список версий можно подменить только через MONTAGER_MANIFEST (для проверки сборки);
    # всё остальное качаем строго по https
    if not url.startswith("https://") and not os.environ.get("MONTAGER_MANIFEST"):
        raise Failure(f"небезопасный адрес: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": f"Montager/{LAUNCHER_VERSION}"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_manifest():
    url = os.environ.get("MONTAGER_MANIFEST") or MANIFEST_URL
    with open_url(url, CHECK_TIMEOUT) as r:
        manifest = json.loads(r.read().decode("utf-8"))
    for name in PACKS:
        pack = manifest["packs"][name]
        if not all(isinstance(pack.get(k), str) and pack[k] for k in ("id", "url", "sha256")):
            raise ValueError(f"в списке версий нет данных о наборе {name}")
        if not pack["id"].replace("-", "").replace(".", "").isalnum():
            raise ValueError(f"странное имя набора {name}")
    return manifest


def download(url, dest, sha256, title):
    """Качает файл с проверкой контрольной суммы; при обрыве пробует ещё раз."""
    last = None
    for attempt in range(ATTEMPTS):
        try:
            digest = hashlib.sha256()
            with open_url(url, 60) as r, open(dest, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got, shown = 0, 0.0
                while True:
                    data = r.read(256 * 1024)
                    if not data:
                        break
                    f.write(data)
                    digest.update(data)
                    got += len(data)
                    if time.monotonic() - shown > 0.2:
                        shown = time.monotonic()
                        part = f"{got * 100 // total:3d} %  " if total else ""
                        print(f"\r  ↓ {title}: {part}{got / 1e6:.0f} МБ   ", end="", flush=True)
            print(f"\r  ↓ {title}: готово, {got / 1e6:.0f} МБ      ", flush=True)
            if digest.hexdigest() != sha256.lower():
                last = "файл повреждён при скачивании (не сошлась контрольная сумма)"
                continue
            return
        except (urllib.error.URLError, OSError) as e:
            print()
            last = e
        time.sleep(1 + attempt)
    Path(dest).unlink(missing_ok=True)
    raise Failure(f"не удалось скачать «{title}»: {last}")


def unpack(archive, dest):
    """Распаковывает архив в dest, не давая файлам выйти за его пределы."""
    root = dest.resolve()
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            target = (root / item.filename).resolve()
            if root != target and root not in target.parents:
                raise Failure(f"в архиве недопустимый путь: {item.filename}")
        z.extractall(root)


def load_state(store):
    try:
        state = json.loads((store / "state.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(store, state):
    tmp = store / "state.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, store / "state.json")


def pack_dir(store, name, state):
    """Папка установленного набора или None."""
    pack_id = state.get(name)
    if pack_id and (store / f"{name}-{pack_id}" / ".ready").is_file():
        return store / f"{name}-{pack_id}"
    return None


def install(store, name, pack, state):
    """Качает набор и ставит его рядом со старым; старый убирается после переключения."""
    tmp = store / "tmp"
    tmp.mkdir(exist_ok=True)
    archive = tmp / f"{name}.zip"
    download(pack["url"], archive, pack["sha256"], TITLES[name])
    target = store / f"{name}-{pack['id']}"
    shutil.rmtree(target, ignore_errors=True)
    try:
        unpack(archive, target)
        (target / ".ready").write_bytes(b"")     # метка: набор распакован целиком
    except (OSError, zipfile.BadZipFile) as e:
        shutil.rmtree(target, ignore_errors=True)
        raise Failure(f"не удалось распаковать «{TITLES[name]}»: {e}") from e
    finally:
        archive.unlink(missing_ok=True)
    state[name] = pack["id"]
    save_state(store, state)
    for old in store.glob(f"{name}-*"):
        if old != target:
            # набор может быть занят вторым открытым окном программы — уберётся в другой раз
            shutil.rmtree(old, ignore_errors=True)


def update_self(info):
    """Заменяет работающий exe новым и перезапускает его. Windows не даёт перезаписать
    запущенный файл, но даёт переименовать — старый exe отодвигаем в сторону."""
    exe = Path(sys.executable)
    new = exe.with_name(exe.stem + ".new" + exe.suffix)
    old = exe.with_name(exe.stem + ".old" + exe.suffix)
    download(info["url"], new, info["sha256"], TITLES["launcher"])
    old.unlink(missing_ok=True)
    os.replace(exe, old)
    try:
        os.replace(new, exe)
    except OSError:
        os.replace(old, exe)
        raise
    # MONTAGER_UPDATED не даёт новой запускалке обновляться ещё раз, если что-то пошло не так;
    # вторая переменная нужна PyInstaller, чтобы новый exe стартовал как самостоятельный
    env = dict(os.environ, MONTAGER_UPDATED="1", PYINSTALLER_RESET_ENVIRONMENT="1")
    sys.exit(wait(subprocess.Popen([str(exe), *sys.argv[1:]], env=env)))


def wait(proc):
    """Ждёт дочерний процесс. Ctrl+C получает и он тоже — выходить должен он, а не мы."""
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


def run(dirs, home, version):
    python = os.environ.get("MONTAGER_PYTHON") or str(
        dirs["runtime"] / ("python.exe" if os.name == "nt" else "bin/python3"))
    env = dict(os.environ)
    env["PATH"] = str(dirs["ffmpeg"]) + os.pathsep + env.get("PATH", "")
    env["MONTAGE_HOME"] = str(home)
    env["MONTAGER_VERSION"] = version
    env["PYTHONUTF8"] = "1"
    for key in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    if os.name == "nt" and frozen():
        # PyInstaller направляет поиск DLL в свою временную папку, и это наследуют
        # дочерние процессы — нашему Python чужие DLL ни к чему
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    return wait(subprocess.Popen([python, str(dirs["app"] / "run.py"), *sys.argv[1:]], env=env))


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    home, store = home_dir(), store_dir()
    state = load_state(store)
    shutil.rmtree(store / "tmp", ignore_errors=True)
    if frozen():
        exe = Path(sys.executable)
        try:
            exe.with_name(exe.stem + ".old" + exe.suffix).unlink(missing_ok=True)
        except OSError:
            pass

    say("Проверяю обновления...")
    try:
        manifest = fetch_manifest()
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, Failure) as e:
        manifest = None
        if not all(pack_dir(store, name, state) for name in PACKS):
            raise Failure("Для первого запуска нужен интернет: программа должна скачать "
                          f"свои файлы.\nНе удалось получить список версий: {e}") from e
        say("  нет связи с сервером обновлений — запускаю установленную версию")

    if manifest:
        newer = manifest.get("launcher") or {}
        if (frozen() and os.name == "nt" and not os.environ.get("MONTAGER_UPDATED")
                and isinstance(newer.get("version"), int) and newer["version"] > LAUNCHER_VERSION):
            try:
                update_self(newer)
            except (Failure, OSError) as e:
                say(f"  ! запускалку обновить не удалось ({e}) — продолжаю на прежней")
        fresh = True
        for name in PACKS:
            pack = manifest["packs"][name]
            if state.get(name) == pack["id"] and pack_dir(store, name, state):
                continue
            had = pack_dir(store, name, state)
            if fresh:
                say("Обновляю программу..." if had else "Первый запуск: скачиваю файлы программы...")
                fresh = False
            try:
                install(store, name, pack, state)
            except Failure as e:
                if not had:
                    raise
                say(f"  ! {e}\n    запускаю с прежней версией, обновление повторится в следующий раз")
        if state.get("version") != manifest.get("version"):
            state["version"] = manifest.get("version")
            save_state(store, state)

    dirs = {name: pack_dir(store, name, state) for name in PACKS}
    if not all(dirs.values()):
        raise Failure("Файлы программы установлены не полностью. Запусти ещё раз при "
                      "работающем интернете.")
    return run(dirs, home, str(state.get("version") or "?"))


if __name__ == "__main__":
    try:
        code = main()
    except Failure as e:
        say(f"\nНе получилось запустить программу.\n{e}")
        code = 1
    except KeyboardInterrupt:
        code = 0
    if code and sys.stdin and sys.stdin.isatty():
        # окно консоли закрывается сразу — даём прочитать, что случилось
        try:
            input("\nEnter — закрыть")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
