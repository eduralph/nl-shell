"""The HTTP client to the local model server (``nl_shell.teacher_client``).

Every test talks to a fake OpenAI-compatible server built on the standard library
(``socketserver`` + ``http.server``), on ``127.0.0.1`` port 0 or on a Unix socket in a
short temporary directory: no network, model or GPU. The fake writes raw bytes, so a test
controls the status line, the headers and the pace of the body exactly.

The nl_shell modules are imported inside the tests and fixtures, never at module level:
the C4 gate re-runs this file with the production change reverted, and a module-level
import of the new client would stop the file from loading instead of failing its tests.

The TLS leg captures the ``ssl.SSLContext`` the client uses by wrapping
``ssl.create_default_context`` with ``monkeypatch`` (the client builds its context with
that function for each https request).
"""

from __future__ import annotations

import http.server
import io
import json
import os
import shutil
import socket
import socketserver
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

VALID = '{"kind": "command", "command": "df -h", "risk": "read-only"}'
# Every timing leg: the server stalls for at least this long, the client must give up
# within BOUND_S. The wide gap keeps a loaded CI host from flipping the result.
STALL_S = 5.0
BOUND_S = 2.0
DRIP_EVERY_S = 0.2


# --- the fake server ----------------------------------------------------------------


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


@dataclass
class Fake:
    """One fake server: what it answers (``script``) and every request it saw."""

    script: object
    requests: list[Seen] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    url: str = ""
    path: str = ""
    port: int = 0

    def stall(self, seconds: float) -> None:
        self.stop.wait(seconds)


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 10  # the server side never waits on a client forever

    def do_POST(self) -> None:
        self._answer()

    def do_GET(self) -> None:
        self._answer()

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        seen = Seen(
            self.command,
            self.path,
            {key.lower(): value for key, value in self.headers.items()},
            body,
        )
        fake: Fake = self.server.fake  # type: ignore[attr-defined]
        fake.requests.append(seen)
        try:
            fake.script(self.wfile, fake)
        except OSError:
            pass  # the client hung up, as it should on a stall
        self.close_connection = True

    def log_message(self, *args) -> None:
        pass


class _Quiet:
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        pass


