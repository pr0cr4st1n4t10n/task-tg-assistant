#!/bin/bash
# Скрипт деплоя на VPS
# Запускать от root: bash deploy.sh

set -e

echo "=== Telegram Business Assistant Bot ==="
echo ""

PROJECT_DIR="/root/tg-assistant"

# 1. Копируем файлы
echo "[1/5] Копируем файлы..."
mkdir -p "$PROJECT_DIR"
cp bot.py analyzer.py notion_db.py requirements.txt tg-assistant.service "$PROJECT_DIR/"

# 2. Копируем .env если ещё нет
if [ ! -f "$PROJECT_DIR/.env" ]; then
    cp .env.example "$PROJECT_DIR/.env"
    echo ""
    echo "⚠️  Заполни файл .env:"
    echo "    nano $PROJECT_DIR/.env"
    echo ""
fi

# 3. Виртуальное окружение
echo "[2/5] Создаём venv..."
cd "$PROJECT_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip -q
venv/bin/pip install -r requirements.txt -q
echo "    ✅ Зависимости установлены"

# 4. systemd сервис
echo "[3/5] Устанавливаем systemd сервис..."
cp "$PROJECT_DIR/tg-assistant.service" /etc/systemd/system/tg-assistant.service
systemd-analyze verify /etc/systemd/system/tg-assistant.service 2>/dev/null || true
systemctl daemon-reload
echo "    ✅ Сервис зарегистрирован"

# 5. Проверяем .env
echo "[4/5] Проверяем конфигурацию..."
if grep -q "BOT_TOKEN=1234567890" "$PROJECT_DIR/.env"; then
    echo ""
    echo "❌  .env не заполнен! Заполни его и запусти снова:"
    echo "    nano $PROJECT_DIR/.env"
    echo "    bash deploy.sh"
    exit 1
fi
echo "    ✅ .env заполнен"

# 6. Запускаем
echo "[5/5] Запускаем бота..."
systemctl enable tg-assistant
systemctl restart tg-assistant
sleep 2

if systemctl is-active --quiet tg-assistant; then
    echo ""
    echo "🚀 Бот успешно запущен!"
    echo ""
    echo "Управление:"
    echo "  systemctl status tg-assistant   — статус"
    echo "  journalctl -u tg-assistant -f   — логи в реальном времени"
    echo "  systemctl restart tg-assistant  — перезапуск"
    echo "  systemctl stop tg-assistant     — остановить"
else
    echo ""
    echo "❌ Бот не запустился. Смотри логи:"
    echo "   journalctl -u tg-assistant -n 30"
fi
