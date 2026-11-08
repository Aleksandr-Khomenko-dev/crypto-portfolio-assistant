# Деплой TrendAnalystBot на VPS (работа 24/7)

Схема: на сервере в Docker работают четыре процесса из одного образа:

| Сервис | Что делает | Доступ из интернета |
|---|---|---|
| `runner` | сканер, быстрый watcher, OI, новости, календарь, отправка сигналов | нет |
| `api` | дашборд, REST API, команды Telegram | **нет**, только `localhost` сервера |
| `public` | только `/health` и приём webhook TradingView | через Caddy |
| `caddy` | HTTPS на 443, сертификат Let's Encrypt автоматически | да, порты 80/443 |

В интернет смотрит только Caddy, и он пропускает **только** `/health` и адреса webhook.
Портфель, дашборд и админка из интернета недоступны.

## Что понадобится

- VPS на Ubuntu 24.04: 2 vCPU, 4 ГБ RAM, от 40 ГБ диска (база SQLite сейчас около 2 ГБ).
- Домен (или поддомен, например `bot.example.com`) с **A-записью** на IPv4-адрес сервера.
  TradingView не работает с IPv6, поэтому нужна именно A-запись.
- Ваш `.env` с Mac и файл базы `dev.db`.

## 1. Подготовить сервер

```bash
ssh root@IP_СЕРВЕРА
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh          # Docker + docker compose
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
```

## 2. Получить код

Репозиторий приватный: при `git clone` укажите логин GitHub и токен (как при push с Mac).

```bash
git clone https://github.com/Aleksandr-Khomenko-dev/crypto-portfolio-assistant.git trendanalyst
cd trendanalyst
git checkout feature/gold-only      # ветка, в которой ведётся работа
mkdir -p data logs
```

## 3. Перенести `.env` и базу с Mac

На Mac, в папке проекта:

```bash
scripts/gold_bot.sh stop                          # остановить бота на Mac
sqlite3 dev.db ".backup 'dev-transfer.db'"        # целостная копия базы
scp .env root@IP_СЕРВЕРА:~/trendanalyst/.env
scp dev-transfer.db root@IP_СЕРВЕРА:~/trendanalyst/data/dev.db
rm dev-transfer.db
```

На сервере добавьте в `~/trendanalyst/.env` строку с вашим доменом и выдайте права папкам:

```bash
echo "DOMAIN=bot.example.com" >> .env
chown -R 10001:10001 data logs
```

## 4. Запустить

```bash
docker compose up -d --build
docker compose ps                      # migrate: exited (0), остальные: running
docker compose logs -f runner          # логи сканера (Ctrl+C — выйти из просмотра)
```

Проверка с любого компьютера:

```bash
curl https://bot.example.com/health              # {"status":"ok"}
curl -i https://bot.example.com/api/portfolios   # 404: внутренний API закрыт
```

## 5. Только одна копия бота

Два запущенных бота с одним токеном конкурируют за сообщения и дублируют сигналы.
Когда сервер заработал, на Mac отключите автозапуск:

```bash
scripts/gold_bot.sh uninstall
```

## 6. Дашборд с сервера

Дашборд доступен только через SSH-туннель:

```bash
ssh -L 8000:127.0.0.1:8000 root@IP_СЕРВЕРА
# затем в браузере на Mac: http://localhost:8000/dashboard
```

## 7. Обновление и резервные копии

```bash
cd ~/trendanalyst && git pull && docker compose up -d --build   # обновить
apt install -y sqlite3
sqlite3 data/dev.db ".backup 'data/backup-$(date +%F).db'"     # копия базы на ходу
```

## Настройка TradingView (индикатор QS PRO ICT v4)

Нужен платный тариф TradingView (webhook есть только на платных) и **включённая
двухфакторная авторизация** — без неё TradingView не отправляет webhook.

1. В `.env` на сервере задайте `CPDA_TV_WEBHOOK_TOKEN` — длинный случайный токен:
   `python3 -c "import secrets; print(secrets.token_urlsafe(36))"`, затем
   `docker compose up -d` (перезапуск подхватит токен).
2. На графике актива (например, XAUUSD 5m) в настройках индикатора:
   «Entry mode» → «Confirmed: enter after a reaction candle»;
   «Alert message format» → «JSON for a bot (webhook)»;
   «Also alert waiting zones» — выключено.
3. Создайте алерт: условие — «QS PRO ICT v4» → «Any alert() function call»;
   в «Уведомлениях» включите Webhook URL:
   `https://bot.example.com/webhook/tv/<CPDA_TV_WEBHOOK_TOKEN>`
4. Один алерт на каждую пару актив + таймфрейм. Актив должен быть в
   `CPDA_TV_ALLOWED_SYMBOLS`.
5. На тарифах Essential и Plus алерты истекают через 2 месяца — пересоздавайте.
6. Проверка: журнал алертов TradingView, колонка статуса webhook; в боте — сообщение
   о входе. Без TradingView: `docker compose exec public python -m scripts.replay_tv
   --url http://127.0.0.1:8001 --scenario full` (сообщения помечены TEST).

Старый формат «QS PRO Gold» продолжает приниматься по адресу
`https://bot.example.com/api/webhooks/tradingview` (и по новому адресу тоже),
пока `CPDA_LEGACY_TV_FORMAT_ENABLED=true`.

## PostgreSQL

Пока бот работает на SQLite: так переносится вся история без конвертации.
PostgreSQL подключается профилем `postgres` и переменной `DOCKER_DATABASE_URL`,
но переход требует переноса данных и отдельной проверки; делаем его позже,
отдельным шагом.

## Временный туннель с Mac (для проверки до сервера)

```bash
brew install cloudflared
scripts/tunnel.sh
```

Скрипт запускает только публичное приложение и туннель Cloudflare к нему. Адрес
вида `https://…trycloudflare.com` меняется при каждом запуске — только для проверки.
