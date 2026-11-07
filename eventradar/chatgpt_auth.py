"""Owner-only Sign in with ChatGPT OAuth and protected runtime credentials.

No API-key fallback, browser-cookie extraction, or access to Codex credentials.
"""

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import tempfile
import time
import uuid
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from eventradar.network import connection_error_message, tls_context

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
USAGE_URL = "https://chatgpt.com/settings/usage"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class AuthRequired(RuntimeError):
    """A fresh, interactive owner authorization is required."""


def private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise AuthRequired("Каталог авторизации не должен быть символической ссылкой.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_uid != os.geteuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise AuthRequired("Файл авторизации должен принадлежать владельцу и иметь права 600.")
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError) as error:
        raise AuthRequired("Файл авторизации поврежден или недоступен.") from error
    if not isinstance(value, dict):
        raise AuthRequired("Неверный формат файла авторизации.")
    return value


@contextmanager
def credential_lock(directory: Path) -> Iterator[None]:
    descriptor = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def http_client() -> httpx.Client:
    return httpx.Client(verify=tls_context(), timeout=30, follow_redirects=False)


def verify_identity(client: httpx.Client, token: str, client_id: str, *, nonce: str | None = None, subject: str | None = None) -> dict:
    discovery = client.get(ISSUER + "/.well-known/openid-configuration")
    discovery.raise_for_status()
    configuration = discovery.json()
    jwks_uri = configuration.get("jwks_uri", "")
    if configuration.get("issuer") != ISSUER or urlsplit(jwks_uri).netloc != "auth.openai.com" or not jwks_uri.startswith("https://"):
        raise AuthRequired("OpenAI вернул неожиданные параметры проверки авторизации.")
    response = client.get(jwks_uri)
    response.raise_for_status()
    header = jwt.get_unverified_header(token)
    if header.get("alg") not in {"RS256", "ES256"}:
        raise AuthRequired("Неподдерживаемая подпись ID token.")
    key = next((key for key in response.json()["keys"] if key.get("kid") == header.get("kid")), None)
    if key is None:
        raise AuthRequired("Не найден ключ подписи OpenAI.")
    identity = jwt.decode(
        token, jwt.PyJWK.from_dict(key).key, algorithms=[header["alg"]],
        issuer=ISSUER, audience=client_id, leeway=5,
        options={"require": ["iss", "aud", "sub", "exp", "iat"]},
    )
    if not isinstance(identity["sub"], str) or not identity["sub"]:
        raise AuthRequired("ID token не содержит идентификатор аккаунта.")
    if nonce is not None and not hmac.compare_digest(str(identity.get("nonce", "")), nonce):
        raise AuthRequired("Не совпал nonce авторизации.")
    if subject is not None and identity["sub"] != subject:
        raise AuthRequired("Авторизован другой аккаунт; прежняя сессия сохранена.")
    return identity


def token_record(previous: dict, response: dict) -> dict:
    if str(response.get("token_type", "")).lower() != "bearer" or not response.get("access_token"):
        raise AuthRequired("OpenAI не выдал access token.")
    scopes = response.get("scope")
    granted = scopes.split() if isinstance(scopes, str) else previous.get("scopes", [])
    if "chatgpt.tokens.use.direct" not in granted or "resource.invoke" not in granted:
        raise AuthRequired("Не предоставлено разрешение на использование подписки ChatGPT. Повторите вход и разрешите ChatGPT plan usage.")
    expiry = int(response.get("expires_in", 0))
    if expiry <= 0:
        raise AuthRequired("Не указан срок действия access token.")
    return {
        **previous,
        "access_token": response["access_token"],
        "refresh_token": response.get("refresh_token") or previous.get("refresh_token"),
        "id_token": response.get("id_token") or previous.get("id_token"),
        "scopes": granted, "expires_at": time.time() + expiry,
    }


