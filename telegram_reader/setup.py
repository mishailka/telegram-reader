"""Loopback-only, temporary browser wizard. Credentials never go through MCP."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
import socket
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route
from telethon import errors
from telethon.sessions import StringSession

from .core import make_client, name
from .storage import load_account, save_account, remove_account

logging.getLogger("telethon").setLevel(logging.CRITICAL)


class Wizard:
    def __init__(self):
        self.client = None
        self.account = None
        self.phone = None
        self.code_hash = None
        self.state = "start"
        self.saved = False
        self.lock = asyncio.Lock()

    async def close(self):
        if self.client:
            if not self.saved:
                try:
                    if await self.client.is_user_authorized():
                        await asyncio.wait_for(self.client.log_out(), 10)
                except Exception:
                    pass
            await self.client.disconnect()

    async def action(self, action, payload):
        if action == "status":
            return {"state": self.state, "has_account": load_account() is not None}
        if action == "resume":
            if self.client:
                raise ValueError("Окно подключения уже активно. Продолжите текущий шаг.")
            self.account = load_account()
            if not self.account:
                raise ValueError("Сохранённого подключения нет.")
            self.saved = True
            self.client = make_client(self.account)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                await self.client.disconnect()
                self.client = None
                remove_account()
                self.state = "start"
                self.saved = False
                raise ValueError("Сессия отозвана. Обновите страницу и войдите заново.")
            self.state = "scope"
        elif action == "begin":
            if load_account() or self.client:
                raise ValueError("Сначала отключите существующее подключение либо продолжите текущий вход.")
            api_id = int(payload.get("api_id", 0))
            api_hash = str(payload.get("api_hash", "")).strip()
            phone = str(payload.get("phone", "")).strip().replace(" ", "")
            if api_id <= 0 or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                raise ValueError("Проверьте API ID, API Hash и номер телефона в формате +79991234567.")
            self.account = {"api_id": api_id, "api_hash": api_hash, "allowed_chat_ids": []}
            self.phone = phone
            self.client = make_client(self.account)
            try:
                await self.client.connect()
                sent = await self.client.send_code_request(phone)
                self.code_hash = sent.phone_code_hash
            except BaseException:
                await self.client.disconnect()
                self.client = None
                raise
            self.state = "code"
        elif action == "code":
            if self.state != "code":
                raise ValueError("Сначала запросите код входа.")
            code = str(payload.get("code", "")).strip().replace(" ", "")
            if not re.fullmatch(r"[0-9]{4,8}", code):
                raise ValueError("Введите код из Telegram.")
            try:
                await self.client.sign_in(self.phone, code, phone_code_hash=self.code_hash)
                self.state = "scope"
            except errors.SessionPasswordNeededError:
                self.state = "password"
        elif action == "password":
            if self.state != "password":
                raise ValueError("Пароль сейчас не требуется.")
            await self.client.sign_in(password=str(payload.get("password", "")))
            self.state = "scope"
        elif action == "chats":
            if self.state != "scope":
                raise ValueError("Сначала войдите в Telegram.")
            chats = []
            async for dialog in self.client.iter_dialogs(limit=2000):
                chats.append({"id": str(dialog.id), "title": dialog.name})
            me = await self.client.get_me()
            return {"state": "scope", "name": name(me), "chats": chats,
                    "allowed_chat_ids": self.account.get("allowed_chat_ids", []),
                    "scan_limit_reached": len(chats) == 2000}
        elif action == "save":
            if self.state != "scope":
                raise ValueError("Сначала войдите в Telegram.")
            ids = payload.get("allowed_chat_ids")
            if ids is not None and (not isinstance(ids, list) or len(ids) > 2000 or not all(isinstance(i, str) and re.fullmatch(r"-?[1-9][0-9]{0,19}", i) for i in ids)):
                raise ValueError("Некорректный список чатов.")
            if ids == []:
                raise ValueError("Выберите хотя бы один чат или доступ ко всем чатам.")
            self.account["allowed_chat_ids"] = sorted(set(ids)) if ids is not None else None
            self.account["session"] = StringSession.save(self.client.session)
            save_account(self.account)
            self.saved = True
            self.state = "done"
            self.phone = self.code_hash = None
        elif action == "disconnect":
            if not self.client:
                self.account = load_account()
                if self.account:
                    self.client = make_client(self.account)
                    self.saved = True
                    await self.client.connect()
            if self.client:
                if await self.client.is_user_authorized():
                    await self.client.log_out()
                await self.client.disconnect()
                self.client = None
            remove_account()
            self.account = None
            self.saved = False
            self.state = "start"
            self.phone = self.code_hash = None
        elif action != "resume":
            raise ValueError("Неизвестная операция.")
        return {"state": self.state}


def create_app(token: str, origin: str, wizard: Wizard | None = None):
    wizard = wizard or Wizard()
    deadline = time.monotonic() + 20 * 60
    hostname = origin.removeprefix("http://")
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
               "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}

    async def page(request):
        if request.headers.get("host") != hostname:
            return HTMLResponse("Forbidden", status_code=403, headers=headers)
        return HTMLResponse(Path(__file__).with_name("setup.html").read_text(encoding="utf-8"), headers=headers)

    async def api(request: Request):
        if (time.monotonic() > deadline or request.headers.get("host") != hostname
                or request.headers.get("origin") != origin
                or not secrets.compare_digest(request.headers.get("x-setup-token", ""), token)):
            return JSONResponse({"error": "Окно недействительно. Запустите Connect.cmd заново."}, status_code=403, headers=headers)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                return JSONResponse({"error": "Слишком большой запрос."}, status_code=413, headers=headers)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Некорректный запрос.")
            async with wizard.lock:
                result = await asyncio.wait_for(wizard.action(request.path_params["action"], payload), 60)
            return JSONResponse(result, headers=headers)
        except errors.FloodWaitError as exc:
            error = f"Telegram просит подождать {exc.seconds} секунд. Не повторяйте запрос до этого времени."
        except errors.PhoneCodeInvalidError:
            error = "Неверный код входа. Проверьте последнее сообщение Telegram."
        except errors.PhoneCodeExpiredError:
            error = "Код истёк. Запустите Connect.cmd заново."
        except errors.PasswordHashInvalidError:
            error = "Неверный пароль двухэтапной проверки."
        except (ValueError, json.JSONDecodeError) as exc:
            error = str(exc) if not isinstance(exc, json.JSONDecodeError) else "Некорректный запрос."
        except (TimeoutError, OSError, ConnectionError):
            error = "Не удалось связаться с Telegram. Проверьте соединение."
        except errors.RPCError as exc:
            error = f"Telegram отклонил запрос ({type(exc).__name__})."
        except Exception:
            error = "Не удалось завершить операцию. Перезапустите окно подключения."
        return JSONResponse({"error": error}, status_code=400, headers=headers)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await wizard.close()

    return Starlette(routes=[Route("/", page), Route("/api/{action}", api, methods=["POST"])], lifespan=lifespan)


async def run(port=0, open_browser=True):
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(20)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    token = secrets.token_urlsafe(32)
    app = create_app(token, origin)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="critical"))
    url = f"{origin}/#{token}"
    print(f"Telegram Reader setup (valid 20 minutes): {url}", flush=True)

    async def browser_when_ready():
        while not server.started:
            await asyncio.sleep(0.1)
        if open_browser:
            webbrowser.open(url)

    async def expire():
        await asyncio.sleep(20 * 60)
        server.should_exit = True

    tasks = [asyncio.create_task(browser_when_ready()), asyncio.create_task(expire())]
    try:
        await server.serve(sockets=[listener])
    finally:
        for task in tasks:
            task.cancel()
        listener.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    asyncio.run(run(args.port, not args.no_browser))
