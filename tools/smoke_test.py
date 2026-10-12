"""Проверка собранного выпуска на настоящей Windows — перед публикацией, в GitHub Actions.

Поднимает локальный «сервер обновлений» из папки dist и запускает Montager.exe так же,
как это сделает пользователь: установка с нуля, монтаж ролика, распознавание речи,
обновление самой запускалки.

    python tools/smoke_test.py dist      (manifest.json должен быть собран с --base-url
                                          http://127.0.0.1:8765)
"""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def main():
    dist = Path(sys.argv[1]).resolve()
    # resolve: временная папка на Windows приходит в коротком виде (RUNNER~1),
    # а программа показывает полный путь
    work = Path(tempfile.mkdtemp(prefix="montager_smoke_")).resolve()
    home, store = work / "home", work / "store"
    home.mkdir()
    exe = home / "Montager.exe"
    shutil.copy2(dist / "Montager.exe", exe)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(dist))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def launch(*args, manifest="manifest.json"):
        env = dict(os.environ, MONTAGER_MANIFEST=f"{BASE}/{manifest}", MONTAGER_STORE=str(store))
        env.pop("MONTAGER_HOME", None)
        print(f"\n=== Montager.exe {' '.join(args)}  [{manifest}]", flush=True)
        r = subprocess.run([str(exe), *args], env=env, stdin=subprocess.DEVNULL, timeout=1500,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace")
        print(out.replace("\r", "\n"), flush=True)
        return r.returncode, out

    def expect(ok, what):
        print(("PASS " if ok else "FAIL ") + what, flush=True)
        if not ok:
            sys.exit(1)

    # 1. установка с нуля: всё качается, библиотеки загружаются, ffmpeg и node на месте
    code, out = launch("--selfcheck")
    expect(code == 0 and "selfcheck ok" in out, "установка с нуля и загрузка всех библиотек")
    expect(str(home).lower() in out.lower(), "ролики и настройки — рядом с exe")

    # 2. монтаж ролика (ffmpeg берём тот, что поставила запускалка)
    state = json.loads((store / "state.json").read_text(encoding="utf-8"))
    ffmpeg_dir = store / f"ffmpeg-{state['ffmpeg']}"
    clip = work / "clip.mp4"
    subprocess.run([str(ffmpeg_dir / "ffmpeg.exe"), "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True)

    def size_of(path):
        r = subprocess.run([str(ffmpeg_dir / "ffprobe.exe"), "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True)
        return r.stdout.strip()

    out1 = work / "out1.mp4"
    code, out = launch(str(clip), "--no-subs", "-j", "2", "-o", str(out1))
    expect(code == 0 and out1.is_file() and size_of(out1) == "1080,1920", "монтаж ролика 1080×1920")

    # 3. распознавание речи: маленькая модель, чтобы проверить сам механизм
    out2 = work / "out2.mp4"
    code, out = launch(str(clip), "--model", "tiny", "-j", "2", "-o", str(out2))
    expect(code == 0 and out2.is_file() and "не удалось" not in out, "распознавание речи запускается")

    # 4. повторный запуск ничего не качает
    code, out = launch("--selfcheck")
    expect(code == 0 and "↓" not in out, "повторный запуск без скачиваний")

    # 5. обновление запускалки: объявляем, что вышла версия новее
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    manifest["launcher"]["version"] = 999
    (dist / "manifest_newer.json").write_text(json.dumps(manifest), encoding="utf-8")
    old = home / "Montager.old.exe"
    code, out = launch("--selfcheck", manifest="manifest_newer.json")
    expect(code == 0 and "запускалка" in out and "selfcheck ok" in out and exe.is_file(),
           "запускалка заменила себя и перезапустилась")
    expect(old.is_file(), "прежний exe отложен в сторону")
    code, out = launch("--selfcheck")
    expect(code == 0 and not old.is_file(), "прежний exe убран при следующем запуске")

    server.shutdown()
    # 6. без сети запускается установленное
    code, out = launch("--selfcheck")
    expect(code == 0 and "selfcheck ok" in out, "без сети запускается установленная версия")
    print("\nвсе проверки пройдены", flush=True)


if __name__ == "__main__":
    main()