class _TcpServer(_Quiet, socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


class _UnixServer(_Quiet, socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    pass


def response(status: str = "200 OK", body: bytes = b"", headers=()) -> bytes:
    head = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", "Connection: close"]
    head += [f"{name}: {value}" for name, value in headers]
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body


def completion(content) -> bytes:
    doc = {"choices": [{"index": 0, "message": {"role": "assistant", "content": content}}]}
    return json.dumps(doc).encode("utf-8")


def answer(content=VALID):
    def script(out, fake):
        out.write(response(body=completion(content)))

    return script


def answers(*contents):
    """Answer each request with the next content, in order."""
    queue = list(contents)

    def script(out, fake):
        out.write(response(body=completion(queue.pop(0))))

    return script


@pytest.fixture
def serve():
    """``serve(script, unix=False)`` starts a fake server, shut down in teardown."""
    started = []

    def start(script, *, unix: bool = False) -> Fake:
        fake = Fake(script)
        if unix:
            directory = tempfile.mkdtemp(prefix="nls-")
            fake.path = os.path.join(directory, "s")
            server = _UnixServer(fake.path, _Handler)
        else:
            directory = None
            server = _TcpServer(("127.0.0.1", 0), _Handler)
            fake.port = server.server_address[1]
            fake.url = f"http://127.0.0.1:{fake.port}"
        server.fake = fake  # type: ignore[attr-defined]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        started.append((fake, server, thread, directory))
        return fake

    yield start
    for fake, server, thread, directory in started:
        fake.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


def messages():
    from nl_shell.prompts import Message

    return (
        Message("system", "You turn requests into bash."),
        Message("user", "show disk usage"),
        Message("assistant", VALID),
        Message("user", "list my files"),
    )


def client(url_or_path, *, kind="http", timeout_s=5.0, cap=None, model=None):
    from nl_shell import teacher_client

    kwargs = {"timeout_s": timeout_s, "model": model}
    if cap is not None:
        kwargs["max_response_bytes"] = cap
    return teacher_client.TeacherClient(
        teacher_client.Endpoint(kind, url_or_path), **kwargs
    )


def fails(make_and_call):
    """Run ``make_and_call``; it must raise a BackendError. Returns (error, seconds)."""
    from nl_shell.backend import BackendError

    start = time.monotonic()
    with pytest.raises(BackendError) as info:
        make_and_call()
    elapsed = time.monotonic() - start
    assert str(info.value), "a BackendError must say why"
    return info.value, elapsed


# --- (a) the request ----------------------------------------------------------------


def test_returns_the_content_and_sends_the_chat_completions_request(serve):
    from nl_shell import proposal

    fake = serve(answer())
    sent = messages()

    assert client(fake.url, model="m1").complete(sent) == VALID

    assert len(fake.requests) == 1
    seen = fake.requests[0]
    assert (seen.method, seen.path) == ("POST", "/v1/chat/completions")
    doc = seen.json()
    assert doc["messages"] == [{"role": m.role, "content": m.content} for m in sent]
    assert doc["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "proposal_v1", "strict": True, "schema": proposal.SCHEMA},
    }
    assert doc["model"] == "m1"
    assert "authorization" not in seen.headers
    assert seen.headers["content-type"] == "application/json"


def test_no_model_key_without_a_model(serve):
    fake = serve(answer())
    assert client(fake.url, model=None).complete(messages()) == VALID
    assert "model" not in fake.requests[0].json()
    assert "authorization" not in fake.requests[0].headers


def test_unix_socket_request_sends_host_localhost(serve):
    fake = serve(answer(), unix=True)
    assert client(fake.path, kind="unix").complete(messages()) == VALID
    assert len(fake.requests) == 1
    seen = fake.requests[0]
    assert (seen.method, seen.path) == ("POST", "/v1/chat/completions")
    assert seen.headers["host"] == "localhost"
    assert "authorization" not in seen.headers


@pytest.mark.parametrize("suffix", ["/v1", "/v1/", "/", ""])
def test_a_trailing_v1_is_dropped_from_the_base(serve, suffix):
    fake = serve(answer())
    assert client(fake.url + suffix).complete(messages()) == VALID
    assert fake.requests[0].path == "/v1/chat/completions"


def test_a_base_path_is_kept(serve):
    fake = serve(answer())
    assert client(fake.url + "/api/v1").complete(messages()) == VALID
    assert fake.requests[0].path == "/api/v1/chat/completions"


# --- (b) strict handling ------------------------------------------------------------


def _settle(fake, count=1):
    """Give a wrongly retrying client time to send again; return what the fake saw."""
    time.sleep(0.3)
    assert len(fake.requests) == count, "the client must not retry"
    return fake.requests


def test_an_error_status_fails_once(serve):
    from nl_shell import teacher_client

    def script(out, fake):
        out.write(response("500 Internal Server Error", b"model exploded"))

    fake = serve(script)
    error, _ = fails(lambda: client(fake.url).complete(messages()))
    assert isinstance(error, teacher_client.HttpStatusError)
    assert "500" in str(error) and "model exploded" in str(error)
    _settle(fake)


def test_a_redirect_is_not_followed(serve):
    from nl_shell import teacher_client

    elsewhere = serve(answer())

    def script(out, fake):
        location = elsewhere.url + "/v1/chat/completions"
        out.write(response("307 Temporary Redirect", b"", [("Location", location)]))

    fake = serve(script)
    error, _ = fails(lambda: client(fake.url).complete(messages()))
    assert isinstance(error, teacher_client.RedirectRefused)
    assert "307" in str(error)
    _settle(fake)
    assert elsewhere.requests == []


def test_a_non_json_body_fails(serve):
    from nl_shell import teacher_client

    def script(out, fake):
        out.write(response(body=b"<html>not json</html>"))

    fake = serve(script)
    error, _ = fails(lambda: client(fake.url).complete(messages()))
    assert isinstance(error, teacher_client.BadResponse)
    _settle(fake)


@pytest.mark.parametrize(
    "doc",
    [
        {},
        [],
        {"choices": []},
        {"choices": {}},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": 5}}]},
        {"choices": [{"message": {"content": {"kind": "command"}}}]},
        {"choices": [{"message": "text"}]},
    ],
)
def test_json_without_a_string_content_fails(serve, doc):
    from nl_shell import teacher_client

    def script(out, fake):
        out.write(response(body=json.dumps(doc).encode()))

    fake = serve(script)
    error, _ = fails(lambda: client(fake.url).complete(messages()))
    assert isinstance(error, teacher_client.BadResponse)
    _settle(fake)


def test_a_body_over_the_cap_stops_the_read(serve):
    """The body never ends: a client that kept reading would hit the deadline instead."""
    from nl_shell import teacher_client

    def script(out, fake):
        out.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
        while not fake.stop.is_set():
            out.write(b"x" * 4096)

    fake = serve(script)
    error, elapsed = fails(
        lambda: client(fake.url, cap=1024, timeout_s=STALL_S).complete(messages())
    )
    assert isinstance(error, teacher_client.ResponseTooLarge)
    assert elapsed < BOUND_S
    _settle(fake)


def test_a_declared_length_over_the_cap_fails(serve):
    from nl_shell import teacher_client

    def script(out, fake):
        out.write(response(body=completion("x" * 4096)))

    fake = serve(script)
    error, _ = fails(lambda: client(fake.url, cap=1024).complete(messages()))
    assert isinstance(error, teacher_client.ResponseTooLarge)


def test_a_body_at_the_cap_is_read(serve):
    body = completion(VALID)

    def script(out, fake):
        out.write(response(body=body))

    fake = serve(script)
    assert client(fake.url, cap=len(body)).complete(messages()) == VALID


def test_an_error_body_at_the_excerpt_cap_does_not_wait_for_more(serve):
    """No length, exactly the excerpt cap, then the connection stays open."""
    from nl_shell import teacher_client

    def script(out, fake):
        out.write(b"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n")
        out.write(b"x" * (teacher_client.EXCERPT_CHARS * 4))
        out.flush()
        fake.stall(STALL_S)

    fake = serve(script)
    error, elapsed = fails(lambda: client(fake.url, timeout_s=STALL_S).complete(messages()))
    assert isinstance(error, teacher_client.HttpStatusError)
    assert "500" in str(error)
    assert elapsed < BOUND_S, f"waited {elapsed:.2f} s for a byte past the cap"


def _times_out(fake, **kwargs):
    from nl_shell import teacher_client

    error, elapsed = fails(
        lambda: client(fake.url, timeout_s=0.5, **kwargs).complete(messages())
    )
    assert isinstance(error, teacher_client.DeadlineExceeded)
    assert elapsed < BOUND_S, f"gave up after {elapsed:.2f} s"
    return error


def test_an_answer_that_lands_after_the_deadline_is_refused(monkeypatch):
    """The worker finishes, but the clock has passed the deadline by the time it does."""
    from nl_shell import teacher_client

    real_monotonic = time.monotonic
    late = threading.Event()

    def post_then_run_out_the_clock(self, messages, exchange):
        late.set()
        return VALID

    monkeypatch.setattr(
        time, "monotonic", lambda: real_monotonic() + (3600.0 if late.is_set() else 0.0)
    )
    monkeypatch.setattr(teacher_client.TeacherClient, "_post", post_then_run_out_the_clock)
    error, _ = fails(lambda: client("http://127.0.0.1:9").complete(messages()))
    assert isinstance(error, teacher_client.DeadlineExceeded)


def test_a_server_that_never_answers(serve):
    def script(out, fake):
        fake.stall(STALL_S)

    _times_out(serve(script))


def test_a_slow_drip_body(serve):
    def script(out, fake):
        out.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
        end = time.monotonic() + STALL_S
        while time.monotonic() < end and not fake.stop.is_set():
            out.write(b" ")
            fake.stall(DRIP_EVERY_S)

    _times_out(serve(script))


def test_a_chunked_slow_drip(serve):
    """The chunk-size line never ends; http.client reads it off the response's socket."""

    def script(out, fake):
        out.write(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
        )
        end = time.monotonic() + STALL_S
        while time.monotonic() < end and not fake.stop.is_set():
            out.write(b"0")
            fake.stall(DRIP_EVERY_S)

    _times_out(serve(script))


def test_a_slow_drip_status_line(serve):
    def script(out, fake):
        end = time.monotonic() + STALL_S
        for char in b"HTTP/1.1 200 OK" * 100:
            if time.monotonic() >= end or fake.stop.is_set():
                break
            out.write(bytes([char]))
            fake.stall(DRIP_EVERY_S)

    _times_out(serve(script))


def test_https_to_a_plain_http_server_is_a_tls_error(serve, monkeypatch):
    from nl_shell import teacher_client

    contexts = []
    real = ssl.create_default_context

    def recording(*args, **kwargs):
        context = real(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(ssl, "create_default_context", recording)

    fake = serve(answer())
    url = f"https://127.0.0.1:{fake.port}"
    error, _ = fails(lambda: client(url, kind="https", timeout_s=3).complete(messages()))
    assert isinstance(
        error, (teacher_client.TransportError, teacher_client.DeadlineExceeded)
    )
    time.sleep(0.3)
    assert fake.requests == [], "no plain-HTTP request may reach the server"

    assert len(contexts) == 1, "the context must come from create_default_context"
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True


def test_a_huge_timeout_is_clamped_and_works(serve):
    fake = serve(answer())
    assert client(fake.url, timeout_s=1e10).complete(messages()) == VALID


def test_a_tiny_timeout_fails_cleanly(serve):
    fake = serve(answer())
    fails(lambda: client(fake.url, timeout_s=1e-5).complete(messages()))


@pytest.mark.parametrize(
    "timeout_s",
    [0, -1, float("nan"), float("inf"), 10**400, -(10**400), 10**5000, "5", None],
    ids=["0", "-1", "nan", "inf", "10**400", "-10**400", "10**5000", "str", "None"],
)
def test_an_unusable_timeout_is_refused(timeout_s):
    """An int too big for a float (10**400) is a BackendError, never an OverflowError."""
    fails(lambda: client("http://127.0.0.1:1", timeout_s=timeout_s))


@pytest.mark.parametrize(
    "url",
    [
        "http://bad host:8080/",
        "http://bad\x01host:8080/",
        "http://bad\nhost/",
        "http://bad\thost/",
        "http://bad\x7fhost/",
        "http://127.0.0.1:8080/v1 /",
        "http://127.0.0.1:notaport/",
        "http://[::1/",
        "http://user:pw@127.0.0.1:8080/",
        "http://user@127.0.0.1:8080/",
        "http://127.0.0.1:8080/?q=1",
        "ftp://127.0.0.1:8080/",
        "http:///v1",
    ],
)
def test_a_bad_url_is_refused(url, monkeypatch):
    from nl_shell import teacher_client

    def no_lookup(*args, **kwargs):
        raise AssertionError("a refused URL must not be looked up")

    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)
    error, _ = fails(lambda: client(url).complete(messages()))
    assert isinstance(error, teacher_client.BadEndpoint)


def test_userinfo_never_reaches_the_server(serve):
    fake = serve(answer())
    url = f"http://user:pw@127.0.0.1:{fake.port}/"
    fails(lambda: client(url).complete(messages()))
    time.sleep(0.3)
    assert fake.requests == []


@pytest.mark.parametrize(
    "kind, url, host, port, path",
    [
        ("http", "http://[::1]/", "::1", 80, "/v1/chat/completions"),
        ("http", "http://localhost/", "localhost", 80, "/v1/chat/completions"),
        ("http", "http://localhost", "localhost", 80, "/v1/chat/completions"),
        ("https", "https://localhost/v1", "localhost", 443, "/v1/chat/completions"),
        ("http", "http://[::1]:8080/", "::1", 8080, "/v1/chat/completions"),
    ],
)
def test_the_address_connected_to(kind, url, host, port, path, monkeypatch):
    from nl_shell import teacher_client

    target = teacher_client.http_target(teacher_client.Endpoint(kind, url))
    assert (target.host, target.port, target.path) == (host, port, path)
    assert target.tls is (kind == "https")

    looked_up = []

    def lookup(name, number, *args, **kwargs):
        looked_up.append((name, number))
        raise socket.gaierror(socket.EAI_NONAME, "not in this test")

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    fails(lambda: client(url, kind=kind).complete(messages()))
    assert looked_up == [(host, port)]


def test_ipv6_loopback_with_a_port(serve):
    try:
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind(("::1", 0))
    except OSError:
        pytest.skip("no IPv6 loopback here")
    probe.listen(1)
    port = probe.getsockname()[1]

    def accept_one():
        conn, _ = probe.accept()
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            head, _, rest = data.partition(b"\r\n\r\n")
            fields = dict(
                line.lower().split(b":", 1) for line in head.split(b"\r\n")[1:]
            )
            length = int(fields[b"content-length"])
            while len(rest) < length:
                rest += conn.recv(4096)
            conn.sendall(response(body=completion(VALID)))

    thread = threading.Thread(target=accept_one, daemon=True)
    thread.start()
    try:
        assert client(f"http://[::1]:{port}/").complete(messages()) == VALID
    finally:
        thread.join(timeout=5)
        probe.close()


# --- (c) one deadline covers lookup and connect ---------------------------------------


def test_a_hanging_name_lookup_is_abandoned(monkeypatch):
    from nl_shell import teacher_client

    def slow_lookup(*args, **kwargs):
        time.sleep(STALL_S)
        raise socket.gaierror(socket.EAI_AGAIN, "too slow")

    monkeypatch.setattr(socket, "getaddrinfo", slow_lookup)
    error, elapsed = fails(
        lambda: client("http://model.invalid:8080", timeout_s=0.5).complete(messages())
    )
    assert isinstance(error, teacher_client.DeadlineExceeded)
    assert elapsed < BOUND_S


def test_hanging_connects_share_one_deadline(monkeypatch):
    from nl_shell import teacher_client

    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (f"127.0.0.{n}", 9))
        for n in (2, 3, 4, 5)
    ]
    attempts = []

    def lookup(*args, **kwargs):
        return addresses

    def hanging_connect(sock, address):
        attempts.append(address)
        time.sleep(STALL_S)  # ignores the socket's own timeout, like a stuck SYN
        raise TimeoutError("connect hung")

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    monkeypatch.setattr(socket.socket, "connect", hanging_connect)
    error, elapsed = fails(
        lambda: client("http://model.example:9", timeout_s=0.5).complete(messages())
    )
    assert isinstance(error, teacher_client.DeadlineExceeded)
    assert elapsed < BOUND_S, f"{elapsed:.2f} s: one deadline in total, not per address"
    assert attempts, "the client must have tried to connect"


