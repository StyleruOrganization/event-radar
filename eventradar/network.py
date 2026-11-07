"""Verified HTTPS using the runtime's configured or native certificate store."""

import os
import socket
import ssl

import httpx
import truststore


def tls_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE")
    capath = os.environ.get("SSL_CERT_DIR")
    if cafile or capath:
        return ssl.create_default_context(cafile=cafile, capath=capath)
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def connection_error_message(error: httpx.RequestError) -> str:
    """Explain connection failures without exposing URLs, credentials or headers."""
    cause: BaseException | None = error
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return "Не удалось проверить TLS-сертификат OpenAI. Проверьте доверенные сертификаты системы или SSL_CERT_FILE/SSL_CERT_DIR."
        if isinstance(cause, socket.gaierror):
            return "Не удалось определить сетевой адрес OpenAI. Проверьте DNS и подключение к сети."
        cause = cause.__cause__ or cause.__context__
    if isinstance(error, httpx.TimeoutException):
        return "Истекло время ожидания OpenAI. Проверьте соединение и повторите позже."
    return "Не удалось установить HTTPS-соединение с OpenAI. Проверьте сеть, VPN или настройки прокси; повторный вход сам по себе это не исправляет."
