# Схема путей → Minecraft RCON

Интерактивная схема железнодорожных путей. Клик по жёлтой полоске (стрелке)
отправляет команду в Minecraft:

| Положение | Команда |
|---|---|
| В бок | `/setblock <x> <y> <z> redstone_block` |
| Прямо | `/setblock <x> <y> <z> air` |

Координаты задаются в админ-панели на самой странице.

Страницы синхронизируются между собой: одна стрелка переключена — у всех
открытых страниц она тоже переключилась (живой канал `/api/events`).

```
Браузер (index.html) --HTTP--> server.py --RCON(TCP)--> Minecraft
```

Пароль RCON хранится только в `.env` на сервере. В странице и в GitHub его нет.

---

## 1. Запуск на своём компьютере

Дважды кликни **`start-local.bat`**.

Он найдёт Python и запустит сервер, затем открой <http://localhost:3000>.

> В архиве уже лежит портативный Python (`runtime\python`), ставить ничего не надо.
> На Linux: `python3 server.py`.

Локальные настройки — файл `.env` (создаётся из `.env.example`).

---

## 2. Админ-панель (координаты стрелок)

На странице кнопка **«Админ»** → для каждой стрелки:

- `X`, `Y`, `Z` — координаты редстоун-блока;
- «Блок в бок» — обычно `redstone_block`;
- «Блок прямо» — обычно `air`.

Под каждой строкой сразу видно, какие команды уйдут. Кнопка
**«Сохранить координаты»** пишет всё в `switches.json`.

Если координаты у стрелки не заданы, используется запись из `commands.json`
(запасной вариант, там можно прописать любые команды).

---

## 3. Публикация на GitHub Pages

GitHub Pages — это **только страница**, без сервера. Схема, админка и настройки
работают, но команды в Minecraft пойдут только если рядом есть запущенный
`server.py` (см. пункт 4) — страница обратится к нему по адресу из кнопки
**«Сервер»**.

1. Создай репозиторий на github.com (Public).
2. **Add file → Upload files** → перетащи содержимое архива (папки `deploy`,
   `runtime` можно не заливать; `.env` заливать НЕЛЬЗЯ — там пароль).
3. **Commit changes**.
4. **Settings → Pages → Source: Deploy from a branch → Branch: `main`, папка `/ (root)` → Save**.
5. Через минуту сайт откроется по адресу `https://ИМЯ.github.io/РЕПОЗИТОРИЙ/`.

### Подключить сервер к странице на GitHub

1. На VPS запусти `server.py` (пункт 4) и в его `.env` задай:
   ```env
   ALLOW_ORIGIN=https://ИМЯ.github.io
   API_KEY=придумай-длинный-ключ
   ```
2. На странице нажми **«Сервер»**, впиши:
   - адрес: `https://твой-vps.ru`
   - ключ: тот же `API_KEY`
3. Адрес и ключ сохранятся в браузере. В исходниках страницы их нет.

Без ключа любой посетитель сайта мог бы щёлкать стрелками — поэтому `API_KEY`
лучше задать всегда, когда сервер доступен из интернета.

---

## 4. Сервер на VPS (Ubuntu/Debian)

```bash
sudo mkdir -p /opt/railway-switches
# скопируй туда server.py, index.html, commands.json, switches.json, .env.example
sudo bash deploy/install.sh
sudo nano /opt/railway-switches/.env      # RCON_* и API_KEY
sudo systemctl restart railway-switches
curl http://127.0.0.1:3000/api/ping
```

Автозапуск, логи:

```bash
systemctl status railway-switches
journalctl -u railway-switches -f
```

Nginx + HTTPS:

```nginx
server {
    listen 80;
    server_name metro.example.ru;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

`sudo certbot --nginx -d metro.example.ru`.
Если впереди nginx — поставь в `.env` `BIND=127.0.0.1`.

---

## 5. RCON на Gamely

1. Панель Gamely → сервер → раздел **RCON**: включить, взять порт и пароль.
2. Добавить IP сервера (VPS) в whitelist RCON, иначе будет `ETIMEDOUT`.
3. В `.env`:

```env
RCON_HOST=123.45.67.89
RCON_PORT=25575
RCON_PASSWORD=...
RCON_TIMEOUT=8000
```

4. `curl http://127.0.0.1:3000/api/ping` — в ответе должен быть список игроков.

Пока `RCON_*` пустые, сервер работает в режиме заглушки: команды не уходят,
только пишутся в лог.

---

## 6. API

| Метод | Адрес | Что делает |
|---|---|---|
| `GET` | `/api/health` | режим, настройки RCON, список стрелок (без ключа) |
| `GET` | `/api/ping` | проверка связи: шлёт `list` в Minecraft |
| `POST` | `/api/switch/:n` | `{"position":"straight"\|"branch"}` |
| `GET` | `/api/admin/switches` | координаты всех стрелок |
| `POST` | `/api/admin/switches` | сохранить координаты |
| `GET` | `/api/log` | последние 300 строк лога |
| `GET` | `/api/events` | живой поток состояний (SSE), ключ можно в `?key=` |

Заголовки при включённом ключе: `X-Api-Key`, для админки — `X-Admin-Key`.

---

## 7. Если что-то не работает

| Симптом | Причина |
|---|---|
| «Демо-режим: команды не отправляются» | страница открыта без сервера (GitHub Pages / файл) |
| «Сервер недоступен» | не запущен `server.py` или неверный адрес в кнопке «Сервер» |
| «RCON не настроен — режим заглушки» | не заполнены `RCON_*` в `.env` |
| `ETIMEDOUT` | RCON-порт закрыт / IP сервера не в whitelist Gamely |
| «Неверный пароль RCON» | неверный `RCON_PASSWORD` |
| «Нет команды для стрелки N» | не заданы координаты в админ-панели |
| Стрелка откатилась назад | команда не ушла, причина — внизу экрана |

Лог: `journalctl -u railway-switches -f` или `GET /api/log`.
