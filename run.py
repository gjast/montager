"""Точка входа, когда программу запускает запускалка для Windows (launcher/launcher.py).

Запускалка держит программу в служебной папке и вызывает этот файл своим Python.
У того путь поиска модулей жёстко задан, поэтому папку программы и libs добавляем сами.
"""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# libs — быстро устаревающие библиотеки (yt-dlp и т. п.): приезжают вместе с программой
# и должны находиться раньше тех, что лежат в наборе с Python
sys.path[:0] = [str(HERE / "libs"), str(HERE)]

# сюда же пишут и сама программа, и запускалка: вывод может идти в файл, а не в консоль
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def add_node():
    """Node.js нужен yt-dlp для YouTube. Он приезжает пакетом nodejs-wheel-binaries,
    но в PATH не попадает — добавляем, если своего Node.js в системе нет."""
    if shutil.which("node"):
        return
    spec = importlib.util.find_spec("nodejs_wheel")
    if spec is None or not spec.origin:
        return
    root = Path(spec.origin).parent
    folder = root if os.name == "nt" else root / "bin"
    if (folder / ("node.exe" if os.name == "nt" else "node")).is_file():
        os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


def selfcheck():
    """Проверка сборки: всё ли на месте и загружается. Вызывается при выпуске новой версии."""
    import av
    import ctranslate2
    import faster_whisper
    import instaloader
    import onnxruntime
    import parth_dl
    import PIL
    import pytubefix
    import rich
    import yt_dlp

    import download
    import montage
    for name in ("ffmpeg", "ffprobe", "node"):
        if shutil.which(name) is None:
            sys.exit(f"selfcheck: не найден {name}")
    montage.video_encoder()
    # gallery-dl запускается отдельным процессом — проверяем, что он находит свои модули
    if download.gallery_run(["--version"], 60).returncode != 0:
        sys.exit("selfcheck: gallery-dl не запускается")
    print("selfcheck ok:", f"python {sys.version.split()[0]},",
          f"yt-dlp {yt_dlp.version.__version__},", f"faster-whisper {faster_whisper.__version__},",
          f"home {montage.HOME}")


if __name__ == "__main__":
    add_node()
    if sys.argv[1:] == ["--selfcheck"]:
        selfcheck()
    else:
        import montage
        montage.cli()