class ChatGPTCredentials:
    def __init__(self, directory: Path):
        self.directory = private_directory(directory)
        self.path = self.directory / "credentials.json"
        with credential_lock(self.directory):
            host_path = self.directory / "host.json"
            if not host_path.exists():
                atomic_json(host_path, {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())})

    def profile(self) -> dict:
        return read_json(self.path)

    def access_token(self, force_refresh: bool = False) -> str:
        # Also serializes CLI and runtime refreshes across processes.
        with credential_lock(self.directory):
            profile = self.profile()
            if not profile.get("access_token") or not profile.get("subject"):
                raise AuthRequired("Подписка не подключена. Выполните python -m eventradar.chatgpt_auth login на своем компьютере.")
            if "chatgpt.tokens.use.direct" not in profile.get("scopes", []):
                raise AuthRequired("Разрешение ChatGPT plan usage не предоставлено.")
            if not force_refresh and profile.get("expires_at", 0) > time.time() + 120:
                return profile["access_token"]
            if not profile.get("refresh_token"):
                raise AuthRequired("Сессия ChatGPT истекла. Выполните повторный вход.")
            with http_client() as client:
                response = client.post(TOKEN, data={
                    "grant_type": "refresh_token", "client_id": profile["client_id"],
                    "refresh_token": profile["refresh_token"], "resource": RESOURCE,
                })
                if response.status_code in {400, 401, 403}:
                    raise AuthRequired("OpenAI отклонил обновление сессии. Подключите подписку заново.")
                response.raise_for_status()
                payload = response.json()
                updated = token_record(profile, payload)
                # Persist rotated credentials before other operations can fail.
                atomic_json(self.path, updated)
                if payload.get("id_token"):
                    try:
                        verify_identity(client, payload["id_token"], profile["client_id"], subject=profile["subject"])
                    except Exception:
                        atomic_json(self.path, {**profile, "access_token": None, "refresh_token": None, "id_token": None})
                        raise AuthRequired("Не удалось подтвердить аккаунт после обновления. Требуется новый вход.") from None
                return updated["access_token"]

    def models(self) -> list[dict]:
        with http_client() as client:
            response = client.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + self.access_token()})
            if response.status_code == 401:
                response = client.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + self.access_token(True)})
            response.raise_for_status()
            models = response.json().get("models", [])
            return [model for model in models if model.get("visibility") == "list" and isinstance(model.get("slug"), str)]


