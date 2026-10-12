#!/usr/bin/env python3
"""Монтажёр шортсов/тиктоков.

Видео 1 — сверху, задаёт длину ролика и звук, по нему делаются субтитры.
Видео 2 — снизу, без звука (зацикливается, если короче); без него видео 1 занимает весь кадр.
Музыка — необязательный фон под весь ролик.
Баннер (видео 3) — одно из двух:
  заставка — видео, показывается каждые 30 секунд; на это время фон темнеет, а звук ролика замолкает;
  постоянный баннер — картинка или видео по кругу, виден весь ролик внизу по центру.
Зелёный фон у баннера без прозрачности убирается сам.

Ролик режется на куски, которые рендерятся параллельно, потом склеиваются.
"""
import argparse
import json
import math
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                           TextColumn, TimeElapsedColumn, TimeRemainingColumn)
from rich.table import Table

import download

# ---------- настройки ----------
W, H = 1080, 1920
FPS = 30

INSERT_EVERY = 30     # сек, как часто показывать видео 3
DUCK = 0.0            # громкость фона (видео 1 + музыка), пока играет видео 3: 0 — тишина
V3_VOLUME = 1.5       # громкость видео 3
DUCK_RAMP = 0.25      # сек, плавность приглушения
MUSIC_VOLUME = 0.2    # громкость музыки относительно исходной
MUSIC_FADE = 1.5      # сек, затухание музыки в конце ролика

WHISPER_MODEL = "large-v3-turbo"

SUB_H = 180           # высота полосы субтитров
SUB_CENTER_Y = 1060   # центр текста (чуть ниже середины кадра)
SUB_MAX_W = 1000
SUB_FONT_SIZE = 96
SUB_STROKE = 10
SUB_OUTLINE = (255, 255, 255, 255)   # белая обводка
SUB_SHADOW = 170      # плотность тёмной тени вокруг обводки (0–255), чтобы читалось на светлом
# цвета букв: (обычные слова, произносимое слово) — для каждого ролика случайная пара
SUB_PAIRS = [
    ((25, 95, 235), (235, 35, 50)),     # синий / красный
    ((130, 50, 220), (240, 110, 0)),    # фиолетовый / оранжевый
    ((0, 140, 90), (225, 30, 140)),     # зелёный / розовый
    ((225, 30, 60), (25, 95, 235)),     # красный / синий
    ((235, 95, 0), (130, 50, 220)),     # оранжевый / фиолетовый
    ((0, 130, 200), (240, 90, 0)),      # голубой / оранжевый
]
SUB_MAX_WORDS = 3
SUB_MAX_CHARS = 16
SUB_GAP = 0.6         # пауза в речи, после которой начинается новая фраза

V3_W = 1000           # ширина содержимого видео 3 в кадре
V3_MAX_H = 640
V3_TOP = 1160         # верхний край содержимого видео 3
V3_FADE = 0.3         # появление/исчезновение, если у видео 3 нет прозрачности
V3_DIM = 0.5          # насколько затемнять фон, пока идёт видео 3 (0 — не затемнять)

# постоянный баннер: картинка или видео по кругу, виден весь ролик, по центру снизу
BANNER_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
BAN_W = 760           # ширина картинки в кадре (кадр — 1080)
BAN_MAX_H = 400
BAN_BOTTOM = 150      # отступ снизу
BAN_OPACITY = 1.0     # прозрачность баннера (1 — непрозрачный)
BAN_FLOAT_X = 0       # размах покачивания вбок, пикселей (0 — баннер стоит на месте)
BAN_FLOAT_Y = 0       # размах покачивания вверх-вниз
BAN_FLOAT_TX = 6.5    # сек, период качания вбок
BAN_FLOAT_TY = 4.3    # сек, период качания вверх-вниз
DIM_FADE = 0.3        # сек, плавность затемнения

# зелёный фон у баннера и заставки без прозрачности убирается сам (хромакей)
CHROMA = True
CHROMA_SIMILARITY = 0.16   # какие оттенки считать фоном: больше — вырезается смелее
CHROMA_BLEND = 0.06        # мягкость края

BAR_FILL = 0.2        # строка/столбец, где светлых точек меньше этой доли, — чёрное поле

PARTICLES = 46        # сколько частиц летает поверх ролика (0 — выключить)
FX_SCALE = 2          # частицы рисуются в уменьшенном кадре и растягиваются
FX_LOOP = 12          # сек, длина зацикленного клипа с частицами

MAX_JOBS = 4          # сколько кусков рендерить одновременно
MIN_PART = 6          # сек, меньше этого куски не делаем
STALL_TIMEOUT = 120   # сек без движения — считаем, что ffmpeg завис
VIDEO_BITRATE = "8M"

FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "C:/Windows/Fonts/impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# готовые ролики и настройки лежат рядом с программой; запускалка для Windows (Montager.exe)
# держит саму программу в служебной папке, которая меняется при обновлении, и указывает
# постоянную папку через MONTAGE_HOME
HOME = Path(os.environ.get("MONTAGE_HOME") or Path(__file__).resolve().parent)
OUT_DIR = HOME / "output"
SETTINGS_FILE = HOME / "settings.json"

console = Console(highlight=False)


class MontageError(Exception):
    pass


# ---------- ввод ----------
def parse_paths(line):
    try:
        parts = shlex.split(line, posix=os.name != "nt")
    except ValueError:
        parts = [line.strip()]
    return [p.strip("'\"") for p in parts if p.strip()]


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(**values):
    data = load_settings()
    data.update(values)
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def banner_key(always):
    return "banner_always" if always else "banner_video"


def remembered(key):
    """Прежний файл для этого вида баннера, если он ещё на месте."""
    data = load_settings()
    # с прошлых версий: баннер был один, потом картинка хранилась как banner_image
    old = data.get("banner")
    if old and not data.get(banner_key(is_image(old))):
        data[banner_key(is_image(old))] = old
    if data.get("banner_image") and not data.get("banner_always"):
        data["banner_always"] = data["banner_image"]
    path = data.get(key)
    return path if path and Path(path).is_file() else None


