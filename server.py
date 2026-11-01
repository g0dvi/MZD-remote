# -*- coding: utf-8 -*-
"""
Мост «страница -> Minecraft RCON» на чистом Python.

    Браузер (index.html) --HTTP--> этот сервер --RCON(TCP)--> Minecraft

Переключение стрелки:
    на боковой путь ->  setblock <x> <y> <z> redstone_block
    прямо           ->  setblock <x> <y> <z> air

Координаты берутся из switches.json (их правит админ-панель на сайте).
Если у стрелки координат нет — используется старая запись из commands.json.

Только стандартная библиотека: ничего ставить через pip не нужно.
Запуск:  python server.py
"""

import json
import os
import queue
import re
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COMMANDS_FILE = os.path.join(BASE_DIR, "commands.json")
SWITCHES_FILE = os.path.join(BASE_DIR, "switches.json")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")
ENV_FILE = os.path.join(BASE_DIR, ".env")

# Читаемый русский текст в консоли Windows.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# --------------------------------------------------------------------------
#  .env
# --------------------------------------------------------------------------

def load_env(path):
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                data[key.strip()] = value
    for key, value in os.environ.items():
        data.setdefault(key, value)
    return data


ENV = load_env(ENV_FILE)


def env_str(name, default=""):
    return (ENV.get(name) or default).strip()


