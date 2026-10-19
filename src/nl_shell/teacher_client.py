"""The HTTP client to the local model server: a ``Backend`` that speaks chat-completions.

``TeacherClient`` posts one model input (the message list ``nl_shell.prompts.build``
makes) to an OpenAI-compatible ``/v1/chat/completions`` endpoint, as served by llama.cpp's
``llama-server`` or by vLLM, and returns ``choices[0].message.content`` as raw text. It
never parses, repairs or retries the answer: the REPL hands it to the one strict
``proposal/v1`` parser (``nl_shell.proposal``, ADR-0018 §3). The request asks for
constrained decoding with ``proposal.SCHEMA`` itself, never a copy. No API key is sent.

The endpoint is a Unix socket (``Endpoint("unix", path)``; ``path=None`` means there is no
default socket, so every request fails as "isn't running") or an ``http://`` /
``https://`` URL. A URL's path is the base the client posts under, with a trailing ``/v1``
dropped: ``http://h:8080``, ``http://h:8080/`` and ``http://h:8080/v1`` all post to
``/v1/chat/completions``. A URL with no port uses the scheme's default (80 or 443). A URL
holding a space, a control character or a non-ASCII character, user info
(``user@host``), a query or a fragment is refused.

The transport is the standard library's ``http.client``, not ``urllib``: it reads no
``HTTP(S)_PROXY`` variable and follows no redirect (a 3xx is an error, read before the
body). ``https://`` checks the certificate and the host name: the client builds its
context with ``ssl.create_default_context()`` for each request and keeps
``verify_mode = CERT_REQUIRED`` and ``check_hostname = True``; it never falls back to
plain HTTP.

One total deadline, ``timeout_s`` from the start of ``complete``, covers everything: for a
TCP endpoint the name lookup (``socket.getaddrinfo``, which has no timeout of its own),
each connect attempt, the TLS handshake, sending the request and reading the response. The
whole exchange runs in a worker thread; the calling thread waits for it only until the
deadline, then shuts the exchange's sockets down (which wakes any read the worker is
blocked in, including one inside ``http.client``'s chunked reader) and raises. The worker
also bounds each socket wait by the time left and checks the deadline and the size cap
(``max_response_bytes``) after every chunk it reads. Every wait value is clamped to
``MAX_WAIT_S``, so a huge ``timeout_s`` cannot overflow ``settimeout`` or a timer.

Every failure is a ``nl_shell.backend.BackendError`` subclass whose text says why; any text
that came from the server (a reason phrase, a body excerpt, an exception message such as
``http.client``'s ``BadStatusLine``, which holds the raw status line) is folded to one line,
cut to ``EXCERPT_CHARS`` and passed through ``nl_shell.render.escape`` when the error is
built, so it can never carry a terminal escape sequence, a line break or a long run of its
own text to the screen.
"""

from __future__ import annotations

import http.client
import json
import math
import socket
import ssl
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from nl_shell import proposal, render
from nl_shell.backend import BackendError
from nl_shell.prompts import Message

MAX_RESPONSE_BYTES = 64 * 1024
# The longest single wait handed to `settimeout` or `Event.wait`. The deadline itself is
# not clamped: a longer `timeout_s` just means more waits of this length.
MAX_WAIT_S = 24 * 60 * 60.0
CHAT_PATH = "/v1/chat/completions"
DEFAULT_PORTS = {"http": 80, "https": 443}
SCHEMA_NAME = "proposal_v1"
# How much of an error body goes into the error text.
EXCERPT_CHARS = 200
_READ_CHUNK = 16 * 1024

Kind = Literal["unix", "http", "https"]


@dataclass(frozen=True)
class Endpoint:
    """Where the model server listens: a Unix socket path or an http(s) URL."""

    kind: Kind
    address: str | None


@dataclass(frozen=True)
class HttpTarget:
    """What an http(s) endpoint resolves to: the host and port connected to, the path."""

    tls: bool
    host: str
    port: int
    path: str


class ServerNotRunning(BackendError):
    """Nothing answers at the endpoint (no socket path, no socket, connection refused)."""