def test_a_hanging_connect_without_a_patch_is_bounded():
    """A real listener whose accept queue is full: the SYN is dropped, connect hangs."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    port = listener.getsockname()[1]
    fillers = []
    try:
        for _ in range(8):
            filler = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            filler.settimeout(0.2)
            fillers.append(filler)
            try:
                filler.connect(("127.0.0.1", port))
            except (TimeoutError, OSError):
                break
        else:
            pytest.skip("this kernel never fills the accept queue")
        start = time.monotonic()
        url = f"http://127.0.0.1:{port}"
        fails(lambda: client(url, timeout_s=0.5).complete(messages()))
        assert time.monotonic() - start < BOUND_S
    finally:
        for filler in fillers:
            filler.close()
        listener.close()


# --- (d) through the REPL -----------------------------------------------------------


@pytest.fixture
def loaded_prompts(tmp_path):
    from nl_shell import prompts

    folder = tmp_path / "target"
    (folder / "prompts").mkdir(parents=True)
    (folder / "prompts" / "prompts.yaml").write_text(
        "system: You turn one request into one bash command.\n"
        "examples:\n"
        "  - request: show disk usage\n"
        "    response: '" + VALID + "'\n"
        "request_template: |-\n"
        "  State:\n"
        "  {state}\n"
        "  Request: {request}\n",
        encoding="utf-8",
    )
    return prompts.load(folder)


def run_repl(backend, prompts, *lines):
    """Run a REPL over ``lines``; return its output and how many lines it read."""
    from nl_shell.repl import Repl

    queue = list(lines)
    reads = []

    def read(prompt):
        reads.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)

    out = io.StringIO()
    assert Repl(read, out, backend=backend, prompts=prompts).run() == 0
    assert len(reads) == len(lines) + 1, "every line must be read, then end of input"
    return out.getvalue()


def test_repl_shows_the_framed_proposal(serve, loaded_prompts):
    fake = serve(answer())
    out = run_repl(client(fake.url), loaded_prompts, "show disk usage")
    assert out.startswith("Proposed command (risk: read-only, 1 line):\n  | df -h\n")
    assert fake.requests[0].json()["messages"][-1]["content"].endswith("show disk usage")


REJECTS = {
    "extra property": '{"kind": "command", "command": "ls", "risk": "read-only", "x": 1}',
    "missing risk": '{"kind": "command", "command": "ls"}',
    "command with reason": (
        '{"kind": "command", "command": "ls", "risk": "read-only", '
        '"reason": "unclear-request"}'
    ),
    "command over 2000 code points": json.dumps(
        {"kind": "command", "command": "é" * 2001, "risk": "read-only"}
    ),
    "invalid JSON": '{"kind": "command", ',
    "a JSON array": '[{"kind": "command", "command": "ls", "risk": "read-only"}]',
}


@pytest.mark.parametrize("raw", REJECTS.values(), ids=REJECTS.keys())
def test_repl_rejects_a_bad_proposal_and_reads_on(serve, loaded_prompts, raw):
    fake = serve(answers(raw, VALID))
    out = run_repl(client(fake.url), loaded_prompts, "list files", "show disk usage")
    first, _, rest = out.partition("\n")
    assert first == "No usable proposal came back from the model."
    assert rest.startswith("Proposed command (risk: read-only, 1 line):")


def test_repl_with_no_socket_says_the_server_is_not_running(loaded_prompts):
    directory = tempfile.mkdtemp(prefix="nls-")
    try:
        path = os.path.join(directory, "missing.sock")
        backend = client(path, kind="unix")
        out = run_repl(backend, loaded_prompts, "show disk usage", "list files")
        line = f"The model server isn't running (no answer at {path})."
        assert out == f"{line}\n{line}\n"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_a_socket_path_with_a_newline_stays_on_one_line(loaded_prompts):
    directory = tempfile.mkdtemp(prefix="nls-")
    try:
        path = os.path.join(directory, "missing\nnl> \tfake.sock")
        out = run_repl(client(path, kind="unix"), loaded_prompts, "show disk usage")
        assert out.count("\n") == 1 and "\t" not in out
        assert out.startswith("The model server isn't running (no answer at ")
        assert not any(line.startswith("nl>") for line in out.splitlines())
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_repl_with_no_socket_path_says_the_server_is_not_running(
    loaded_prompts, monkeypatch
):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    backend = client(None, kind="unix")
    out = run_repl(backend, loaded_prompts, "show disk usage", "list files")
    line = "The model server isn't running (no socket path: XDG_RUNTIME_DIR is not set)."
    assert out == f"{line}\n{line}\n"


def test_repl_catches_any_backend_error(loaded_prompts):
    """A stub backend, no teacher_client: only the catch in Repl._propose can pass this."""
    from nl_shell import backend

    class Failing:
        def __init__(self):
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            raise backend.BackendError("boom")

    failing = Failing()
    out = run_repl(failing, loaded_prompts, "show disk usage", "list files")
    line = "The model server could not be used: boom."
    assert out == f"{line}\n{line}\n"
    assert failing.calls == 2


HOSTILE_STATUS = b"HTTP/1.1 500 \x1b]52;c;ZWNobyBwd25lZA==\x07\x1b[2K\rnl> fake\r\n"


def test_a_hostile_status_line_never_reaches_the_terminal(serve, loaded_prompts):
    def script(out, fake):
        body = b"\x1b[31merror\x07\r\x1b]0;title\x07"
        out.write(
            HOSTILE_STATUS
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )

    fake = serve(script)
    error, _ = fails(lambda: client(fake.url).complete(messages()))
    reason = str(error)
    for char in ("\x1b", "\x07", "\r"):
        assert char not in reason

    out = run_repl(client(fake.url), loaded_prompts, "show disk usage", "list files")
    for char in ("\x1b", "\x07", "\r"):
        assert char not in out
    first, second, end = out.split("\n")
    assert first == f"The model server could not be used: {reason}."
    assert first.startswith("The model server could not be used: the server answered 500")
    assert second == first and end == ""


LONG_SPOOF = b"Proposed command (risk: read-only) " + b"A" * 5000


@pytest.mark.parametrize(
    "status_line",
    [
        # Does not parse: http.client raises BadStatusLine holding the whole raw line.
        b"HTTP/1.1 " + LONG_SPOOF + b"\x1b[2K\r\n",
        # Parses: a 500 whose reason phrase is the long text.
        b"HTTP/1.1 500 " + LONG_SPOOF + b"\x1b[2K\r\n",
    ],
    ids=["malformed", "long-reason"],
)
def test_a_long_status_line_is_cut_to_one_short_line(serve, loaded_prompts, status_line):
    from nl_shell import teacher_client

    def script(out, fake):
        out.write(status_line + b"Content-Length: 0\r\nConnection: close\r\n\r\n")

    fake = serve(script)
    error, _ = fails(lambda: client(fake.url).complete(messages()))
    reason = str(error)
    assert "\n" not in reason and "\x1b" not in reason and "\r" not in reason
    assert reason.count("A") <= teacher_client.EXCERPT_CHARS

    out = run_repl(client(fake.url), loaded_prompts, "show disk usage")
    assert out == f"The model server could not be used: {reason}.\n"
    assert out.count("\n") == 1
    # The fixed prefix plus at most one cut, escaped excerpt (each hidden character
    # becomes at most 10 characters), not the server's 5000-character line.
    assert len(out) < 600
    for char in ("\x1b", "\x07", "\r"):
        assert char not in out


def test_any_other_failure_is_shown_with_its_reason(serve, loaded_prompts):
    def script(out, fake):
        out.write(response("503 Service Unavailable", b"loading model"))

    fake = serve(script)
    backend = client(fake.url)
    error, _ = fails(lambda: backend.complete(messages()))
    out = run_repl(backend, loaded_prompts, "show disk usage")
    assert out == f"The model server could not be used: {error}.\n"
    assert "503" in out and "loading model" in out


def test_a_tcp_server_that_refuses_is_not_running(loaded_prompts):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    url = f"http://127.0.0.1:{port}"
    out = run_repl(client(url), loaded_prompts, "show disk usage")
    assert out == f"The model server isn't running (no answer at {url}).\n"


def test_backend_error_is_the_base_of_every_client_error():
    from nl_shell import backend, teacher_client

    for name in (
        "ServerNotRunning",
        "BadEndpoint",
        "DeadlineExceeded",
        "HttpStatusError",
        "RedirectRefused",
        "ResponseTooLarge",
        "BadResponse",
        "TransportError",
    ):
        assert issubclass(getattr(teacher_client, name), backend.BackendError)
    assert teacher_client.MAX_RESPONSE_BYTES == 64 * 1024
    assert Path(teacher_client.__file__).name == "teacher_client.py"