def ask_banner():
    """Выбор баннера. Возвращает (файл, постоянный ли он) или (None, False).
    Файл для каждого вида помнится отдельно — Enter берёт прежний."""
    console.print("[bold cyan]▸ Баннер[/] [dim]·[/] что показывать")
    console.print("  [bold cyan]1[/]  заставка [dim]— видео, выезжает раз в N секунд со звуком[/]")
    console.print("  [bold cyan]2[/]  постоянный баннер [dim]— картинка или видео, виден весь ролик[/]")
    console.print("  [bold cyan]-[/]  без баннера")
    console.print("  [dim]зелёный фон убирается сам[/]")
    while True:
        choice = console.input("  ").strip()
        if choice in ("-", "–", "—", ""):
            return None, False
        if choice not in ("1", "2"):
            console.print("  [red]✗ выбери 1, 2 или минус[/]")
            continue
        always = choice == "2"
        title, what = ("Баннер", "картинку или видео") if always else ("Заставка", "видео")
        old = remembered(banner_key(always))
        hint = f"Enter — прежний: {Path(old).name}; минус — назад" if old else "минус — назад"
        while True:
            line = console.input(f"[bold cyan]▸ {title}[/] [dim]·[/] перетащи {what} "
                                 f"[dim]({hint})[/]\n  ").strip()
            if not line and old:
                console.print(f"  [green]✓[/] баннер: {Path(old).name}")
                return old, always
            if line in ("-", "–", "—"):
                break
            if not line:
                continue
            paths = parse_paths(line)
            path = paths[0] if paths else ""
            if not Path(path).is_file():
                console.print(f"  [red]✗ файл не найден:[/] {path or line}")
                continue
            if not always and is_image(path):
                console.print("  [red]✗ заставке нужно видео; картинку можно поставить "
                              "постоянным баннером (минус — назад)[/]")
                continue
            save_settings(**{banner_key(always): str(Path(path).resolve())})
            console.print(f"  [green]✓[/] баннер: {Path(path).name}")
            return path, always
        return ask_banner()


def ask_files():
    """Спрашивает файлы по очереди; несколько перетащенных сразу раскладываются по порядку.
    Возвращает (видео 1, видео 2, баннер, музыка, постоянный ли баннер);
    необязательные файлы могут быть None."""
    slots = [
        ("Видео 1", "верх — звук, субтитры и длина ролика", None),
        ("Видео 2", "низ — без звука", "Enter — без него"),
        ("Баннер", None, None),
        ("Музыка", "фоном под ролик", "Enter — без неё"),
    ]
    files, queue, always = [], [], False
    while len(files) < len(slots):
        title, role, hint = slots[len(files)]
        is_banner = len(files) == 2
        if is_banner and not queue:
            path, always = ask_banner()
            files.append(path)
            continue
        if not queue:
            tail = f" [dim]({hint})[/]" if hint else ""
            line = console.input(f"[bold cyan]▸ {title}[/] [dim]·[/] {role}{tail}\n  ").strip()
            if not line:
                if hint:
                    files.append(None)
                continue
            if line in ("-", "–", "—") and hint:
                files.append(None)
                continue
            queue = parse_paths(line)
        path = queue.pop(0)
        if Path(path).is_file():
            files.append(path)
            console.print(f"  [green]✓[/] {title.lower()}: {Path(path).name}")
            if is_banner:
                # все файлы перетащили разом, вопроса не было: картинка — постоянный баннер,
                # видео — заставка
                always = is_image(path)
                save_settings(**{banner_key(always): str(Path(path).resolve())})
        else:
            console.print(f"  [red]✗ файл не найден:[/] {path}")
            queue = []
    return (*files, always)


def ask_every(default):
    while True:
        s = console.input(
            f"[bold cyan]▸ Баннер[/] [dim]·[/] раз в сколько секунд показывать заставку "
            f"[dim](Enter — {default})[/]\n  ").strip()
        if not s:
            return default
        if s.isdigit() and int(s) > 0:
            return int(s)
        console.print("  [red]✗ нужно целое число секунд, например 30[/]")


def ask_subs():
    s = console.input("[bold cyan]▸ Субтитры[/] [dim]·[/] распознать речь и добавить текст "
                      "[dim](Enter — да, минус — без субтитров)[/]\n  ").strip().lower()
    return s not in ("-", "–", "—", "n", "н", "нет", "no")


def ask_action():
    console.rule(style="dim")
    console.print("  [bold cyan]1[/]  Смонтировать ролик")
    console.print("  [bold cyan]2[/]  Скачать видео по ссылке")
    while True:
        s = console.input("[bold cyan]▸ Что делаем?[/] [dim](1 или 2)[/]\n  ").strip()
        if s in ("1", "2"):
            return "montage" if s == "1" else "download"


def retry_or_quit(error):
    """Показывает ошибку и спрашивает, пробовать ли ещё раз."""
    console.print(Panel(f"[red]{error}[/]", title="Не получилось", title_align="left",
                        border_style="red"))
    s = console.input("[bold cyan]▸ Попробовать ещё раз?[/] "
                      "[dim](Enter — да, минус — в меню)[/]\n  ").strip().lower()
    return s not in ("-", "–", "—", "n", "н", "нет", "no")


def download_note(text):
    console.print(f"  [yellow]![/] [dim]{text}[/]")


def fetch_video(url):
    with console.status("Получаю данные о видео..."):
        return download.get_video(url, on_note=download_note)


def read_links():
    """Ссылки — одной строкой через пробел или столбиком. Строки, вставленные разом,
    дочитываются сразу: иначе остаток ушёл бы ответом на следующий вопрос."""
    text = console.input(
        "[bold cyan]▸ Ссылки на видео[/] [dim](одна или несколько — через пробел или с новой "
        "строки; YouTube, Instagram, TikTok — источник определится сам)[/]\n  ")
    if os.name != "nt" and sys.stdin.isatty():
        import select
        while select.select([sys.stdin], [], [], 0.15)[0]:
            line = sys.stdin.readline()
            if not line:
                break
            text += "\n" + line
    links = []
    for word in re.split(r"[\s,;]+", text):
        if "." in word and word not in links:
            links.append(word)
    return links


def show_video(video, prefix=""):
    length = video["length"]
    site = download.SOURCE_NAMES.get(video["source"])
    console.print(f"  {prefix}[green]✓[/] {video['title']}  [dim]"
                  + (f"{site} · " if site else "")
                  + (f"{download.fmt_clock(length)}, " if length else "")
                  + f"{video['resolution']}, через {video['engine']}[/]")


