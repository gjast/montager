"""Собирает выпуск для Windows: Montager.exe, три набора (runtime, ffmpeg, app) и manifest.json.

Запускается в GitHub Actions на Windows (см. .github/workflows/release.yml) тем же Python,
какой кладётся в набор runtime: библиотеки ставятся его pip-ом и подходят именно ему.

    python tools/build_release.py --out dist --version 2026.10.13.5 --base-url https://.../v5

Набор app собирается и на Mac — так проверяется запускалка: --only app --no-libs.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = ["montage.py", "download.py", "run.py"]
# эти библиотеки тяжёлые и меняются редко — едут в runtime; всё остальное из requirements.txt
# (yt-dlp, pytubefix и т. п.) устаревает быстро и едет вместе с программой, в app/libs
HEAVY = {"faster-whisper", "pillow", "rich"}
# Node.js нужен pytubefix и yt-dlp, весит много, а меняется редко — тоже в runtime
RUNTIME_EXTRA = ["nodejs-wheel-binaries"]
# поднять число, чтобы все заново скачали набор, хотя список библиотек не менялся
RUNTIME_EPOCH = 1
FFMPEG_EPOCH = 1
FFMPEG_REPO = "BtbN/FFmpeg-Builds"
# библиотеки Visual C++, без которых на чистой Windows не загрузятся onnxruntime и ctranslate2
VC_DLLS = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll",
           "vcruntime140_1.dll", "concrt140.dll", "vcomp140.dll"]


def log(text):
    print(f"[build] {text}", flush=True)


def run(cmd):
    log(" ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def fetch(url, dest=None):
    headers = {"User-Agent": "montager-build"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    return dest


def requirements():
    """(тяжёлые, быстро устаревающие) — строки из requirements.txt."""
    heavy, light = [], []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0].lower().replace("_", "-")
        (heavy if name in HEAVY else light).append(line)
    return heavy, light


def short_hash(*parts):
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def tree_hash(folder):
    """Отпечаток содержимого папки: одинаковое содержимое — одинаковое имя набора,
    и тем, у кого он уже стоит, не придётся качать его заново."""
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:12]


def make_zip(folder, archive):
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            z.write(path, path.relative_to(folder).as_posix())
    sha = hashlib.sha256()
    with open(archive, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    log(f"{archive.name}: {archive.stat().st_size / 1e6:.1f} МБ")
    return {"file": archive.name, "sha256": sha.hexdigest(), "size": archive.stat().st_size}


def drop_caches(folder):
    for cache in list(folder.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)


# ---------- app: программа и быстро устаревающие библиотеки ----------
def build_app(work, out, libs):
    app = work / "app"
    app.mkdir(parents=True)
    for name in APP_FILES:
        shutil.copy2(ROOT / name, app / name)
    if libs:
        _, light = requirements()
        run([sys.executable, "-m", "pip", "install", "--no-warn-script-location",
             "--target", app / "libs", *light])
        # Node.js лежит в runtime, второй экземпляр на сотню мегабайт тут не нужен
        for extra in list((app / "libs").glob("nodejs_wheel*")) + [app / "libs" / "bin"]:
            shutil.rmtree(extra, ignore_errors=True)
    drop_caches(app)
    return {"id": tree_hash(app), **make_zip(app, out / "app.zip")}


# ---------- runtime: Python и тяжёлые библиотеки ----------
def embed_python(work):
    """Скачивает встраиваемый Python той же ветки, что и запущенный. У самой свежей
    версии сборки на python.org может ещё не быть — тогда берём предыдущую."""
    major, minor, patch = sys.version_info[:3]
    for p in range(patch, -1, -1):
        version = f"{major}.{minor}.{p}"
        url = f"https://www.python.org/ftp/python/{version}/python-{version}-embed-amd64.zip"
        try:
            fetch(url, work / "python-embed.zip")
            log(f"встраиваемый Python {version}")
            return work / "python-embed.zip"
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    sys.exit(f"нет встраиваемого Python {major}.{minor} на python.org")


def build_runtime(work, out):
    heavy, _ = requirements()
    runtime = work / "runtime"
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(embed_python(work)) as z:
        z.extractall(runtime)
    # у встраиваемого Python пути поиска модулей заданы файлом ._pth; добавляем site-packages
    pth = next(runtime.glob("python*._pth"))
    stdlib = next(runtime.glob("python*.zip")).name
    pth.write_text(f"{stdlib}\n.\nLib\\site-packages\nimport site\n", encoding="utf-8")
    site = runtime / "Lib" / "site-packages"
    run([sys.executable, "-m", "pip", "install", "--no-warn-script-location",
         "--target", site, *heavy, *RUNTIME_EXTRA])
    shutil.rmtree(site / "bin", ignore_errors=True)
    # от Node.js нужен только сам node.exe
    for extra in ("include", "node_modules", "share"):
        shutil.rmtree(site / "nodejs_wheel" / extra, ignore_errors=True)
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    for name in VC_DLLS:
        if (system / name).is_file() and not (runtime / name).is_file():
            shutil.copy2(system / name, runtime / name)
    drop_caches(runtime)
    pack_id = short_hash(f"{sys.version_info[0]}.{sys.version_info[1]}",
                         *sorted(heavy + RUNTIME_EXTRA), str(RUNTIME_EPOCH))
    return {"id": pack_id, **make_zip(runtime, out / "runtime.zip")}


# ---------- ffmpeg ----------
def build_ffmpeg(work, out):
    release = json.loads(fetch(f"https://api.github.com/repos/{FFMPEG_REPO}/releases/latest"))
    # сборки стабильных веток называются ffmpeg-n8.0-latest-win64-gpl-shared-8.0.zip;
    # shared — общие DLL на ffmpeg и ffprobe, так набор вдвое меньше
    found = []
    for asset in release["assets"]:
        m = re.fullmatch(r"ffmpeg-n([\d.]+)-latest-win64-gpl-shared-[\d.]+\.zip", asset["name"])
        if m:
            found.append((tuple(int(x) for x in m.group(1).split(".")), m.group(1), asset))
    if not found:
        sys.exit(f"в выпуске {FFMPEG_REPO} нет подходящей сборки ffmpeg")
    _, branch, asset = max(found, key=lambda x: x[0])
    log(f"ffmpeg {branch}: {asset['name']}")
    archive = fetch(asset["browser_download_url"], work / "ffmpeg-src.zip")
    ffmpeg = work / "ffmpeg"
    ffmpeg.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            parts = Path(item.filename).parts
            if item.is_dir() or len(parts) < 3 or parts[1] != "bin" or parts[-1] == "ffplay.exe":
                continue
            (ffmpeg / parts[-1]).write_bytes(z.read(item))
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        if not (ffmpeg / name).is_file():
            sys.exit(f"в сборке ffmpeg нет {name}")
    return {"id": short_hash(branch, str(FFMPEG_EPOCH)), **make_zip(ffmpeg, out / "ffmpeg.zip")}


# ---------- запускалка ----------
def launcher_version():
    text = (ROOT / "launcher" / "launcher.py").read_text(encoding="utf-8")
    return int(re.search(r"^LAUNCHER_VERSION = (\d+)$", text, re.M).group(1))


def build_launcher(work, out):
    # запускалка ищет обновления в репозитории, записанном в ней самой — он должен быть этим
    text = (ROOT / "launcher" / "launcher.py").read_text(encoding="utf-8")
    repo = re.search(r'^REPO = "([^"]+)"$', text, re.M).group(1)
    here = os.environ.get("GITHUB_REPOSITORY")
    if here and here.lower() != repo.lower():
        sys.exit(f"в launcher.py указан репозиторий {repo}, а сборка идёт в {here}")
    run([sys.executable, "-m", "PyInstaller", "--onefile", "--console", "--clean", "--noconfirm",
         "--name", "Montager", "--icon", ROOT / "launcher" / "icon.ico",
         "--distpath", out, "--workpath", work / "pyinstaller",
         "--specpath", work, ROOT / "launcher" / "launcher.py"])
    exe = out / "Montager.exe"
    return {"version": launcher_version(), "file": exe.name, "size": exe.stat().st_size,
            "sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}


def write_manifest(out, version, base_url, name="manifest.json", launcher_version=None):
    """manifest.json из packs.json. Адреса файлов привязаны к конкретному выпуску, чтобы
    запускалка не получила список от одного выпуска, а файлы — от другого."""
    built = json.loads((out / "packs.json").read_text(encoding="utf-8"))
    base = base_url.rstrip("/") + "/"

    def entry(pack):
        return {"url": base + pack["file"], "sha256": pack["sha256"], "size": pack["size"]}

    manifest = {"version": version,
                "packs": {n: {"id": p["id"], **entry(p)} for n, p in built["packs"].items()}}
    if built.get("launcher"):
        manifest["launcher"] = {"version": launcher_version or built["launcher"]["version"],
                                **entry(built["launcher"])}
    (out / name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"{name}: версия {version}, " + ", ".join(f"{n} {p['id']}" for n, p in manifest["packs"].items()))
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="dist")
    ap.add_argument("--version", required=True, help="версия выпуска, показывается в программе")
    ap.add_argument("--base-url", required=True, help="откуда будут скачиваться файлы выпуска")
    ap.add_argument("--only", nargs="*", choices=["app", "runtime", "ffmpeg", "launcher"],
                    help="собрать только это (по умолчанию всё)")
    ap.add_argument("--no-libs", action="store_true", help="app без библиотек (для проверки)")
    ap.add_argument("--manifest-only", action="store_true",
                    help="только переписать manifest.json из уже собранного packs.json")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    if a.manifest_only:
        write_manifest(out, a.version, a.base_url)
        return
    work = out / "_build"
    shutil.rmtree(out, ignore_errors=True)
    work.mkdir(parents=True)
    todo = a.only or ["app", "runtime", "ffmpeg", "launcher"]
    built = {"packs": {}}
    if "app" in todo:
        built["packs"]["app"] = build_app(work, out, not a.no_libs)
    if "runtime" in todo:
        built["packs"]["runtime"] = build_runtime(work, out)
    if "ffmpeg" in todo:
        built["packs"]["ffmpeg"] = build_ffmpeg(work, out)
    if "launcher" in todo:
        built["launcher"] = build_launcher(work, out)
    (out / "packs.json").write_text(json.dumps(built, indent=2), encoding="utf-8")
    write_manifest(out, a.version, a.base_url)


if __name__ == "__main__":
    main()