def login(directory: Path, *, port: int, open_browser: bool) -> None:
    credentials = ChatGPTCredentials(directory)
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    callback: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass  # Callback URLs contain authorization codes.

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(values) != 1 for values in query.values()) or not hmac.compare_digest(query.get("state", [""])[0], state) or callback:
                self.send_error(400, "Invalid or expired OAuth state")
                return
            callback.update({key: value[0] for key, value in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write((
                '<!doctype html><html lang="ru"><meta charset="utf-8"><title>EventRadar</title>'
                '<script>history.replaceState(null,"","/auth/complete");</script>'
                '<p>Ответ получен. Вернитесь в терминал EventRadar для результата подключения.</p></html>'
            ).encode())

    with credential_lock(credentials.directory):
        current = credentials.profile()
        host_path = credentials.directory / "host.json"
        host = read_json(host_path)
        if not host.get("ext_agent_host_id"):
            host = {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())}
            atomic_json(host_path, host)
        registration_path = credentials.directory / "registration.json"
        registration = read_json(registration_path)
        client_id = current.get("client_id") or registration.get("client_id")
        with HTTPServer(("127.0.0.1", port), CallbackHandler) as server:
            server.timeout = 0.5
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            parameters = {
                "client_id": client_id or "dynamic_agent_client", "ext_agent_host_id": host["ext_agent_host_id"],
                "response_type": "code", "redirect_uri": redirect, "scope": SCOPES, "resource": RESOURCE,
                "state": state, "nonce": nonce, "code_challenge_method": "S256", "code_challenge": challenge,
            }
            if not client_id:
                parameters["agent_name_hint"] = "EventRadar"
            # No token hints in URLs printed to terminals or chat.
            url = AUTHORIZE + "?" + urlencode(parameters)
            print("Continue with ChatGPT — разрешите EventRadar использовать вашу подписку.", flush=True)
            print(url, flush=True)
            if open_browser:
                webbrowser.open(url)
            deadline = time.monotonic() + 600
            while not callback and time.monotonic() < deadline:
                server.handle_request()
        if not callback:
            raise AuthRequired("Время входа истекло. Запустите login повторно.")
        if callback.get("error"):
            raise AuthRequired("Авторизация отклонена. Сессия не изменена.")
        returned_id = callback.get("client_id") or client_id
        if not returned_id or returned_id == "dynamic_agent_client" or (client_id and returned_id != client_id):
            raise AuthRequired("OpenAI не вернул ожидаемый client ID.")
        if not client_id:
            atomic_json(registration_path, {"client_id": returned_id})
        with http_client() as client:
            response = client.post(TOKEN, data={
                "grant_type": "authorization_code", "client_id": returned_id, "code": callback.get("code", ""),
                "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE,
            })
            if response.status_code >= 400:
                raise AuthRequired("Обмен кода отклонен. Повторите login; выданный client ID сохранен.")
            payload = response.json()
            if not isinstance(payload.get("id_token"), str):
                raise AuthRequired("OpenAI не выдал ID token.")
            identity = verify_identity(client, payload["id_token"], returned_id, nonce=nonce, subject=current.get("subject"))
            profile = token_record({"client_id": returned_id, "subject": identity["sub"], "email": identity.get("email", "")}, payload)
            if not profile.get("refresh_token"):
                raise AuthRequired("Не выдан refresh token. Нужен offline_access для работы на сервере.")
            atomic_json(credentials.path, profile)
    print("Подписка ChatGPT подключена. Запросы EventRadar используют ее лимиты; отдельный API-биллинг не применяется.", flush=True)
    print("Управление использованием: " + USAGE_URL, flush=True)


def logout(credentials: ChatGPTCredentials) -> None:
    with credential_lock(credentials.directory):
        profile = credentials.profile()
        confirmed = not profile.get("refresh_token")
        if not confirmed:
            try:
                with http_client() as client:
                    discovery = client.get(ISSUER + "/.well-known/openid-configuration")
                    discovery.raise_for_status()
                    endpoint = discovery.json().get("revocation_endpoint", "")
                    if not endpoint.startswith("https://auth.openai.com/"):
                        raise AuthRequired("Неизвестный revocation endpoint.")
                    result = client.post(endpoint, data={"token": profile["refresh_token"], "token_type_hint": "refresh_token", "client_id": profile["client_id"]})
                    confirmed = result.status_code == 200
            except (httpx.HTTPError, ValueError, AuthRequired):
                confirmed = False
        for field in ("access_token", "refresh_token", "id_token"):
            profile.pop(field, None)
        atomic_json(credentials.path, profile)
    print("Локальная сессия удалена." + (" Сессия отозвана в OpenAI." if confirmed else " Отзыв в OpenAI не подтвержден; отключите приложение в настройках ChatGPT."))


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="EventRadar: ChatGPT subscription connection")
    parser.add_argument("--directory", type=Path, default=Path(os.environ.get("CHATGPT_AUTH_DIR", "data/chatgpt")))
    commands = parser.add_subparsers(dest="command", required=True)
    sign_in = commands.add_parser("login")
    sign_in.add_argument("--port", type=int, default=0)
    sign_in.add_argument("--no-browser", action="store_true")
    commands.add_parser("status")
    commands.add_parser("models")
    commands.add_parser("logout")
    commands.add_parser("import-session", help="Import an owner session over stdin, without copying its host ID")
    arguments = parser.parse_args()
    try:
        if arguments.command == "login":
            login(arguments.directory, port=arguments.port, open_browser=not arguments.no_browser)
        elif arguments.command == "models":
            for model in ChatGPTCredentials(arguments.directory).models():
                print(model["slug"] + " — " + model.get("display_name", model["slug"]))
        elif arguments.command == "logout":
            logout(ChatGPTCredentials(arguments.directory))
        elif arguments.command == "import-session":
            # SSH stdin avoids putting tokens in command arguments, environment or GitHub Secrets.
            incoming = json.loads(sys.stdin.read(128_000))
            credentials = ChatGPTCredentials(arguments.directory)
            with credential_lock(credentials.directory):
                existing = credentials.profile()
                if existing.get("access_token") or existing.get("refresh_token"):
                    raise AuthRequired("Сессия уже есть. Сначала остановите бот и выполните logout для замены.")
                if not isinstance(incoming, dict) or not all(incoming.get(key) for key in ("client_id", "subject", "access_token", "refresh_token", "id_token")):
                    raise AuthRequired("В импортируемой сессии отсутствуют обязательные поля.")
                if incoming["client_id"] == "dynamic_agent_client" or "chatgpt.tokens.use.direct" not in incoming.get("scopes", []):
                    raise AuthRequired("Сессия не разрешает использование подписки.")
                if existing.get("subject") and existing["subject"] != incoming["subject"]:
                    raise AuthRequired("Нельзя подменить аккаунт существующей регистрации.")
                with http_client() as client:
                    verify_identity(client, incoming["id_token"], incoming["client_id"], subject=incoming["subject"])
                incoming.pop("ext_agent_host_id", None)
                atomic_json(credentials.path, incoming)
            print("Сессия импортирована. Host ID сервера сохранен. С этого момента обновляет токены только сервер.")
        else:
            profile = ChatGPTCredentials(arguments.directory).profile()
            print("Подключено" if profile.get("access_token") and "chatgpt.tokens.use.direct" in profile.get("scopes", []) else "Нужен вход")
            if profile.get("email"):
                print("Аккаунт: " + profile["email"])
    except httpx.RequestError as error:
        print(connection_error_message(error))
        raise SystemExit(1) from None
    except (AuthRequired, httpx.HTTPError, jwt.PyJWTError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, AuthRequired) else f"Операция не завершена ({type(error).__name__}). Токены не выводятся; повторите позже.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