def download_video(video, start, end):
    """Скачивание с полосой прогресса. Возвращает путь к файлу."""
    progress = Progress(
        SpinnerColumn(finished_text="[green]✓[/]"),
        TextColumn("{task.description:<20}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TextColumn("[dim]прошло[/]"), TimeElapsedColumn(),
        TextColumn("[dim]осталось[/]"), TimeRemainingColumn(),
        console=console,
    )
    with progress:
        task = progress.add_task("Скачивание", total=None)
        return download.download(
            video, start, end,
            lambda got, total: progress.update(task, completed=got, total=total),
            lambda text: progress.console.print(f"  [yellow]![/] [dim]{text}[/]"))


def download_flow():
    """Скачивание видео: одна ссылка — можно выбрать отрезок, несколько — все целиком."""
    links = []
    while not links:
        links = read_links()
    if len(links) > 1:
        return download_many(links)
    url = links[0]

    while True:
        try:
            video = fetch_video(url)
            break
        except download.DownloadError as e:
            if not retry_or_quit(e):
                return None
    length = video["length"]
    show_video(video)

    start, end = 0, length
    while length:
        s = console.input(
            "[bold cyan]▸ Какой отрезок скачать?[/] "
            "[dim]например 0:30-1:45 (Enter — целиком)[/]\n  ").strip()
        if not s:
            break
        try:
            start, end = download.parse_range(s, length)
            break
        except ValueError:
            console.print("  [red]✗ формат: начало-конец, например 12:30-15:00 "
                          f"или 1:02:30-1:05:00; длина видео {download.fmt_clock(length)}[/]")
    console.print()

    while True:
        started = time.monotonic()
        try:
            out = download_video(video, start, end)
            break
        except download.DownloadError as e:
            if not retry_or_quit(e):
                return None
            # ссылки на потоки живут недолго — перед новой попыткой берём свежие
            try:
                video = fetch_video(url)
            except download.DownloadError:
                pass
    size = out.stat().st_size / 1024 / 1024
    console.print(Panel(
        f"[bold green]Скачано[/] за {fmt_time(time.monotonic() - started)}  [dim]·[/]  "
        f"{size:.0f} МБ\n{out}", border_style="green"))
    return out


def download_many(links):
    """Несколько ссылок: качаются по очереди и целиком. Ошибка на одной остальные
    не останавливает — неудачные в конце можно повторить."""
    console.print(f"  [dim]ссылок: {len(links)} — качаю по очереди, каждое видео целиком[/]\n")
    started = time.monotonic()
    done, todo = [], links
    while todo:
        failed = []
        for n, url in enumerate(todo, 1):
            mark = f"[dim]{n}/{len(todo)}[/] "
            try:
                video = fetch_video(url)
                show_video(video, mark)
                done.append(download_video(video, 0, video["length"]))
            except download.DownloadError as e:
                failed.append(url)
                # в пакете ошибку показываем коротко: по строке на причину
                reason = [" ".join(x.split()) for x in str(e).splitlines() if x.strip()] or ["ошибка"]
                console.print(f"  {mark}[red]✗[/] {url}\n      [red]{reason[0][:160]}[/]"
                              + "".join(f"\n      [dim]{x[:160]}[/]" for x in reason[1:]))
        size = sum(f.stat().st_size for f in done) / 1024 / 1024
        console.print(Panel(
            f"[bold {'green' if not failed else 'yellow'}]Скачано {len(done)} из {len(links)}[/] "
            f"за {fmt_time(time.monotonic() - started)}  [dim]·[/]  {size:.0f} МБ\n"
            f"{download.DOWNLOAD_DIR}"
            + "".join(f"\n[dim]•[/] {f.name}" for f in done)
            + "".join(f"\n[red]✗[/] {u}" for u in failed),
            border_style="green" if not failed else "yellow"))
        if not failed:
            break
        s = console.input(f"[bold cyan]▸ Повторить неудачные ({len(failed)})?[/] "
                          "[dim](Enter — да, минус — в меню)[/]\n  ").strip().lower()
        if s in ("-", "–", "—", "n", "н", "нет", "no"):
            break
        todo = failed
    return done


def fmt_time(sec):
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


# ---------- ffprobe / ffmpeg ----------
def probe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise MontageError(f"не удалось прочитать файл: {Path(path).name}")
    info = json.loads(r.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise MontageError(f"в файле нет видео: {Path(path).name}")
    audio = any(s["codec_type"] == "audio" for s in info["streams"])
    duration = float(video.get("duration") or info["format"]["duration"])
    pix = video.get("pix_fmt", "")
    alpha = bool(re.search(r"yuva|rgba|bgra|argb|abgr|gbrap|ya\d", pix))
    return {"duration": duration, "audio": audio, "alpha": alpha,
            "w": int(video["width"]), "h": int(video["height"])}


def key_filters(key):
    """Фильтры, делающие зелёный фон прозрачным. Маска сужается на пиксель: по самому
    краю рисунка иначе остаётся зелёная кайма. Цвета самого рисунка не трогаются
    (despill, которым обычно убирают кайму, перекрашивает жёлтое и салатовое)."""
    return (f"format=yuva444p,chromakey={key}:{CHROMA_SIMILARITY}:{CHROMA_BLEND},"
            "erosion=threshold0=0:threshold1=0:threshold2=0")


def cutout(key, sw, sh):
    """Фильтры, приводящие баннер к размеру в кадре; с key — ещё и вырезающие фон.
    Вырезанное уменьшаем с умножением на прозрачность, чтобы зелень уже невидимых
    точек не подмешалась в края."""
    if not key:
        return f"scale={sw}:{sh},format=yuva420p"
    return (f"{key_filters(key)},premultiply=inplace=1,scale={sw}:{sh},"
            "unpremultiply=inplace=1,format=yuva420p")


def green_screen(path, duration=None):
    """Цвет фона ('0xRRGGBB'), если кадр по краям залит зелёным, иначе None.
    duration — длина видео; у картинки не задаётся. У видео смотрим начало, середину
    и конец: выезжающая заставка в середине может закрывать собой края кадра."""
    if not CHROMA:
        return None
    import numpy as np
    sw, sh, m = 256, 144, 6                  # m — ширина рамки, по которой судим о фоне
    times = [None] if duration is None else [0, duration / 2, max(0.0, duration - 0.1)]
    for at in times:
        seek = ["-ss", f"{at:.3f}"] if at else []
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", *seek, "-i", path, "-frames:v", "1",
             "-vf", f"scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True).stdout
        if len(raw) != sw * sh * 3:
            continue
        frame = np.frombuffer(raw, np.uint8).reshape(sh, sw, 3).astype(int)
        edge = np.concatenate([frame[:m].reshape(-1, 3), frame[-m:].reshape(-1, 3),
                               frame[m:-m, :m].reshape(-1, 3), frame[m:-m, -m:].reshape(-1, 3)])
        r, g, b = (int(x) for x in np.median(edge, axis=0))
        # рисунок может касаться края кадра, поэтому фоном должна быть большая часть рамки, не вся
        same = (np.abs(edge - (r, g, b)).max(axis=1) < 40).mean()
        if same >= 0.6 and g >= 80 and g - max(r, b) >= 50:
            return f"0x{r:02X}{g:02X}{b:02X}"
    return None


def opaque_bbox(path, info, image, key=None, whole=False):
    """Границы видимой части баннера: (x, y, ширина, высота).
    У видео смотрим кадр из середины, а с whole — весь ролик (движущийся рисунок
    должен помещаться целиком)."""
    if image:
        source, vf, frames = ["-loop", "1", "-i", path], "format=rgba,", 4
    elif whole:
        source, vf, frames = ["-i", path], "fps=4,", 80
    else:
        source, vf, frames = ["-ss", f"{info['duration'] / 2:.3f}", "-i", path], "", 4
    if key:
        vf += key_filters(key) + ","
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", *source,
         "-vf", vf + "alphaextract,format=gray,cropdetect=limit=4:round=2:reset=0",
         "-frames:v", str(frames), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
    if not m:
        return 0, 0, info["w"], info["h"]
    bw, bh, bx, by = map(int, m[-1])
    return bx, by, bw, bh


def is_image(path):
    return Path(path).suffix.lower() in BANNER_EXT


def image_info(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,pix_fmt", "-print_format", "json", path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise MontageError(f"не удалось прочитать картинку: {Path(path).name}")
    try:
        v = json.loads(r.stdout)["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise MontageError(f"в файле нет картинки: {Path(path).name}")
    pix = v.get("pix_fmt", "")
    return {"w": int(v["width"]), "h": int(v["height"]),
            "alpha": bool(re.search(r"yuva|rgba|bgra|argb|abgr|gbrap|pal8|ya\d", pix))}


def plan_banner(path):
    """Постоянный баннер — картинка или видео по кругу: вписываем в BAN_W×BAN_MAX_H и ставим
    по центру снизу. Пустые поля вокруг рисунка (прозрачность, зелёный фон) в отступы
    не считаются."""
    image = is_image(path)
    info = image_info(path) if image else probe(path)
    key = None if info["alpha"] else green_screen(path, None if image else info["duration"])
    bx, by, bw, bh = (opaque_bbox(path, info, image, key, whole=True) if info["alpha"] or key
                      else (0, 0, info["w"], info["h"]))
    k = min(BAN_W / bw, BAN_MAX_H / bh)
    return {"path": path, "image": image, "key": key, "duration": info.get("duration"),
            "sw": even(info["w"] * k), "sh": even(info["h"] * k),
            "x": round((W - bw * k) / 2 - bx * k),
            "y": round(H - BAN_BOTTOM - (by + bh) * k)}


def detect_bars(path, info):
    """Ищет вшитые в видео чёрные поля. Возвращает область с картинкой (w, h, x, y).
    Смотрим по строкам и столбцам, а не по рамке: логотип на чёрном поле не мешает."""
    import numpy as np
    sw = 320
    sh = even(sw * info["h"] / info["w"])
    filled = []
    for pos in (0.15, 0.4, 0.65, 0.9):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{info['duration'] * pos:.3f}", "-i", path,
             "-frames:v", "1", "-vf", f"scale={sw}:{sh},format=gray", "-f", "rawvideo", "-"],
            capture_output=True).stdout
        if len(raw) == sw * sh:
            filled.append(np.frombuffer(raw, np.uint8).reshape(sh, sw) > 28)
    full = (info["w"], info["h"], 0, 0)
    if not filled:
        return full
    lit = np.mean(filled, axis=0)

    def span(profile):
        on = np.flatnonzero(profile > BAR_FILL)
        return (int(on[0]), int(on[-1]) + 1) if len(on) else (0, len(profile))

    x0, x1 = span(lit.mean(axis=0))
    y0, y1 = span(lit.mean(axis=1))
    kx, ky = info["w"] / sw, info["h"] / sh
    cw, ch = even((x1 - x0) * kx), even((y1 - y0) * ky)
    cx, cy = int(x0 * kx) // 2 * 2, int(y0 * ky) // 2 * 2
    cw, ch = min(cw, info["w"] - cx), min(ch, info["h"] - cy)
    cut = 1 - (cw * ch) / (info["w"] * info["h"])
    if cut < 0.04 or cw < info["w"] * 0.2 or ch < info["h"] * 0.2:
        return full
    return cw, ch, cx, cy


_encoder = None


def video_encoder():
    """Аппаратный кодировщик, если он есть, иначе быстрый программный."""
    global _encoder
    if _encoder is None:
        encs = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True).stdout
        if "h264_videotoolbox" in encs:
            _encoder = ["-c:v", "h264_videotoolbox", "-b:v", VIDEO_BITRATE,
                        "-profile:v", "high", "-allow_sw", "1"]
        else:
            _encoder = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    return _encoder


_procs = set()
_procs_lock = threading.Lock()


def kill_all():
    with _procs_lock:
        for p in list(_procs):
            if p.poll() is None:
                p.kill()


def run_ffmpeg(args, on_progress=None, feeder=None):
    """Запускает ffmpeg, отдаёт прогресс в on_progress(ключ, значение),
    убивает процесс, если тот перестал двигаться."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-y"] + args
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if feeder else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=err)
    with _procs_lock:
        _procs.add(proc)
    state = {"moved": time.monotonic(), "last": None}

    def read():
        for raw in proc.stdout:
            key, _, val = raw.decode("utf-8", "ignore").strip().partition("=")
            if key in ("frame", "out_time_us") and val != state["last"]:
                state["last"] = val
                state["moved"] = time.monotonic()
                if on_progress and val.lstrip("-").isdigit():
                    on_progress(key, int(val))

    def feed():
        try:
            feeder(proc.stdin)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=read, daemon=True)]
    if feeder:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()

    stalled = False
    while proc.poll() is None:
        time.sleep(0.2)
        if time.monotonic() - state["moved"] > STALL_TIMEOUT:
            stalled = True
            proc.kill()
    for t in threads:
        t.join(timeout=5)
    with _procs_lock:
        _procs.discard(proc)
    if stalled:
        raise MontageError("ffmpeg завис и был остановлен")
    if proc.returncode != 0:
        err.seek(0)
        tail = err.read().decode("utf-8", "ignore").strip().splitlines()[-4:]
        raise MontageError("ошибка ffmpeg:\n" + "\n".join(tail))


# ---------- распознавание речи ----------
_models = {}


def transcribe(path, model_name, language, duration, on_progress=None):
    # через xet загрузка модели зависает на полпути, обычная загрузка идёт стабильно
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    import numpy as np
    from faster_whisper import WhisperModel

    if model_name not in _models:
        _models[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8",
                                           cpu_threads=max(4, (os.cpu_count() or 4) // 2))
    # звук достаём сами: встроенный в faster-whisper декодер несовместим со свежим PyAV
    pcm = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segments, info = _models[model_name].transcribe(
        audio, language=language, word_timestamps=True, vad_filter=True, beam_size=5,
    )
    words = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append((w.start, w.end, text))
        if on_progress:
            on_progress(min(seg.end, duration))
    return words, info.language


def make_chunks(words):
    chunks, cur = [], []

    def flush():
        if cur:
            chunks.append(list(cur))
            cur.clear()

    for start, end, raw in words:
        text = raw.strip(" \"'«»“”„()[]…—–-.,!?;:").upper()
        if not text:
            continue
        length = sum(len(w[2]) for w in cur) + len(cur) + len(text)
        if cur and (len(cur) >= SUB_MAX_WORDS or length > SUB_MAX_CHARS
                    or start - cur[-1][1] > SUB_GAP):
            flush()
        cur.append((start, end, text))
        if raw.rstrip("\"'»”)")[-1:] in ".!?…":
            flush()
    flush()

    result = []
    for i, ch in enumerate(chunks):
        end = ch[-1][1] + 0.35
        if i + 1 < len(chunks):
            end = min(end, chunks[i + 1][0][0])
        result.append({"start": ch[0][0], "end": max(end, ch[-1][1]), "words": ch})
    return result


# ---------- отрисовка субтитров ----------
class SubRenderer:
    def __init__(self):
        from PIL import ImageFont
        self.font_path = next((f for f in FONTS if Path(f).is_file()), None)
        if self.font_path is None:
            raise MontageError("не найден жирный шрифт — пропиши путь в FONTS")
        self.ImageFont = ImageFont
        base, accent = random.choice(SUB_PAIRS)
        self.color, self.accent = base + (255,), accent + (255,)
        self.cache = {}
        self.lock = threading.Lock()
        self.blank = bytes(W * SUB_H * 4)

    def frame(self, chunk, active):
        key = (id(chunk), active)
        with self.lock:
            if key not in self.cache:
                self.cache[key] = self._draw([w[2] for w in chunk["words"]], active)
            return self.cache[key]

    def _draw(self, texts, active):
        from PIL import Image, ImageDraw, ImageFilter
        line = " ".join(texts)
        size = SUB_FONT_SIZE
        font = self.ImageFont.truetype(self.font_path, size)
        while font.getlength(line) + 2 * SUB_STROKE > SUB_MAX_W and size > 40:
            size -= 4
            font = self.ImageFont.truetype(self.font_path, size)
        img = Image.new("RGBA", (W, SUB_H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x0 = (W - font.getlength(line)) / 2
        y = SUB_H / 2
        d.text((x0, y), line, font=font, fill=self.color, anchor="lm",
               stroke_width=SUB_STROKE, stroke_fill=SUB_OUTLINE)
        prefix = " ".join(texts[:active]) + (" " if active else "")
        d.text((x0 + font.getlength(prefix), y), texts[active], font=font,
               fill=self.accent, anchor="lm")
        if SUB_SHADOW:
            mask = Image.new("L", (W, SUB_H), 0)
            ImageDraw.Draw(mask).text((x0, y), line, font=font, fill=SUB_SHADOW, anchor="lm",
                                      stroke_width=SUB_STROKE + 6)
            shadow = Image.new("RGBA", (W, SUB_H), (0, 0, 0, 0))
            shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(6)))
            img = Image.alpha_composite(shadow, img)
        return img.tobytes()


def feed_subs(pipe, renderer, chunks, f0, f1):
    ci = 0
    for n in range(f0, f1):
        t = n / FPS
        while ci < len(chunks) and t >= chunks[ci]["end"]:
            ci += 1
        data = renderer.blank
        if ci < len(chunks) and t >= chunks[ci]["start"]:
            ch = chunks[ci]
            active = max(i for i, w in enumerate(ch["words"]) if w[0] <= t or i == 0)
            data = renderer.frame(ch, active)
        pipe.write(data)
    # Дальше шлём пустые кадры, пока ffmpeg сам не закроет трубу: если поток субтитров
    # кончается раньше видео, ffmpeg намертво зависает в самом конце.
    while True:
        pipe.write(renderer.blank)


# ---------- частицы ----------
def make_sprites():
    """Заготовки частиц: мягкие светящиеся кружки и четырёхлучевые искры."""
    from PIL import Image, ImageDraw, ImageFilter
    tints = [(255, 255, 255), (255, 236, 190), (255, 205, 130), (200, 225, 255)]
    sprites = []
    for kind in ("glow", "glow", "spark"):
        for radius in (5, 8, 12, 17):
            size = radius * 6
            c = size // 2
            mask = Image.new("L", (size, size), 0)
            d = ImageDraw.Draw(mask)
            if kind == "glow":
                d.ellipse((c - radius, c - radius, c + radius, c + radius), fill=120)
                mask = mask.filter(ImageFilter.GaussianBlur(radius * 0.6))
                d = ImageDraw.Draw(mask)
                core = max(1, radius // 3)
                d.ellipse((c - core, c - core, c + core, c + core), fill=255)
                mask = mask.filter(ImageFilter.GaussianBlur(1.2))
            else:
                thin = max(1, radius // 6)
                d.ellipse((c - radius * 2, c - thin, c + radius * 2, c + thin), fill=255)
                d.ellipse((c - thin, c - radius * 2, c + thin, c + radius * 2), fill=255)
                d.ellipse((c - radius // 2, c - radius // 2, c + radius // 2, c + radius // 2),
                          fill=200)
                mask = mask.filter(ImageFilter.GaussianBlur(1.6))
            for tint in tints:
                # один и тот же спрайт в нескольких степенях прозрачности — для мерцания
                levels = []
                for level in range(1, 9):
                    img = Image.new("RGBA", (size, size), tint + (0,))
                    img.putalpha(mask.point(lambda v, k=level / 8: int(v * k)))
                    levels.append(img)
                sprites.append(levels)
    return sprites


def render_particles(out):
    """Рисует зацикленный прозрачный клип с частицами, плывущими вверх."""
    from PIL import Image
    fw, fh = W // FX_SCALE, H // FX_SCALE
    frames = FX_LOOP * FPS
    sprites = make_sprites()
    rnd = random.Random()
    parts = []
    for _ in range(PARTICLES):
        life = rnd.uniform(3.5, 8.0)
        parts.append({
            "sprite": rnd.choice(sprites), "born": rnd.uniform(0, FX_LOOP), "life": life,
            "x": rnd.uniform(0, fw), "y": rnd.uniform(0.1 * fh, 1.05 * fh),
            "rise": rnd.uniform(14, 55), "sway": rnd.uniform(4, 22),
            "sway_speed": rnd.uniform(0.6, 1.8), "phase": rnd.uniform(0, 6.28),
            "twinkle": rnd.uniform(2.0, 6.0), "peak": rnd.uniform(0.45, 1.0),
        })
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
         "-s", f"{fw}x{fh}", "-r", str(FPS), "-i", "pipe:0",
         "-c:v", "qtrle", "-g", str(FPS), str(out)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    with _procs_lock:
        _procs.add(proc)
    try:
        for n in range(frames):
            t = n / FPS
            frame = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
            for q in parts:
                age = (t - q["born"]) % FX_LOOP      # время по кругу — клип стыкуется сам с собой
                if age >= q["life"]:
                    continue
                fade = math.sin(math.pi * age / q["life"])
                blink = 0.75 + 0.25 * math.sin(q["twinkle"] * age + q["phase"])
                level = int(8 * fade * blink * q["peak"])
                if level < 1:
                    continue
                sprite = q["sprite"][min(level, 8) - 1]
                x = q["x"] + q["sway"] * math.sin(q["sway_speed"] * age + q["phase"])
                y = q["y"] - q["rise"] * age
                frame.alpha_composite(sprite, (int(x) - sprite.width // 2,
                                               int(y) - sprite.height // 2)) \
                    if 0 <= int(x) - sprite.width // 2 and 0 <= int(y) - sprite.height // 2 \
                    else paste_clipped(frame, sprite, int(x) - sprite.width // 2,
                                       int(y) - sprite.height // 2)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        if proc.wait() != 0:
            raise MontageError("не удалось отрисовать частицы:\n"
                               + proc.stderr.read().decode("utf-8", "ignore")[-300:])
    except BrokenPipeError:
        raise MontageError("не удалось отрисовать частицы") from None
    finally:
        with _procs_lock:
            _procs.discard(proc)
    return out


def paste_clipped(frame, sprite, x, y):
    """alpha_composite не принимает отрицательные координаты — обрезаем спрайт у края кадра."""
    left, top = max(0, -x), max(0, -y)
    if left >= sprite.width or top >= sprite.height:
        return
    frame.alpha_composite(sprite.crop((left, top, sprite.width, sprite.height)),
                          (max(0, x), max(0, y)))


# ---------- сборка ----------
def even(x):
    return max(2, int(round(x / 2)) * 2)


def plan_insert(v3, D, every):
    """Вставка показывается каждые every секунд: на 30-й, 60-й и т. д.
    Если ролик для этого слишком короткий — один раз, на четверти ролика."""
    i3 = probe(v3)
    d3 = i3["duration"]
    starts = [t for t in range(every, int(D) + 1, every) if t + d3 <= D] if every > 0 else []
    if not starts:
        starts = [D * 0.25 if D * 0.25 + d3 <= D else 0.0]
    key = None if i3["alpha"] else green_screen(v3, d3)
    bx, by, bw, bh = (opaque_bbox(v3, i3, False, key) if i3["alpha"] or key
                      else (0, 0, i3["w"], i3["h"]))
    k = min(V3_W / bw, V3_MAX_H / bh)
    return {"path": v3, "info": i3, "key": key,
            "times": [(float(t), min(t + d3, D)) for t in starts],
            "sw": even(i3["w"] * k), "sh": even(i3["h"] * k),
            "x": round((W - bw * k) / 2 - bx * k), "y": round(V3_TOP - by * k)}


def plan_layout(i1, i2):
    """С двумя видео кадр делится пополам, с одним — оно занимает его целиком.
    Вшитые чёрные поля отрезаются; всё, что видео не заняло в своей части кадра,
    заполняется его размытой копией."""
    def area(info, h):
        return {"crop": info["crop"], "h": h,
                "boxed": info["crop"][:2] != (info["w"], info["h"])}
    if i2 is None:
        return {"top": area(i1, H), "bot": None}
    return {"top": area(i1, H // 2), "bot": area(i2, H // 2)}


def fit_filters(src, v, out):
    """Фильтры, вписывающие видео в полосу W×h."""
    cw, ch, cx, cy = v["crop"]
    h = v["h"]
    nat = even(W * ch / cw)
    if nat >= h and not v["boxed"]:
        # на всю ширину, лишнее по высоте срезается сверху
        return [f"{src}crop={cw}:{ch}:{cx}:{cy},scale={W}:{nat},"
                f"crop={W}:{h}:0:{nat - h},setsar=1,fps={FPS}[{out}]"]
    # целиком, без увеличения сверх полосы; свободное место — размытая копия
    k = min(W / cw, h / ch)
    fw, fh = min(W, even(cw * k)), min(h, even(ch * k))
    return [f"{src}crop={cw}:{ch}:{cx}:{cy},fps={FPS},split[{out}a][{out}b]",
            f"[{out}a]scale={W // 8}:{h // 8}:force_original_aspect_ratio=increase,"
            f"crop={W // 8}:{h // 8},gblur=sigma=6,scale={W}:{h},eq=brightness=-0.12[{out}bg]",
            f"[{out}b]scale={fw}:{fh}[{out}fg]",
            f"[{out}bg][{out}fg]overlay={(W - fw) // 2}:{(h - fh) // 2},setsar=1[{out}]"]


def has_audio(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                        "stream=codec_type", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return r.returncode == 0 and "audio" in r.stdout


def audio_args(v1, i1, ins, music, D, out):
    """Звуковая дорожка целиком: видео 1 + музыка, с приглушением на время вставок,
    + звук самих вставок."""
    args = ["-i", v1] if i1["audio"] else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    idx = 1
    fc = "[0:a]aresample=48000,aformat=channel_layouts=stereo"
    if music:
        args += ["-stream_loop", "-1", "-i", music]
        fade_at = max(0.0, D - MUSIC_FADE)
        fc += (f"[voice];[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
               f"volume={MUSIC_VOLUME},atrim=0:{D:.3f},"
               f"afade=t=out:st={fade_at:.3f}:d={MUSIC_FADE}[music];"
               "[voice][music]amix=inputs=2:normalize=0:duration=first")
        idx += 1
    if ins:
        times = ins["times"]
        windows = "+".join(f"clip((t-{S:.3f})/{DUCK_RAMP},0,1)*clip(({E:.3f}-t)/{DUCK_RAMP},0,1)"
                           for S, E in times)
        fc += f",volume='1-{1 - DUCK}*({windows})':eval=frame"
        if ins["info"]["audio"]:
            args += ["-i", ins["path"]]
            n = len(times)
            fc += (f"[a1];[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
                   f"volume={V3_VOLUME},asplit={n}"
                   + "".join(f"[s{i}]" for i in range(n)) + ";"
                   + "".join(f"[s{i}]adelay={int(S * 1000)}:all=1[d{i}];"
                             for i, (S, _) in enumerate(times))
                   + "[a1]" + "".join(f"[d{i}]" for i in range(n))
                   + f"amix=inputs={n + 1}:normalize=0:duration=longest")
    fc += f",alimiter=limit=0.95,apad,atrim=0:{D:.3f}[aout]"
    return args + ["-filter_complex", fc, "-map", "[aout]", "-vn",
                   "-c:a", "aac", "-b:a", "192k", str(out)]


def part_args(v1, v2, d2, lay, ins, has_subs, f0, f1, out, fx=None, banner=None):
    """Один кусок видео: кадры с f0 по f1."""
    a, b = f0 / FPS, f1 / FPS
    args = ["-ss", f"{a:.4f}", "-i", v1]
    if v2:
        args += ["-stream_loop", "-1", "-ss", f"{a % d2:.4f}", "-i", v2]
        fc = fit_filters("[0:v]", lay["top"], "top") + fit_filters("[1:v]", lay["bot"], "bot")
        fc.append("[top][bot]vstack[base]")
        last, idx = "base", 2
    else:
        fc = fit_filters("[0:v]", lay["top"], "base")
        last, idx = "base", 1

    if fx:
        # частицы — поверх видео, но под затемнением, субтитрами и баннером
        args += ["-stream_loop", "-1", "-ss", f"{a % FX_LOOP:.4f}", "-i", str(fx)]
        fc.append(f"[{idx}:v]scale={W}:{H},fps={FPS}[fx]")
        fc.append(f"[{last}][fx]overlay=format=auto[sparkled]")
        last, idx = "sparkled", idx + 1

    shown = [(S, E) for S, E in ins["times"] if S < b and E > a] if ins else []
    if V3_DIM > 0:
        # затемнение фона на время вставки; субтитры и сама вставка остаются поверх
        for n, (S, E) in enumerate(shown):
            fc.append(f"color=black@{V3_DIM}:s={W}x{H}:r={FPS}:d={E - S:.4f},format=yuva420p,"
                      f"fade=t=in:st=0:d={DIM_FADE}:alpha=1,"
                      f"fade=t=out:st={max(0.0, E - S - DIM_FADE):.4f}:d={DIM_FADE}:alpha=1,"
                      f"trim=start={max(0.0, a - S):.4f},"
                      f"setpts=PTS-STARTPTS+{max(0.0, S - a):.4f}/TB[dim{n}]")
            fc.append(f"[{last}][dim{n}]overlay=eof_action=pass:format=auto[dimmed{n}]")
            last = f"dimmed{n}"

    if has_subs:
        args += ["-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{SUB_H}",
                 "-r", str(FPS), "-i", "pipe:0"]
        fc.append(f"[{last}][{idx}:v]overlay=0:{SUB_CENTER_Y - SUB_H // 2}:format=auto[subbed]")
        last, idx = "subbed", idx + 1

    for n, (S, E) in enumerate(shown):
        skip = max(0.0, a - S)            # сколько вставки уже прошло до этого куска
        delay = max(0.0, S - a)
        args += ["-ss", f"{skip:.4f}", "-i", ins["path"]]
        chain = f"[{idx}:v]" + cutout(ins["key"], ins["sw"], ins["sh"])
        if not ins["info"]["alpha"] and not ins["key"]:
            out_at = max(0.0, ins["info"]["duration"] - V3_FADE - skip)
            if skip == 0:
                chain += f",fade=t=in:st=0:d={V3_FADE}:alpha=1"
            chain += f",fade=t=out:st={out_at:.3f}:d={V3_FADE}:alpha=1"
        chain += f",setpts=PTS-STARTPTS+{delay:.4f}/TB[ins{n}]"
        fc.append(chain)
        fc.append(f"[{last}][ins{n}]overlay={ins['x']}:{ins['y']}:eof_action=pass:format=auto[withins{n}]")
        last, idx = f"withins{n}", idx + 1

    if banner:
        # поверх всего, весь ролик; видео идёт по кругу — с того места, где кончился прошлый кусок
        if banner["image"]:
            args += ["-loop", "1", "-i", banner["path"]]
            chain = f"[{idx}:v]"
        else:
            args += ["-stream_loop", "-1", "-ss", f"{a % banner['duration']:.4f}",
                     "-i", banner["path"]]
            chain = f"[{idx}:v]fps={FPS},"
        chain += cutout(banner["key"], banner["sw"], banner["sh"])
        if BAN_OPACITY < 1:
            chain += f",colorchannelmixer=aa={BAN_OPACITY}"
        fc.append(chain + "[ban]")
        # время отсчитываем от начала ролика, а не куска, иначе на стыках будет рывок
        bx = (f"'{banner['x']}+{BAN_FLOAT_X}*sin(2*PI*(t+{a:.4f})/{BAN_FLOAT_TX})'"
              if BAN_FLOAT_X else str(banner["x"]))
        by = (f"'{banner['y']}+{BAN_FLOAT_Y}*sin(2*PI*(t+{a:.4f})/{BAN_FLOAT_TY}+1.1)'"
              if BAN_FLOAT_Y else str(banner["y"]))
        fc.append(f"[{last}][ban]overlay=x={bx}:y={by}:format=auto[withban]")
        last, idx = "withban", idx + 1

    return args + ["-filter_complex", ";".join(fc), "-map", f"[{last}]", "-an",
                   "-frames:v", str(f1 - f0), "-r", str(FPS), *video_encoder(),
                   "-pix_fmt", "yuv420p", "-video_track_timescale", "15360", str(out)]


def split_frames(n_frames, jobs):
    parts = max(1, min(jobs, int(n_frames / FPS // MIN_PART)))
    edges = [round(n_frames * i / parts) for i in range(parts + 1)]
    return list(zip(edges[:-1], edges[1:]))


def build(v1, v2, v3, out, model_name, language, subs=True, jobs=MAX_JOBS, every=INSERT_EVERY,
          music=None, always=None):
    """v3 — баннер. always: показывать его весь ролик (True) или заставкой раз в every секунд
    (False); None — решить по файлу: картинка видна всегда, видео идёт заставкой."""
    started = time.monotonic()
    i1 = probe(v1)
    i1["crop"] = detect_bars(v1, i1)
    i2 = probe(v2) if v2 else None
    if i2:
        i2["crop"] = detect_bars(v2, i2)
    if music and not has_audio(music):
        raise MontageError(f"в файле музыки нет звука: {Path(music).name}")
    lay = plan_layout(i1, i2)
    D = i1["duration"]
    n_frames = math.ceil(D * FPS)
    do_subs = subs and i1["audio"]

    table = Table.grid(padding=(0, 2))
    table.add_row("[dim]верх[/]" if i2 else "[dim]видео[/]", Path(v1).name,
                  f"[dim]{fmt_time(D)}  {i1['w']}×{i1['h']}[/]")
    if i2:
        table.add_row("[dim]низ[/]", Path(v2).name,
                      f"[dim]{fmt_time(i2['duration'])}  {i2['w']}×{i2['h']}[/]")
    if v3 and always is None:
        always = is_image(v3)
    if v3 and not always and is_image(v3):
        raise MontageError("заставкой может быть только видео; картинку можно показать "
                           "постоянным баннером")
    banner = plan_banner(v3) if v3 and always else None
    ins = plan_insert(v3, D, every) if v3 and not always else None
    if ins:
        at = ", ".join(fmt_time(S) for S, _ in ins["times"])
        table.add_row("[dim]заставка[/]", Path(v3).name,
                      f"[dim]на {at}{', зелёный фон убран' if ins['key'] else ''}[/]")
    if banner:
        table.add_row("[dim]баннер[/]", Path(v3).name,
                      f"[dim]весь ролик, по центру снизу"
                      f"{', зелёный фон убран' if banner['key'] else ''}[/]")
    if music:
        table.add_row("[dim]музыка[/]", Path(music).name,
                      f"[dim]громкость {MUSIC_VOLUME * 100:g} %[/]")
    table.add_row("[dim]итог[/]", f"{W}×{H}, {FPS} к/с", f"[dim]{fmt_time(D)}[/]")
    console.print(Panel(table, title="Ролик", title_align="left", border_style="cyan"))
    for name, info in (("верхнего" if i2 else "этого", i1), ("нижнего", i2)):
        if info and info["crop"][:2] != (info["w"], info["h"]):
            console.print(f"[dim]  у {name} видео чёрные поля заменены размытием[/]")
    if subs and not i1["audio"]:
        console.print("[yellow]![/] в 1-м видео нет звука — субтитров не будет")

    tmp = Path(tempfile.mkdtemp(prefix="montage_"))
    progress = Progress(
        SpinnerColumn(finished_text="[green]✓[/]"),
        TextColumn("{task.description:<20}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TextColumn("[dim]прошло[/]"), TimeElapsedColumn(),
        TextColumn("[dim]осталось[/]"), TimeRemainingColumn(),
        console=console,
    )
    # доли этапов в общем прогрессе: распознавание, рендер, склейка
    w_speech = 0.3 if do_subs else 0.0
    w_final = 0.02
    w_render = 1 - w_speech - w_final

    try:
        with progress, ThreadPoolExecutor(max_workers=jobs + 2) as pool:
            t_all = progress.add_task("[bold]Всего", total=1000)
            t_speech = progress.add_task("Распознавание речи", total=D, visible=do_subs)
            t_render = progress.add_task("Рендер видео", total=n_frames, start=False)
            frac = {"speech": 0.0, "render": 0.0, "final": 0.0}

            def refresh():
                total = (w_speech * frac["speech"] + w_render * frac["render"]
                         + w_final * frac["final"])
                progress.update(t_all, completed=round(total * 1000))

            # звук сводим, пока распознаётся речь
            audio_out = tmp / "audio.m4a"
            audio_job = pool.submit(run_ffmpeg, audio_args(v1, i1, ins, music, D, audio_out))
            fx_job = pool.submit(render_particles, tmp / "particles.mov") if PARTICLES else None

            chunks = []
            if do_subs:
                def on_speech(t):
                    progress.update(t_speech, completed=t)
                    frac["speech"] = t / D
                    refresh()
                # не вышло распознать — ролик всё равно собираем, просто без субтитров
                try:
                    words, lang = transcribe(v1, model_name, language, D, on_speech)
                    chunks = make_chunks(words)
                    note = f"{lang}, {len(words)} сл." if chunks else "речи нет — без субтитров"
                except Exception as e:
                    note = "не удалось — без субтитров"
                    progress.console.print(f"[yellow]![/] речь не распознана: {e}")
                progress.update(t_speech, completed=D,
                                description=f"Распознавание [dim]({note})[/]")
                frac["speech"] = 1.0
                refresh()

            renderer = SubRenderer() if chunks else None
            fx = fx_job.result() if fx_job else None
            parts = split_frames(n_frames, jobs)
            done = [0] * len(parts)
            progress.start_task(t_render)
            progress.update(t_render, description=f"Рендер видео [dim]×{len(parts)}[/]")

            def render_part(i):
                f0, f1 = parts[i]
                part = tmp / f"part{i:02d}.mp4"

                def on_progress(key, val):
                    if key == "frame":
                        done[i] = min(val, f1 - f0)
                        total = sum(done)
                        progress.update(t_render, completed=total)
                        frac["render"] = total / n_frames
                        refresh()

                feeder = (lambda pipe: feed_subs(pipe, renderer, chunks, f0, f1)) if chunks else None
                run_ffmpeg(part_args(v1, v2, i2["duration"] if i2 else 0, lay, ins, bool(chunks),
                                     f0, f1, part, fx, banner),
                           on_progress, feeder)
                done[i] = f1 - f0
                return part

            part_files = list(pool.map(render_part, range(len(parts))))
            progress.update(t_render, completed=n_frames)
            frac["render"] = 1.0
            refresh()
            audio_job.result()

            # склейка кусков + звук, без перекодирования; метаданные исходников не переносим
            listing = tmp / "parts.txt"
            listing.write_text("".join(f"file '{p}'\n" for p in part_files), encoding="utf-8")
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(audio_out),
                        "-map", "0:v", "-map", "1:a", "-c", "copy", "-t", f"{D:.3f}",
                        "-map_metadata", "-1", "-map_chapters", "-1",
                        "-metadata", f"creation_time={stamp}",
                        "-movflags", "+faststart", str(out)])
            frac["final"] = 1.0
            refresh()
    finally:
        kill_all()
        shutil.rmtree(tmp, ignore_errors=True)

    size = Path(out).stat().st_size / 1024 / 1024
    took = time.monotonic() - started
    console.print(Panel(
        f"[bold green]Готово[/] за {fmt_time(took)}  [dim]·[/]  {size:.0f} МБ\n{out}",
        border_style="green"))
    return out


def out_path(v1):
    OUT_DIR.mkdir(exist_ok=True)
    return OUT_DIR / f"short_{Path(v1).stem}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"


def main():
    ap = argparse.ArgumentParser(description="Монтажёр шортсов")
    ap.add_argument("videos", nargs="*",
                    help="видео 1 и (необязательно) видео 2 и баннер; «-» — пропустить")
    ap.add_argument("--music", help="файл с музыкой")
    ap.add_argument("--every", type=int, default=INSERT_EVERY,
                    help="как часто показывать заставку (видео 3), сек")
    ap.add_argument("--always", action="store_true",
                    help="видео 3 — постоянный баннер на весь ролик, а не заставка")
    ap.add_argument("-o", "--output")
    ap.add_argument("--model", default=WHISPER_MODEL, help="модель Whisper")
    ap.add_argument("--lang", default=None, help="язык речи (ru/en), по умолчанию определяется сам")
    ap.add_argument("--no-subs", action="store_true", help="без субтитров")
    ap.add_argument("-j", "--jobs", type=int, default=MAX_JOBS, help="сколько кусков рендерить сразу")
    a = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        console.print("[red]Не найден ffmpeg.[/] "
                      + ("Перезапусти программу — она докачает его сама." if os.name == "nt"
                         else "Установи: brew install ffmpeg"))
        sys.exit(1)

    if a.videos:
        v = ([None if x == "-" else x for x in a.videos] + [None, None])[:3]
        if v[0] is None:
            ap.error("первое видео обязательно")
        try:
            build(v[0], v[1], v[2], a.output or out_path(v[0]), a.model, a.lang,
                  not a.no_subs, a.jobs, a.every, a.music, True if a.always else None)
        except MontageError as e:
            console.print(f"[red]✗ {e}[/]")
            sys.exit(1)
        return

    console.print(Panel(
        "[bold]Монтажёр шортсов[/]"
        + (f"  [dim]версия {os.environ['MONTAGER_VERSION']}[/]"
           if os.environ.get("MONTAGER_VERSION") else "")
        + "\n[dim]Ctrl+C — выход.[/]",
        border_style="cyan"))
    while True:
        action = ask_action()
        console.print()
        try:
            if action == "montage":
                console.print("[dim]Перетаскивай файлы в это окно и жми Enter.[/]")
                v1, v2, v3, music, always = ask_files()
                every = ask_every(a.every) if v3 and not always else a.every
                subs = ask_subs() if not a.no_subs else False
                console.print()
                build(v1, v2, v3, out_path(v1), a.model, a.lang, subs,
                      a.jobs, every, music, always)
            else:
                download_flow()
        except (MontageError, download.DownloadError) as e:
            console.print(Panel(f"[red]{e}[/]", title="Не получилось", title_align="left",
                                border_style="red"))
        console.print()


def cli():
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        kill_all()
        console.print("\n[dim]Выход.[/]")
        sys.exit(0)


if __name__ == "__main__":
    cli()
