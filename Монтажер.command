#!/bin/bash
# Двойной клик открывает Терминал и запускает монтажёр.
cd "$(dirname "$0")" || exit 1

if ! command -v ffmpeg >/dev/null; then
  echo "Не найден ffmpeg. Установи: brew install ffmpeg"
  read -r -p "Enter — закрыть"
  exit 1
fi

# Питон берём проверенной версии: для самых свежих ещё нет готовых библиотек,
# и установка падает на сборке av (faster-whisper).
find_python() {
  for c in python3.14 python3.13 python3.12 python3.11 python3; do
    command -v "$c" >/dev/null && { command -v "$c"; return 0; }
  done
  return 1
}

# библиотеки ставим при первом запуске, когда изменился requirements.txt
# и когда окружение сломалось (например, собрано другим питоном)
if [ -x .venv/bin/python ] && ! .venv/bin/python -c "import rich, faster_whisper" >/dev/null 2>&1; then
  echo "Окружение не в порядке, пересобираю..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Ставлю библиотеки..."
  PY="$(find_python)" || { read -r -p "Не найден python3. Enter — закрыть"; exit 1; }
  { [ -x .venv/bin/python ] || "$PY" -m venv .venv; } \
    && .venv/bin/pip install -q --upgrade pip \
    && .venv/bin/pip install -q -r requirements.txt \
    && touch .venv/.installed || { read -r -p "Ошибка установки. Enter — закрыть"; exit 1; }
fi

clear
.venv/bin/python montage.py
