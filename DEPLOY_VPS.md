# Деплой на VPS Timeweb Cloud Servers

Прямой контроль, без магии App Platform. Стоимость ~200–250 ₽/мес.

## 1. Создать VPS (3 минуты)

1. Timeweb Cloud → **Облачные серверы** → **Создать сервер**
2. **Образ:** Ubuntu 24.04
3. **Регион:** Москва
4. **Конфигурация:** минимальная — 1 CPU / 1 GB / 15 GB. ~200₽/мес
5. **Имя:** `maxim-zakup`
6. **Создать**

Дождись пока статус станет «Активен» (~30 сек). Скопируй **публичный IP** и **root-пароль** (показывается один раз).

## 2. Войти по SSH

С Mac в Терминале:
```bash
ssh root@ПУБЛИЧНЫЙ_IP
```
Введи root-пароль (буквы не отображаются, это норм).

## 3. Установить Docker (одна команда, 1 минута)

Скопируй и запусти:
```bash
curl -fsSL https://get.docker.com | sh
```

Дождись «Successfully installed».

## 4. Клонировать репо

```bash
cd /opt
git clone https://github.com/zamkofde-svg/maxim-zakup.git
cd maxim-zakup
```

## 5. Создать `.env` с ключами

```bash
nano .env
```

Вставь (Ctrl+Shift+V в Терминале):

```
OPENROUTER_API_KEY=<КЛЮЧ_OPENROUTER>
GOOGLE_SA_JSON_CONTENT=<ВЕСЬ_JSON_SA_ОДНОЙ_СТРОКОЙ>
```

Конкретные значения я тебе пришлю в чате (они есть в истории нашего диалога — те же что ставили в Timeweb App Platform).

Сохрани: **Ctrl+O**, Enter, **Ctrl+X**.

## 6. Запустить

```bash
docker compose up -d --build
```

Билд займёт ~3-5 минут (один раз). Потом докер запустит контейнер в фоне.

## 7. Проверить

```bash
curl http://localhost/healthz
```
Должно ответить `{"ok":true}`.

С твоего Mac:
```bash
curl http://ПУБЛИЧНЫЙ_IP/healthz
```
Должно ответить то же самое.

## 8. Открыть в браузере

```
http://ПУБЛИЧНЫЙ_IP/
```

Это и есть твоё приложение. **Эту ссылку отправляешь заказчику.**

(Если хочешь красивый домен `zakupki.maxim-rest.ru` с HTTPS — добавим Caddy за 10 минут, скажешь когда нужно.)

## Полезное

**Логи в реальном времени:**
```bash
docker compose logs -f
```

**Перезапустить после изменений (git pull):**
```bash
cd /opt/maxim-zakup
git pull
docker compose up -d --build
```

**Если что-то сломалось — рестарт:**
```bash
docker compose restart
```