class BadEndpoint(BackendError):
    """The endpoint or the client's settings cannot be used."""


class DeadlineExceeded(BackendError):
    """The server did not answer in full within ``timeout_s``."""


class HttpStatusError(BackendError):
    """The server answered with an error status."""


class RedirectRefused(BackendError):
    """The server answered with a redirect; the client never follows one."""


class ResponseTooLarge(BackendError):
    """The response body is over ``max_response_bytes``."""


class BadResponse(BackendError):
    """The body is not chat-completions JSON with a string ``message.content``."""


class TransportError(BackendError):
    """Connecting, TLS, sending or reading failed."""


def _quote(text: str) -> str:
    """Server text, made safe to show: every hidden character escaped."""
    return render.escape(text)


def _excerpt(text: str) -> str:
    """Server text, made short and safe to show: one line, at most ``EXCERPT_CHARS``.

    Runs of whitespace (LF and CR included) fold to one space and the text is cut before
    it is escaped, so a server cannot print lines of its own or a long run of text into
    the REPL's reply through a reason phrase, a body or an exception message.
    """
    return _quote(" ".join(text.split())[:EXCERPT_CHARS])


def _describe(exc: BaseException) -> str:
    text = str(exc)
    name = type(exc).__name__
    return _excerpt(f"{name}: {text}" if text else name)


