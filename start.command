#!/bin/bash
# Запуск Competitor Analyzer на macOS.
set -u
cd "$(dirname "$0")"

for p in /opt/homebrew/bin /usr/local/bin; do
    [ -x "$p/brew" ] && eval "$("$p/brew" shellenv)"
done

if [ ! -x venv/bin/python ]; then
    echo "Спершу запусти setup.command — бібліотеки ще не встановлені."
    read -r -p "Enter щоб закрити"
    exit 1
fi

# Ollama потрібна лише для колонки «Формат відео» — піднімаємо, якщо є.
if command -v ollama >/dev/null 2>&1 && ! curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
    (ollama serve >/dev/null 2>&1 &)
fi

if curl -s --max-time 2 http://127.0.0.1:5050/ >/dev/null 2>&1; then
    echo "Програма вже працює — відкриваю сторінку."
    open "http://127.0.0.1:5050"
    exit 0
fi

echo "Запускаю Competitor Analyzer..."
./venv/bin/python app.py &
APP_PID=$!

# Фіксована пауза тут крихка: перший холодний запуск вантажить faster-whisper
# та інші бібліотеки і може зайняти більше кількох секунд. Чекаємо, доки
# сервер САМ не почне відповідати, до 60 секунд.
echo "Чекаю запуску сервера (перший раз може бути довше)..."
for i in $(seq 1 60); do
    if curl -s --max-time 1 "http://127.0.0.1:5050/" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 $APP_PID 2>/dev/null; then
        echo "Сервер завершився з помилкою — дивись текст вище."
        read -r -p "Enter щоб закрити"
        exit 1
    fi
    sleep 1
done
open "http://127.0.0.1:5050"
echo
echo "Програма працює. Це вікно закривати НЕ можна — воно і є програма."
echo "Щоб зупинити: Ctrl+C або просто закрий це вікно."
wait $APP_PID
