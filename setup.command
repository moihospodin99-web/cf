#!/bin/bash
# Competitor Analyzer - nzrxo ai : встановлення на macOS
#
# Ставимо через Homebrew — стандартний менеджер пакетів macOS, який сам
# розрізняє Apple Silicon (/opt/homebrew) та Intel (/usr/local). Власні
# збірки ffmpeg під дві архітектури були б крихкіші.
#
# Бібліотеки Python ідуть у venv поруч із програмою, а не в систему: у
# свіжих macOS системний Python захищений і pip у нього не пише.
set -u
cd "$(dirname "$0")"
ROOT="$PWD"

say()  { printf "\n\033[1m%s\033[0m\n" "$1"; }
fail() { printf "\n\033[31m!!! %s\033[0m\n\n" "$1"; read -r -p "Enter щоб закрити"; exit 1; }

echo "================================================"
echo "  Competitor Analyzer - nzrxo ai - встановлення"
echo "================================================"
echo
echo "Завантажиться кілька гігабайтів, це може зайняти від 30 хвилин."
echo "Можна залишити і піти у своїх справах."
echo

# Файл прийшов через мережу (Telegram, git, браузер) — macOS вішає на нього
# позначку карантину і без сертифіката Apple ($99/рік) показує оманливе
# "програму пошкоджено, перенести в Смітник". Знімаємо самі.
if xattr -cr "$ROOT" 2>/dev/null; then
    echo "Позначку карантину macOS знято."
fi
# Прапорець виконуваного файлу часто губиться при розпакуванні/копіюванні.
# Без нього подвійне клацання дає "немає відповідних привілеїв доступу".
chmod +x "$ROOT/setup.command" "$ROOT/start.command" 2>/dev/null

# ---------- 1. Homebrew ----------
say "[1/6] Homebrew"
if command -v brew >/dev/null 2>&1; then
    echo "    вже встановлено"
else
    echo "    встановлюю (може запитати пароль macOS)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
        || fail "Homebrew не встановився"
fi
for p in /opt/homebrew/bin /usr/local/bin; do
    [ -x "$p/brew" ] && eval "$("$p/brew" shellenv)"
done
command -v brew >/dev/null 2>&1 || fail "brew не знайдено навіть після встановлення"

# ---------- 2. Python ----------
say "[2/6] Python"
# НЕ довіряємо системному "python3" наосліп: macOS/Xcode Command Line Tools
# нерідко мають свій старий python3 (напр. 3.9) ще ДО встановлення Homebrew.
# Ставимо Homebrew python@3.12 завжди і venv створюємо саме на ньому.
brew list python@3.12 >/dev/null 2>&1 && echo "    python@3.12 вже є" \
    || brew install python@3.12 || fail "Python не встановився"
PY312="$(brew --prefix python@3.12)/bin/python3.12"
[ -x "$PY312" ] || fail "не знайшов python3.12 після встановлення"
echo "    використовую: $($PY312 --version)"

# ---------- 3. venv і бібліотеки ----------
say "[3/6] Бібліотеки Python"
# Не лише "чи є тека venv", а й "чи Python у ній достатньо новий" —
# інакше зламана копія (напр. зі старого системного 3.9) мовчки лишалась
# би при повторному запуску setup.command.
if [ -x venv/bin/python ] && venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    : # venv є і Python у ньому достатньо новий
else
    [ -d venv ] && { echo "    стара версія Python у venv — перестворюю"; rm -rf venv; }
    "$PY312" -m venv venv || fail "не вдалося створити venv"
fi
# --upgrade навмисно: yt-dlp латають під TikTok кілька разів на місяць, і
# застаріла копія імпортується нормально, але каталог уже не гортає.
./venv/bin/python -m pip install --upgrade pip -q || fail "pip не оновився"
./venv/bin/python -m pip install --upgrade -q -r requirements.txt \
    || fail "бібліотеки не встановились"

# ---------- 4. ffmpeg і deno ----------
say "[4/6] ffmpeg і deno"
brew list ffmpeg >/dev/null 2>&1 && echo "    ffmpeg вже є" || brew install ffmpeg || fail "ffmpeg не встановився"
brew list deno   >/dev/null 2>&1 && echo "    deno вже є"   || brew install deno   || fail "deno не встановився"

# ---------- 5. Модель розпізнавання мови ----------
say "[5/6] Модель розпізнавання мови (~1.5 ГБ)"
./venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('medium', device='cpu', compute_type='int8')" \
    || fail "модель не завантажилась"

# ---------- 6. Ollama ----------
# Потрібна ЛИШЕ для колонки «Формат відео». Якщо не стане — програма
# працює, тому тут не fail, а попередження.
say "[6/6] Ollama і моделі розпізнавання зображень (~5 ГБ)"
if command -v ollama >/dev/null 2>&1 || brew list ollama >/dev/null 2>&1; then
    echo "    вже встановлена"
else
    brew install ollama || echo "    не встановилась — колонка «Формат відео» буде недоступна"
fi
if command -v ollama >/dev/null 2>&1; then
    (ollama serve >/dev/null 2>&1 &)
    sleep 5
    ollama pull moondream    || echo "    moondream не завантажилась"
    ollama pull qwen2.5vl:3b || echo "    qwen2.5vl не завантажилась"
fi

# ---------- ярлик ----------
ln -sf "$ROOT/start.command" "$HOME/Desktop/Competitor Analyzer.command" 2>/dev/null \
    && echo "    ярлик на робочому столі створено"

echo
echo "================================================"
echo "  Готово. Запускай start.command"
echo "================================================"
echo
read -r -p "Enter щоб закрити"