def http_target(endpoint: Endpoint) -> HttpTarget:
    """The host, port and request path for an ``http`` / ``https`` endpoint."""
    url = endpoint.address
    if url is None:
        raise BadEndpoint(f"no URL for the {endpoint.kind} endpoint")
    if not url.isascii() or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
        raise BadEndpoint(
            f"the URL {_quote(url)!r} holds a space, a control or a non-ASCII character"
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BadEndpoint(f"the URL {_quote(url)!r} does not parse: {_describe(exc)}")
    if parts.scheme != endpoint.kind:
        raise BadEndpoint(f"the URL {url!r} is not an {endpoint.kind}:// URL")
    if "@" in parts.netloc:
        raise BadEndpoint(f"the URL {url!r} holds user info, which is refused")
    if parts.query or parts.fragment or url.endswith(("?", "#")):
        raise BadEndpoint(f"the URL {url!r} holds a query or a fragment")
    host = parts.hostname
    if not host:
        raise BadEndpoint(f"the URL {url!r} names no host")
    base = parts.path.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return HttpTarget(
        tls=endpoint.kind == "https",
        host=host,
        port=DEFAULT_PORTS[endpoint.kind] if port is None else port,
        path=base + CHAT_PATH,
    )


def _timeout(timeout_s: object) -> float:
    """``timeout_s`` as a finite, positive float; ``BadEndpoint`` for anything else.

    The value is converted before it is checked: a Python int of any size passes an
    ``isinstance`` test, and one too big for a float (``10**400``) raises
    ``OverflowError`` from ``float`` or ``math.isfinite``. The error text never holds the
    value itself, whose ``repr`` can be huge or can fail for a very long int.
    """
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise BadEndpoint(
            f"timeout_s must be a positive number, not a {type(timeout_s).__name__}"
        )
    try:
        seconds = float(timeout_s)
    except OverflowError:
        raise BadEndpoint("timeout_s is too large to use") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise BadEndpoint("timeout_s must be a positive, finite number of seconds")
    return seconds


def _wait_for(seconds: float) -> float:
    """A wait value ``settimeout`` and ``Event.wait`` accept: in [0, MAX_WAIT_S]."""
    return max(0.0, min(seconds, MAX_WAIT_S))


class _Exchange:
    """One request's deadline and sockets, shared by the worker and the waiting caller."""

    def __init__(self, timeout_s: float) -> None:
        self.timeout_s = timeout_s
        self.deadline = time.monotonic() + timeout_s
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._abandoned = False

    def remaining(self) -> float:
        """The wait left before the deadline; raises once it has passed."""
        left = self.deadline - time.monotonic()
        if left <= 0 or self._abandoned:
            raise self.expired()
        return _wait_for(left)

    def expired(self) -> DeadlineExceeded:
        return DeadlineExceeded(f"no full answer within {self.timeout_s:g} s")

    def adopt(self, sock: socket.socket) -> None:
        """Track ``sock`` so ``abandon`` can wake it; refuse it once abandoned."""
        with self._lock:
            if self._abandoned:
                sock.close()
                raise self.expired()
            self._sockets.append(sock)

    def arm(self) -> None:
        """Bound the next blocking socket call by the time left."""
        left = self.remaining()
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            if sock.fileno() != -1:
                sock.settimeout(left)

    def abandon(self) -> None:
        """Shut every socket down, waking whatever the worker is blocked in."""
        with self._lock:
            self._abandoned = True
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass


class _UnixConnection(http.client.HTTPConnection):
    """HTTP over a Unix socket. The Host header is ``localhost``."""

    def __init__(self, path: str, exchange: _Exchange) -> None:
        super().__init__("localhost")
        self._path = path
        self._exchange = exchange

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._exchange.adopt(sock)
        sock.settimeout(self._exchange.remaining())
        sock.connect(self._path)
        self.sock = sock


class _TcpConnection(http.client.HTTPConnection):
    """HTTP or HTTPS over TCP; lookup, connect and handshake bounded by the deadline."""

    def __init__(self, target: HttpTarget, exchange: _Exchange) -> None:
        super().__init__(target.host, target.port)
        self._target = target
        self._exchange = exchange
        if target.tls:
            # So the Host header leaves out the port only when it is 443.
            self.default_port = DEFAULT_PORTS["https"]

    def connect(self) -> None:
        sock = self._open()
        if self._target.tls:
            context = ssl.create_default_context()
            context.verify_mode = ssl.CERT_REQUIRED
            context.check_hostname = True
            sock.settimeout(self._exchange.remaining())
            sock = context.wrap_socket(sock, server_hostname=self._target.host)
            self._exchange.adopt(sock)
        self.sock = sock

    def _open(self) -> socket.socket:
        infos = socket.getaddrinfo(
            self._target.host, self._target.port, type=socket.SOCK_STREAM
        )
        last: OSError | None = None
        for family, kind, proto, _, address in infos:
            left = self._exchange.remaining()
            sock = socket.socket(family, kind, proto)
            self._exchange.adopt(sock)
            try:
                sock.settimeout(left)
                sock.connect(address)
                return sock
            except OSError as exc:
                last = exc
                sock.close()
        raise last if last is not None else OSError("the host name resolved to nothing")


class TeacherClient:
    """A ``Backend`` (``nl_shell.backend``) talking to an OpenAI-compatible model server."""

    def __init__(
        self,
        endpoint: Endpoint,
        *,
        timeout_s: float,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        model: str | None = None,
    ) -> None:
        if endpoint.kind not in ("unix", "http", "https"):
            raise BadEndpoint(f"unknown endpoint kind {_quote(str(endpoint.kind))!r}")
        self.timeout_s = _timeout(timeout_s)
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise BadEndpoint("max_response_bytes must be a positive integer")
        self.endpoint = endpoint
        self.max_response_bytes = max_response_bytes
        self.model = model

    def request_body(self, messages: Sequence[Message]) -> dict[str, Any]:
        """The JSON document posted for one model input."""
        body: dict[str, Any] = {
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": SCHEMA_NAME,
                    "strict": True,
                    "schema": proposal.SCHEMA,
                },
            },
        }
        if self.model is not None:
            body["model"] = self.model
        return body

    def complete(self, messages: Sequence[Message]) -> str:
        """The raw ``message.content`` for one model input; a ``BackendError`` if none."""
        exchange = _Exchange(self.timeout_s)
        result: dict[str, Any] = {}
        done = threading.Event()

        def work() -> None:
            try:
                result["content"] = self._post(messages, exchange)
            except BaseException as exc:  # handed to the caller, mapped there
                result["error"] = exc
            finally:
                exchange.close()
                done.set()

        worker = threading.Thread(target=work, name="nl-shell-model-request", daemon=True)
        finished = False
        try:
            worker.start()
            while not done.wait(_wait_for(exchange.deadline - time.monotonic())):
                if time.monotonic() >= exchange.deadline:
                    raise exchange.expired()
            finished = True
        finally:
            if not finished:
                exchange.abandon()
        if "error" in result:
            raise self._as_backend_error(result["error"], exchange)
        content: str = result["content"]
        return content

    def _as_backend_error(self, exc: BaseException, exchange: _Exchange) -> BaseException:
        if isinstance(exc, BackendError):
            return exc
        if not isinstance(exc, Exception):
            return exc  # KeyboardInterrupt and the like stay what they are
        if isinstance(exc, TimeoutError) or time.monotonic() >= exchange.deadline:
            return exchange.expired()
        if self.endpoint.kind == "unix" and isinstance(
            exc, (FileNotFoundError, ConnectionRefusedError)
        ):
            return ServerNotRunning(f"no answer at {_quote(str(self.endpoint.address))}")
        if isinstance(exc, ConnectionRefusedError):
            return ServerNotRunning(f"no answer at {_quote(str(self.endpoint.address))}")
        if isinstance(exc, ssl.SSLError):
            return TransportError(f"TLS failed: {_describe(exc)}")
        return TransportError(_describe(exc))

    def _post(self, messages: Sequence[Message], exchange: _Exchange) -> str:
        """Connect, send, read and decode. Runs in the worker thread."""
        conn: http.client.HTTPConnection
        if self.endpoint.kind == "unix":
            if self.endpoint.address is None:
                raise ServerNotRunning("no socket path: XDG_RUNTIME_DIR is not set")
            path = CHAT_PATH
            conn = _UnixConnection(self.endpoint.address, exchange)
        else:
            target = http_target(self.endpoint)
            path = target.path
            conn = _TcpConnection(target, exchange)
        payload = json.dumps(self.request_body(messages)).encode("utf-8")
        try:
            conn.connect()
            exchange.arm()
            conn.request(
                "POST",
                path,
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            exchange.arm()
            response = conn.getresponse()
            return self._content(response, exchange)
        finally:
            conn.close()

    def _content(self, response: http.client.HTTPResponse, exchange: _Exchange) -> str:
        status = response.status
        reason = _excerpt(response.reason or "")
        if 300 <= status < 400:
            raise RedirectRefused(
                f"the server answered {status} {reason}, a redirect, which is not followed"
            )
        if status != 200:
            excerpt = self._read(response, exchange, EXCERPT_CHARS * 4, strict=False)
            text = _excerpt(excerpt.decode("utf-8", errors="replace"))
            detail = f": {text}" if text else ""
            raise HttpStatusError(f"the server answered {status} {reason}{detail}")
        if response.length is not None and response.length > self.max_response_bytes:
            raise ResponseTooLarge(
                f"the response is {response.length} bytes, over the "
                f"{self.max_response_bytes}-byte limit"
            )
        body = self._read(response, exchange, self.max_response_bytes, strict=True)
        return _message_content(body)

    def _read(
        self,
        response: http.client.HTTPResponse,
        exchange: _Exchange,
        cap: int,
        *,
        strict: bool,
    ) -> bytes:
        """At most ``cap`` bytes of the body; over it, an error (``strict``) or a cut."""
        data = bytearray()
        while True:
            exchange.arm()
            chunk = response.read1(min(_READ_CHUNK, cap + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data += chunk
            if len(data) > cap:
                if strict:
                    raise ResponseTooLarge(
                        f"the response is over the {self.max_response_bytes}-byte limit"
                    )
                return bytes(data[:cap])


def _message_content(body: bytes) -> str:
    """``choices[0].message.content`` of a chat-completions body, which must be a string."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BadResponse(f"the response is not JSON: {_describe(exc)}")
    try:
        content = doc["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BadResponse("the response has no choices[0].message.content")
    if not isinstance(content, str):
        raise BadResponse("choices[0].message.content is not a string")
    return content