def env_int(name, default):
    try:
        return int(float(env_str(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


PORT = env_int("PORT", 3000)
BIND = env_str("BIND", "0.0.0.0")

RCON_HOST = env_str("RCON_HOST")
RCON_PORT = env_int("RCON_PORT", 25575)
RCON_PASSWORD = ENV.get("RCON_PASSWORD") or ""
RCON_TIMEOUT = env_int("RCON_TIMEOUT", 8000) / 1000.0

SITE_USER = env_str("SITE_USER")
SITE_PASS = env_str("SITE_PASS")
ADMIN_PASSWORD = env_str("ADMIN_PASSWORD")

# Ключ для вызовов API с другого домена (например, со страницы на GitHub Pages).
API_KEY = env_str("API_KEY")
# Каким сайтам разрешено обращаться к API: "*" или список через запятую.
ALLOW_ORIGIN = env_str("ALLOW_ORIGIN", "*")

DEDUPE_MS = env_int("DEDUPE_MS", 1500)
MAX_QUEUE = env_int("MAX_QUEUE", 30)

DEFAULT_BLOCK = "redstone_block"
DEFAULT_CLEAR = "air"

HAS_CREDS = bool(RCON_HOST and RCON_PASSWORD)
FORCE_DRY = env_str("DRY_RUN").lower() == "true"
DRY_RUN = FORCE_DRY or not HAS_CREDS


# --------------------------------------------------------------------------
#  Лог
# --------------------------------------------------------------------------

_logs = []
_log_lock = threading.Lock()


def log(message):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    with _log_lock:
        _logs.append(line)
        if len(_logs) > 300:
            del _logs[0]
    try:
        print(line, flush=True)
    except Exception:
        pass


# --------------------------------------------------------------------------
#  switches.json — координаты редстоун-блоков
# --------------------------------------------------------------------------

_sw_lock = threading.RLock()
_sw_cache = {"mtime": None, "data": {}}

COORD_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
BLOCK_RE = re.compile(r"^[a-z0-9_:]+$")


def load_switches():
    with _sw_lock:
        try:
            mtime = os.path.getmtime(SWITCHES_FILE)
        except OSError:
            return _sw_cache["data"]

        if mtime == _sw_cache["mtime"]:
            return _sw_cache["data"]

        try:
            with open(SWITCHES_FILE, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            _sw_cache.update(mtime=mtime, data=data if isinstance(data, dict) else {})
            log("switches.json прочитан заново")
        except (OSError, ValueError) as err:
            _sw_cache["mtime"] = mtime
            log("ОШИБКА в switches.json: %s — работаю со старыми данными" % err)

        return _sw_cache["data"]


def save_switches(data):
    with _sw_lock:
        tmp = SWITCHES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, SWITCHES_FILE)
        _sw_cache["mtime"] = None       # заставим перечитать при следующем запросе
        _sw_cache["data"] = data


def has_coords(cfg):
    return (
        isinstance(cfg, dict)
        and cfg.get("x") is not None
        and cfg.get("y") is not None
        and cfg.get("z") is not None
    )


def fmt_coord(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def coords_text(cfg):
    return "%s %s %s" % (
        fmt_coord(cfg["x"]),
        fmt_coord(cfg["y"]),
        fmt_coord(cfg["z"]),
    )


def parse_coord(value):
    """Пусто -> None. Число или числовая строка -> int/float. Иначе ValueError."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value

    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    if not COORD_RE.match(text):
        raise ValueError("«%s» не похоже на координату" % value)

    return float(text) if "." in text else int(text)


def parse_block(value, default):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if not BLOCK_RE.match(text):
        raise ValueError("«%s» не похоже на имя блока" % value)
    return text


# --------------------------------------------------------------------------
#  commands.json — запасной вариант для стрелок без координат
# --------------------------------------------------------------------------

_cmd_lock = threading.RLock()
_cmd_cache = {"mtime": None, "data": {}, "missing": False}


def load_commands():
    with _cmd_lock:
        try:
            mtime = os.path.getmtime(COMMANDS_FILE)
        except OSError:
            if not _cmd_cache["missing"]:
                _cmd_cache["missing"] = True
                log("commands.json не найден: %s" % COMMANDS_FILE)
            return _cmd_cache["data"]

        if mtime == _cmd_cache["mtime"]:
            return _cmd_cache["data"]

        try:
            with open(COMMANDS_FILE, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            _cmd_cache.update(mtime=mtime, data=data, missing=False)
            log("commands.json прочитан заново")
        except (OSError, ValueError) as err:
            _cmd_cache["mtime"] = mtime
            log("ОШИБКА в commands.json: %s — работаю со старыми командами" % err)

        return _cmd_cache["data"]


def commands_from_file(number, position):
    entry = load_commands().get(str(number))
    if not isinstance(entry, dict):
        return None

    value = entry.get(position)
    if value is None:
        return None

    items = value if isinstance(value, list) else [value]
    result = []
    for item in items:
        command = str(item).strip()
        if command.startswith("/"):
            command = command[1:]
        if command:
            result.append(command)

    return result or None


# --------------------------------------------------------------------------
#  Итоговые команды
# --------------------------------------------------------------------------

def build_commands(cfg, position):
    if position == "branch":
        block = cfg.get("block") or DEFAULT_BLOCK
    else:
        block = cfg.get("clear") or DEFAULT_CLEAR
    return ["setblock %s %s" % (coords_text(cfg), block)]


def commands_for(number, position):
    cfg = load_switches().get(str(number))
    if has_coords(cfg):
        return build_commands(cfg, position)
    return commands_from_file(number, position)


def switch_numbers():
    numbers = set()
    for source in (load_switches(), load_commands()):
        for key in source.keys():
            if str(key).isdigit():
                numbers.add(int(key))

    # всегда показываем хотя бы 1..6
    if not numbers:
        numbers = set(range(1, 7))

    return [str(n) for n in sorted(numbers)]


def admin_state():
    data = load_switches()
    result = {}
    for number in switch_numbers():
        cfg = data.get(number) or {}
        result[number] = {
            "x": cfg.get("x"),
            "y": cfg.get("y"),
            "z": cfg.get("z"),
            "block": cfg.get("block") or DEFAULT_BLOCK,
            "clear": cfg.get("clear") or DEFAULT_CLEAR,
            "configured": has_coords(cfg),
            "source": "coords" if has_coords(cfg) else "commands.json",
        }
    return result


# --------------------------------------------------------------------------
#  RCON (протокол Source)
# --------------------------------------------------------------------------

class RconError(Exception):
    pass


SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2

_rcon_lock = threading.Lock()
_queue_count = 0


def _pack_packet(req_id, ptype, body):
    payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def _recv_exact(sock, size):
    chunks = []
    received = 0
    while received < size:
        chunk = sock.recv(size - received)
        if not chunk:
            raise RconError("соединение закрыто сервером")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def _read_packet(sock):
    (size,) = struct.unpack("<i", _recv_exact(sock, 4))
    if size < 10 or size > 65536:
        raise RconError("некорректный RCON-пакет (size=%d)" % size)
    data = _recv_exact(sock, size)
    req_id, ptype = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", errors="replace")
    return req_id, ptype, body


def rcon_execute(commands):
    replies = []

    with socket.create_connection((RCON_HOST, RCON_PORT), timeout=RCON_TIMEOUT) as sock:
        sock.settimeout(RCON_TIMEOUT)
        sock.sendall(_pack_packet(1, SERVERDATA_AUTH, RCON_PASSWORD))

        authorized = False
        deadline = time.time() + RCON_TIMEOUT
        while time.time() < deadline:
            req_id, ptype, _body = _read_packet(sock)
            if ptype == SERVERDATA_AUTH_RESPONSE:
                if req_id == -1:
                    raise RconError("неверный пароль RCON")
                authorized = True
                break

        if not authorized:
            raise RconError("сервер не подтвердил авторизацию RCON")

        for index, command in enumerate(commands, start=1):
            sock.sendall(_pack_packet(index, SERVERDATA_EXECCOMMAND, command))
            _req_id, _ptype, body = _read_packet(sock)
            replies.append({"command": command, "reply": body})

    return replies


def rcon_call(commands):
    """Одна задача за раз, чтобы не плодить соединения."""
    global _queue_count

    _queue_count += 1
    try:
        with _rcon_lock:
            return rcon_execute(commands)
    finally:
        _queue_count -= 1


def queue_depth():
    return max(0, _queue_count - 1)


# --------------------------------------------------------------------------
#  Последнее отправленное положение (защита от дублей)
# --------------------------------------------------------------------------

_last_sent = {}
_last_lock = threading.Lock()

_BRANCH_WORDS = {"branch", "on", "1", "true", "yes", "боковой", "на боковой путь"}
_STRAIGHT_WORDS = {"straight", "off", "0", "false", "no", "прямо", "main"}


def normalize_position(value):
    if isinstance(value, bool):
        return "branch" if value else "straight"
    if isinstance(value, (int, float)):
        return "branch" if value else "straight"
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _BRANCH_WORDS:
            return "branch"
        if word in _STRAIGHT_WORDS:
            return "straight"
    return None


# --------------------------------------------------------------------------
#  Общее состояние стрелок + рассылка всем открытым страницам (SSE)
# --------------------------------------------------------------------------

_state_lock = threading.Lock()
_switch_state = {}          # номер -> "straight" | "branch"
STATE_FILE = os.path.join(BASE_DIR, "state.json")

_subs_lock = threading.Lock()
_subscribers = set()


def get_state():
    with _state_lock:
        return dict(_switch_state)


def _load_saved_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(snapshot):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def set_switch_state(number, position):
    with _state_lock:
        _switch_state[str(number)] = position
        snapshot = dict(_switch_state)
    _save_state(snapshot)
    broadcast({"type": "state", "switches": snapshot})
    return snapshot


def reset_state():
    saved = _load_saved_state()
    with _state_lock:
        _switch_state.clear()
        for number in switch_numbers():
            position = saved.get(str(number))
            _switch_state[str(number)] = "branch" if position == "branch" else "straight"
        return dict(_switch_state)


def subscribe():
    channel = queue.Queue(maxsize=64)
    with _subs_lock:
        _subscribers.add(channel)
    return channel


def unsubscribe(channel):
    with _subs_lock:
        _subscribers.discard(channel)


def broadcast(payload):
    with _subs_lock:
        targets = list(_subscribers)
    for channel in targets:
        try:
            channel.put_nowait(payload)
        except queue.Full:
            try:
                channel.get_nowait()
                channel.put_nowait(payload)
            except Exception:
                pass


# --------------------------------------------------------------------------
#  HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "RailwayRCON/1.1"
    protocol_version = "HTTP/1.1"

    # -- утилиты ----------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _cors(self):
        origin = self.headers.get("Origin", "")
        if ALLOW_ORIGIN == "*":
            allowed = "*"
        else:
            pairs = [item.strip() for item in ALLOW_ORIGIN.split(",") if item.strip()]
            allowed = origin if origin in pairs else ""

        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key, X-Admin-Key")
            self.send_header("Access-Control-Max-Age", "600")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _api_key_ok(self, value=None):
        if not API_KEY:
            return True
        provided = value if value is not None else self.headers.get("X-Api-Key", "")
        return provided == API_KEY

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _authorized(self):
        if not (SITE_USER and SITE_PASS):
            return True

        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
            except Exception:
                decoded = ""
            if ":" in decoded:
                user, password = decoded.split(":", 1)
                if user == SITE_USER and password == SITE_PASS:
                    return True

        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Railway switches", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _admin_ok(self):
        if not ADMIN_PASSWORD:
            return True
        return self.headers.get("X-Admin-Key", "") == ADMIN_PASSWORD

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # -- маршруты ---------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if not self._authorized():
            return

        if path == "/api/events":
            # EventSource не умеет заголовки, поэтому ключ можно передать в ?key=
            key = parse_qs(parsed.query).get("key", [""])[0]
            if not self._api_key_ok(key):
                return self._json(401, {"ok": False, "error": "Неверный ключ доступа"})
            return self.api_events()

        if path.startswith("/api/") and path != "/api/health" and not self._api_key_ok():
            return self._json(401, {"ok": False, "error": "Неверный ключ доступа"})

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as fh:
                    body = fh.read()
            except OSError as err:
                return self._json(500, {"ok": False, "error": "index.html не найден: %s" % err})
            return self._send(200, body, "text/html; charset=utf-8")

        if path == "/api/health":
            return self._json(200, {
                "ok": True,
                "mode": "dry-run" if DRY_RUN else "live",
                "rcon": {
                    "host": RCON_HOST,
                    "port": RCON_PORT,
                    "configured": HAS_CREDS,
                    "timeout": int(RCON_TIMEOUT * 1000),
                },
                "admin": {"password_required": bool(ADMIN_PASSWORD)},
                "switches": switch_numbers(),
                "state": get_state(),
                "queue": queue_depth(),
            })

        if path == "/api/ping":
            return self.api_ping()

        if path == "/api/log":
            with _log_lock:
                lines = list(_logs)
            return self._json(200, {"ok": True, "logs": lines})

        if path == "/api/admin/switches":
            if not self._admin_ok():
                return self._json(401, {"ok": False, "error": "Неверный пароль администратора"})
            return self._json(200, {
                "ok": True,
                "defaults": {"block": DEFAULT_BLOCK, "clear": DEFAULT_CLEAR},
                "switches": admin_state(),
            })

        return self._json(404, {"ok": False, "error": "Не найдено"})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)

        if not self._authorized():
            return

        if path.startswith("/api/") and not self._api_key_ok():
            return self._json(401, {"ok": False, "error": "Неверный ключ доступа"})

        if path == "/api/admin/switches":
            if not self._admin_ok():
                return self._json(401, {"ok": False, "error": "Неверный пароль администратора"})
            return self.api_admin_save()

        if not path.startswith("/api/switch/"):
            return self._json(404, {"ok": False, "error": "Не найдено"})

        return self.api_switch(path[len("/api/switch/"):].strip())

    # -- переключение стрелки ---------------------------------------------

    def api_switch(self, number):
        body = self._read_json()
        if body is None:
            return self._json(400, {"ok": False, "error": "Тело запроса должно быть JSON"})

        position = normalize_position(body.get("position", body.get("state")))
        if not position:
            return self._json(400, {
                "ok": False,
                "error": 'Не понял положение. Ожидается position: "straight" или "branch".',
            })

        commands = commands_for(number, position)
        if not commands:
            return self._json(404, {
                "ok": False,
                "error": "Нет команды для стрелки %s: задай координаты в админ-панели" % number,
            })

        if queue_depth() >= MAX_QUEUE:
            return self._json(503, {
                "ok": False,
                "error": "Слишком много запросов к RCON, попробуй ещё раз через секунду.",
            })

        now = time.time() * 1000.0
        with _last_lock:
            previous = _last_sent.get(number)

        if previous and previous["position"] == position and now - previous["at"] < DEDUPE_MS:
            return self._json(200, {
                "ok": True,
                "skipped": True,
                "mode": "dry-run" if DRY_RUN else "live",
                "switch": number,
                "position": position,
                "commands": commands,
            })

        if DRY_RUN:
            with _last_lock:
                _last_sent[number] = {"position": position, "at": now}
            set_switch_state(number, position)
            log("[DRY-RUN] стрелка %s -> %s: %s" % (number, position, " | ".join(commands)))
            return self._json(200, {
                "ok": True,
                "mode": "dry-run",
                "switch": number,
                "position": position,
                "commands": commands,
            })

        try:
            replies = rcon_call(commands)
        except RconError as err:
            log("[RCON] стрелка %s -> %s: ОШИБКА %s" % (number, position, err))
            return self._json(502, {
                "ok": False,
                "error": "Не удалось отправить команду в Minecraft: %s" % err,
                "switch": number,
                "position": position,
            })
        except (socket.timeout, TimeoutError):
            log("[RCON] стрелка %s -> %s: ТАЙМАУТ" % (number, position))
            return self._json(504, {
                "ok": False,
                "error": "Minecraft не ответил за %d мс (RCON-порт закрыт?)" % int(RCON_TIMEOUT * 1000),
                "switch": number,
                "position": position,
            })
        except OSError as err:
            log("[RCON] стрелка %s -> %s: %s" % (number, position, err))
            return self._json(502, {
                "ok": False,
                "error": "Ошибка сети при подключении к RCON: %s" % err,
                "switch": number,
                "position": position,
            })

        with _last_lock:
            _last_sent[number] = {"position": position, "at": now}
        set_switch_state(number, position)

        answer = " ; ".join(
            (item.get("reply") or "").strip().replace("\n", " ") for item in replies
        ).strip()
        log("[RCON] стрелка %s -> %s: %s%s" % (
            number, position, " | ".join(commands),
            ("  ->  " + answer) if answer else "",
        ))

        return self._json(200, {
            "ok": True,
            "mode": "live",
            "switch": number,
            "position": position,
            "commands": commands,
            "replies": replies,
        })

    # -- админ-панель: координаты -----------------------------------------

    def api_admin_save(self):
        body = self._read_json()
        if body is None:
            return self._json(400, {"ok": False, "error": "Тело запроса должно быть JSON"})

        payload = body.get("switches")
        if not isinstance(payload, dict):
            return self._json(400, {"ok": False, "error": "Ожидается объект switches"})

        result = {}
        errors = []

        for raw_number, raw_item in payload.items():
            number = str(raw_number).strip()
            if not number.isdigit():
                continue
            item = raw_item if isinstance(raw_item, dict) else {}

            try:
                x = parse_coord(item.get("x"))
                y = parse_coord(item.get("y"))
                z = parse_coord(item.get("z"))
                block = parse_block(item.get("block"), DEFAULT_BLOCK)
                clear = parse_block(item.get("clear"), DEFAULT_CLEAR)
            except ValueError as err:
                errors.append("стрелка %s: %s" % (number, err))
                continue

            # либо все три координаты, либо ни одной
            if (x is None) != (y is None) or (x is None) != (z is None):
                errors.append("стрелка %s: нужны все три координаты (X, Y, Z)" % number)
                continue

            result[number] = {
                "x": x,
                "y": y,
                "z": z,
                "block": block,
                "clear": clear,
            }

        if errors:
            return self._json(400, {"ok": False, "error": "; ".join(errors)})

        if not result:
            return self._json(400, {"ok": False, "error": "Нечего сохранять"})

        try:
            save_switches(result)
        except OSError as err:
            log("[ADMIN] не удалось записать switches.json: %s" % err)
            return self._json(500, {"ok": False, "error": "Не удалось записать switches.json: %s" % err})

        configured = [n for n, cfg in result.items() if has_coords(cfg)]
        log("[ADMIN] координаты сохранены: %s" % (", ".join(sorted(configured, key=int)) or "нет"))

        return self._json(200, {
            "ok": True,
            "switches": admin_state(),
            "configured": sorted(configured, key=int),
        })

    # -- живая синхронизация (SSE) ----------------------------------------

    def api_events(self):
        channel = subscribe()

        try:
            self.protocol_version = "HTTP/1.0"   # поток без Content-Length
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            self._sse_send({"type": "state", "switches": get_state()})

            while True:
                try:
                    payload = channel.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self._sse_send(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe(channel)
            self.close_connection = True

    def _sse_send(self, payload):
        line = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    # -- проверка связи ---------------------------------------------------

    def api_ping(self):
        if DRY_RUN:
            return self._json(200, {
                "ok": True,
                "mode": "dry-run",
                "message": (
                    "Режим заглушки включён принудительно (DRY_RUN=true)"
                    if HAS_CREDS
                    else "RCON не настроен: заполни RCON_HOST / RCON_PORT / RCON_PASSWORD в .env"
                ),
            })

        started = time.time()
        try:
            replies = rcon_call(["list"])
        except (RconError, OSError, socket.timeout, TimeoutError) as err:
            log("[RCON] ping FAIL: %s" % err)
            return self._json(502, {"ok": False, "error": str(err) or err.__class__.__name__})

        elapsed = int((time.time() - started) * 1000)
        log("[RCON] ping ok (%d мс)" % elapsed)
        return self._json(200, {
            "ok": True,
            "mode": "live",
            "elapsed_ms": elapsed,
            "replies": replies,
        })


# --------------------------------------------------------------------------
#  Запуск
# --------------------------------------------------------------------------

def main():
    if not os.path.exists(INDEX_FILE):
        print("index.html не найден рядом с server.py", file=sys.stderr)
        return 1

    try:
        httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    except OSError as err:
        print("Не удалось занять %s:%s — %s" % (BIND, PORT, err), file=sys.stderr)
        return 1

    httpd.daemon_threads = True
    reset_state()

    shown = "localhost" if BIND in ("0.0.0.0", "::") else BIND
    configured = [n for n, cfg in admin_state().items() if cfg["configured"]]
    log("-----------------------------------------------")
    log("Сайт:      http://%s:%d" % (shown, PORT))
    log("Режим:     %s" % ("ЗАГЛУШКА (команды только в лог)" if DRY_RUN else "БОЕВОЙ (RCON)"))
    if not DRY_RUN:
        log("RCON:      %s:%d (таймаут %d мс)" % (RCON_HOST, RCON_PORT, int(RCON_TIMEOUT * 1000)))
    log("Стрелки:   %s" % ", ".join(switch_numbers()))
    log("Координ.:  %s" % (", ".join(sorted(configured, key=int)) or "не заданы"))
    log("Админка:   %s" % ("пароль задан" if ADMIN_PASSWORD else "без пароля"))
    log("-----------------------------------------------")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("Остановлено пользователем")
    finally:
        httpd.server_close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
