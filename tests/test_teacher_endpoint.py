"""Which model endpoint a session uses, and the wiring of the client into ``cli.main``.

ADR-0003 and ADR-0020 §3: by default nl-shell talks only to
``$XDG_RUNTIME_DIR/nl-shell/model.sock`` over ``AF_UNIX`` and opens no TCP connection. Any
other endpoint, loopback TCP included, needs ``--dev-teacher`` for that session; a
``teacher_url`` in ``config.toml`` without the flag is refused. The request deadline is
``[model] timeout_s`` (ADR-0018 §7, ADR-0020 §5).

Every fake model server here is built on the standard library, on ``127.0.0.1`` port 0 or
on a Unix socket in a short temporary directory: no network, model or GPU. Every test that
touches the default socket sets ``XDG_RUNTIME_DIR`` itself (``tests/conftest.py`` does not
isolate it), and every server thread is shut down in teardown.

``nl_shell.teacher_client`` and ``nl_shell.prompts`` are imported inside the tests, never
at module level: the C4 gate re-runs this file with the production change reverted, and a
module-level import of ``select_endpoint`` would stop the file from loading instead of
failing its tests.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from nl_shell import cli, config
from nl_shell.repl import Repl

TARGET_TOML = """\
id = "testos-1"
display_name = "Test OS 1"
shell = "bash"
package_owner_query = ["owner-of"]

[os_release]
ID = "testos"
VERSION_ID = "1"

[sudo]
implementation = "test-sudo"
version = "1.0"
"""
PROMPTS_YAML = """\
system: You propose one bash command.
examples: []
request_template: "{state}{request}"
"""
VALID = '{"kind": "command", "command": "df -h", "risk": "read-only"}'
REQUEST = "show disk usage"
BOUND_S = 2.0


# Fixtures -------------------------------------------------------------------------------


@pytest.fixture
def runtime_dir(monkeypatch):
    """A short ``XDG_RUNTIME_DIR`` (Unix socket paths are limited to ~108 bytes)."""
    path = tempfile.mkdtemp(prefix="nls-")
    monkeypatch.setenv("XDG_RUNTIME_DIR", path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def default_socket(runtime_dir):
    return os.path.join(runtime_dir, "nl-shell", "model.sock")


@pytest.fixture
def config_file():
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "targets"
    (root / "testos-1" / "prompts").mkdir(parents=True)
    (root / "testos-1" / "target.toml").write_text(TARGET_TOML, encoding="utf-8")
    (root / "testos-1" / "prompts" / "prompts.yaml").write_text(
        PROMPTS_YAML, encoding="utf-8"
    )
    release = tmp_path / "os-release"
    release.write_text('ID=testos\nVERSION_ID="1"\n', encoding="utf-8")
    return root, release


class FakeServer:
    """A one-thread HTTP server on a listening socket that records every request.

    ``mode`` is ``"answer"`` (a chat-completions answer holding ``VALID``) or ``"drip"``
    (headers, then one body byte every 0.1 s until stopped).
    """

    def __init__(self, listener: socket.socket, mode: str = "answer") -> None:
        self.listener = listener
        self.listener.settimeout(0.05)
        self.mode = mode
        self.requests: list[bytes] = []
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            self.connections += 1
            try:
                self._handle(conn)
            except OSError:
                pass
            finally:
                conn.close()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        while len(body) < length:
            chunk = conn.recv(4096)
            if not chunk:
                break
            body += chunk
        self.requests.append(head + b"\r\n\r\n" + body)
        if self.mode == "drip":
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100000\r\n\r\n"
            )
            while not self._stop.wait(0.1):
                conn.sendall(b" ")
            return
        payload = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": VALID}}]}
        ).encode()
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.listener.close()


@pytest.fixture
def servers():
    started: list[FakeServer] = []

    def unix(path: str, mode: str = "answer") -> FakeServer:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(8)
        server = FakeServer(listener, mode)
        started.append(server)
        return server

    def tcp(mode: str = "answer") -> tuple[FakeServer, int]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        server = FakeServer(listener, mode)
        started.append(server)
        return server, listener.getsockname()[1]

    yield unix, tcp
    for server in started:
        server.stop()


def scripted(*lines):
    pending = list(lines)

    def read(prompt):
        if not pending:
            raise EOFError
        return pending.pop(0)

    return read


@pytest.fixture
def start(monkeypatch, capsys, setup):
    """Run the real ``cli.main``; the REPL it builds (with every keyword ``cli.main``
    passes, the backend included) reads ``lines`` then ends. Returns (code, out, err)."""
    root, release = setup

    def run(argv, *lines):
        out = io.StringIO()

        def interactive(cls, status_lines=(), **kwargs):
            return Repl(scripted(*lines), out, status_lines, **kwargs)

        monkeypatch.setattr(Repl, "interactive", classmethod(interactive))
        code = cli.main(argv, targets_root=root, os_release_paths=[release])
        err = capsys.readouterr().err
        assert "Traceback" not in err
        return code, out.getvalue(), err

    return run


# (a) config -----------------------------------------------------------------------------


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_timeout_defaults_to_10_without_a_model_table(tmp_path):
    assert config.load(write(tmp_path / "c.toml", 'target = "x"\n')).model_timeout_s == 10
    assert config.load(tmp_path / "missing.toml").model_timeout_s == 10


def test_timeout_is_read_from_the_model_table(tmp_path):
    loaded = config.load(write(tmp_path / "c.toml", "[model]\ntimeout_s = 0.5\n"))
    assert loaded.model_timeout_s == 0.5
    loaded = config.load(write(tmp_path / "c.toml", "[model]\ntimeout_s = 30\n"))
    assert loaded.model_timeout_s == 30


def test_timeout_upper_bound_is_a_named_constant(tmp_path):
    bound = config.MAX_MODEL_TIMEOUT_S
    assert 10 < bound < 1e10
    text = f"[model]\ntimeout_s = {bound!r}\n"
    assert config.load(write(tmp_path / "c.toml", text)).model_timeout_s == bound


@pytest.mark.parametrize(
    "value",
    ['"10"', "true", "false", "0", "0.0", "-1", "-0.5", "1e10", "inf", "nan", "[1]",
     "{ s = 1 }"],
)
def test_bad_timeout_is_refused(tmp_path, value):
    path = write(tmp_path / "c.toml", f"[model]\ntimeout_s = {value}\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load(path)
    assert str(path) in str(exc.value)
    assert "timeout_s" in str(exc.value)


def test_model_that_is_not_a_table_is_refused(tmp_path):
    with pytest.raises(config.ConfigError):
        config.load(write(tmp_path / "c.toml", "model = 5\n"))


def test_teacher_keys_load_as_strings(tmp_path):
    text = 'teacher_url = "http://127.0.0.1:8080"\nteacher_model = "qwen"\n'
    loaded = config.load(write(tmp_path / "c.toml", text))
    assert loaded.teacher_url == "http://127.0.0.1:8080"
    assert loaded.teacher_model == "qwen"
    empty = config.load(write(tmp_path / "d.toml", ""))
    assert (empty.teacher_url, empty.teacher_model) == (None, None)


@pytest.mark.parametrize("key", ["teacher_url", "teacher_model"])
@pytest.mark.parametrize("value", ["5", "true", '["x"]', '{ a = "x" }'])
def test_teacher_key_that_is_not_a_string_is_refused(tmp_path, key, value):
    path = write(tmp_path / "c.toml", f"{key} = {value}\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load(path)
    assert str(exc.value) == f"{path}: `{key}` must be a string"


# (b) endpoint rules ---------------------------------------------------------------------


RUN = "/run/user/1000"
SOCK = "/run/user/1000/nl-shell/model.sock"


def select(*args):
    from nl_shell import teacher_client

    return teacher_client.select_endpoint(*args)


def unix(path):
    from nl_shell.teacher_client import Endpoint

    return Endpoint("unix", path)


def test_default_is_the_socket_under_the_runtime_dir():
    assert select(None, None, RUN) == (unix(SOCK), [])


@pytest.mark.parametrize("runtime", [None, "", "run/user/1000", "./x"])
def test_unusable_runtime_dir_gives_no_socket_path(runtime):
    endpoint, messages = select(None, None, runtime)
    assert endpoint == unix(None)
    assert messages == []


def test_config_url_without_the_flag_is_refused():
    endpoint, messages = select(None, "http://127.0.0.1:8080", RUN)
    assert endpoint == unix(SOCK)
    assert len(messages) == 1
    assert "teacher_url" in messages[0]
    assert "http://127.0.0.1:8080" in messages[0]
    assert "--dev-teacher" in messages[0]
    assert "refused" in messages[0]


def test_config_url_with_the_flag_is_used():
    from nl_shell.teacher_client import Endpoint

    url = "http://127.0.0.1:8080"
    assert select(True, url, RUN) == (Endpoint("http", url), [])


def test_flag_url_wins_over_config_url():
    from nl_shell.teacher_client import Endpoint

    url = "https://teacher.example:8443/v1"
    assert select(url, "http://127.0.0.1:8080", RUN) == (Endpoint("https", url), [])
    assert select(url, None, RUN) == (Endpoint("https", url), [])


def test_flag_without_any_url_says_so_and_uses_the_socket():
    endpoint, messages = select(True, None, RUN)
    assert endpoint == unix(SOCK)
    assert len(messages) == 1
    assert "--dev-teacher" in messages[0]
    assert "URL" in messages[0]
    assert "teacher_url" in messages[0]


BAD_URLS = ["ftp://x", "unix:/x", "localhost:8080", "127.0.0.1:8080", "http://[::1",
            "not a url", "http://", "http://user@host:1", "http://h:99999"]


@pytest.mark.parametrize("url", BAD_URLS)
@pytest.mark.parametrize("source", ["flag", "config"])
def test_url_that_cannot_be_used_is_refused(url, source):
    args = (url, None) if source == "flag" else (True, url)
    endpoint, messages = select(*args, RUN)
    assert endpoint == unix(SOCK)
    assert len(messages) == 1
    assert url in messages[0]
    assert "refused" in messages[0]


@pytest.mark.parametrize("source", ["flag", "config"])
def test_refused_url_is_escaped_in_the_message(source):
    url = "ftp://\x1b[2Jx‮"
    args = (url, None) if source == "flag" else (True, url)
    endpoint, messages = select(*args, RUN)
    assert endpoint == unix(SOCK)
    assert len(messages) == 1
    assert "\x1b" not in messages[0] and "‮" not in messages[0]
    assert "x1b[2Jx" in messages[0]


def test_config_url_without_the_flag_is_escaped():
    _, messages = select(None, "http://\x1b[31mx", RUN)
    assert "\x1b" not in messages[0]


# An ESC byte, a real backslash followed by "x1b", and a quote: each must show once,
# in single quotes, and no two of them the same way.
SHOWN_URLS = [
    ("http://h\x1b", "'http://h\\x1b'"),
    ("http://h\\x1b", "'http://h\\\\x1b'"),
    ("http://h'\x1b", "'http://h'\\x1b'"),
]


@pytest.mark.parametrize(("url", "shown"), SHOWN_URLS)
@pytest.mark.parametrize("source", ["flag", "config", "no-flag"])
def test_url_in_a_message_is_shown_once_and_exactly(url, shown, source):
    args = {"flag": (url, None), "config": (True, url), "no-flag": (None, url)}[source]
    _, messages = select(*args, RUN)
    assert len(messages) == 1
    assert shown in messages[0]
    assert messages[0].count("http://h") == 1


def test_escape_and_backslash_urls_give_different_messages():
    # Without the flag every URL gets the same refusal text, so only the URL differs.
    shown = {select(None, url, RUN)[1][0] for url, _ in SHOWN_URLS}
    assert len(shown) == len(SHOWN_URLS)


@pytest.mark.parametrize(("url", "shown"), SHOWN_URLS)
def test_url_reaches_stderr_exactly_as_shown(start, runtime_dir, url, shown):
    code, _, err = start(["--dev-teacher", url])
    assert code == 0
    assert err.count(shown) == 1
    assert "\x1b" not in err


def test_socket_path_in_a_message_is_shown_exactly():
    _, messages = select(None, "http://127.0.0.1:1", "/run/a\\x1b\x1b")
    assert "'/run/a\\\\x1b\\x1b/nl-shell/model.sock'" in messages[0]


@pytest.mark.parametrize("url", ["http://[::1", "https://[::1]x", "http://h:99999",
                                 "http://h:port"])
def test_http_url_that_does_not_parse_says_so(url):
    _, messages = select(url, None, RUN)
    assert len(messages) == 1
    assert "does not parse" in messages[0]
    assert "must be an http:// or https:// URL" not in messages[0]


@pytest.mark.parametrize(("url", "reason"), [
    ("http://user@host:1", "user info"),
    ("http://h:1/?q", "query or a fragment"),
    ("http://", "names no host"),
    ("http://h x", "a space, a control or a non-ASCII character"),
    ("ftp://x", "must be an http:// or https:// URL"),
])
def test_refusal_gives_the_real_reason(url, reason):
    _, messages = select(url, None, RUN)
    assert len(messages) == 1
    assert reason in messages[0]
    assert messages[0].count(url) == 1


@pytest.mark.parametrize(
    "url",
    ["http://192.0.2.1:8080", "http://teacher.lan:8080", "http://10.0.0.1",
     "http://[2001:db8::1]:80", "http://localhost.example:1", "http://128.0.0.1"],
)
def test_plain_http_to_a_non_loopback_host_warns(url):
    from nl_shell.teacher_client import Endpoint

    endpoint, messages = select(url, None, RUN)
    assert endpoint == Endpoint("http", url)
    assert len(messages) == 1
    assert "warning" in messages[0]
    assert "SSH tunnel" in messages[0]
    assert "TLS" in messages[0] or "https://" in messages[0]


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8080", "http://LOCALHOST", "http://127.0.0.1:8080",
     "http://127.1.2.3", "http://[::1]:8080", "https://192.0.2.1:8443",
     "https://teacher.example"],
)
def test_loopback_http_and_any_https_do_not_warn(url):
    endpoint, messages = select(url, None, RUN)
    assert endpoint.address == url
    assert messages == []


def test_select_endpoint_opens_no_connection(monkeypatch):
    calls = []
    monkeypatch.setattr(socket.socket, "connect", lambda *a: calls.append(a))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: calls.append(a))
    for args in [(None, None, RUN), ("http://teacher.lan:1", None, RUN),
                 (True, "https://x.example", RUN)]:
        select(*args)
    assert calls == []


# (c) messages and the flag through cli.main ---------------------------------------------


def test_dev_teacher_flag_parses(start, runtime_dir):
    code, _, _ = start(["--dev-teacher"])
    assert code == 0
    code, _, _ = start(["--dev-teacher", "http://127.0.0.1:1"])
    assert code == 0


def test_config_url_refusal_reaches_stderr_once(start, runtime_dir, config_file):
    config_file.write_text('teacher_url = "http://127.0.0.1:8080"\n', encoding="utf-8")
    code, _, err = start([])
    assert code == 0
    _, messages = select(None, "http://127.0.0.1:8080", runtime_dir)
    assert err.count(messages[0]) == 1
    assert err.count("refused") == 1


def test_non_loopback_warning_reaches_stderr_once(start, runtime_dir):
    url = "http://192.0.2.1:8080"
    code, _, err = start(["--dev-teacher", url])
    assert code == 0
    _, messages = select(url, None, runtime_dir)
    assert err.count(messages[0]) == 1
    assert err.count("warning") == 1


# (d) the timeout reaches the client -----------------------------------------------------


def test_config_timeout_bounds_a_slow_server(
    start, servers, default_socket, config_file
):
    unix_server, _ = servers
    drip = unix_server(default_socket, mode="drip")
    config_file.write_text("[model]\ntimeout_s = 0.5\n", encoding="utf-8")
    began = time.monotonic()
    code, out, _ = start([], REQUEST)
    elapsed = time.monotonic() - began
    assert code == 0
    assert "could not be used" in out
    assert elapsed < BOUND_S
    assert len(drip.requests) == 1


# (e) :status ----------------------------------------------------------------------------


def status_of(monkeypatch, setup, argv):
    root, release = setup
    seen = {}

    class FakeRepl:
        def run(self):
            return 0

    def interactive(cls, status_lines=(), **kwargs):
        seen["status_lines"] = [line() for line in status_lines]
        seen["backend"] = kwargs.get("backend")
        return FakeRepl()

    monkeypatch.setattr(cli.Repl, "interactive", classmethod(interactive))
    assert cli.main(argv, targets_root=root, os_release_paths=[release]) == 0
    return seen


def test_status_names_the_default_socket_and_backend(
    monkeypatch, setup, default_socket
):
    lines = status_of(monkeypatch, setup, [])["status_lines"]
    assert f"model endpoint: unix socket {default_socket}" in lines
    assert any(line.startswith("model backend: local model server") for line in lines)


def test_status_names_the_dev_teacher_and_backend(monkeypatch, setup, runtime_dir):
    url = "http://127.0.0.1:8080"
    seen = status_of(monkeypatch, setup, ["--dev-teacher", url])
    assert f"model endpoint: {url}" in seen["status_lines"]
    assert any(
        line.startswith("model backend: dev teacher") for line in seen["status_lines"]
    )


def test_status_without_a_runtime_dir(monkeypatch, setup):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    lines = status_of(monkeypatch, setup, [])["status_lines"]
    assert any(
        line.startswith("model endpoint: unix socket (none") for line in lines
    )


def test_main_passes_a_teacher_client_with_the_config_settings(
    monkeypatch, setup, default_socket, config_file
):
    from nl_shell.teacher_client import Endpoint, TeacherClient

    config_file.write_text(
        'teacher_model = "qwen"\n[model]\ntimeout_s = 2.5\n', encoding="utf-8"
    )
    backend = status_of(monkeypatch, setup, [])["backend"]
    assert isinstance(backend, TeacherClient)
    assert backend.endpoint == Endpoint("unix", default_socket)
    assert backend.timeout_s == 2.5
    assert backend.model == "qwen"


# (f) no TCP on the default path ---------------------------------------------------------


def test_default_path_opens_no_tcp_connection(
    monkeypatch, start, servers, default_socket, config_file
):
    unix_server, tcp_server = servers
    on_socket = unix_server(default_socket)
    at_url, port = tcp_server()
    config_file.write_text(
        f'teacher_url = "http://127.0.0.1:{port}"\n', encoding="utf-8"
    )
    connects = []
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def connect(self, address):
        connects.append((self.family, address))
        return real_connect(self, address)

    def create_connection(address, *args, **kwargs):
        connects.append(("create_connection", address))
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    code, out, _ = start([], REQUEST)
    monkeypatch.setattr(socket.socket, "connect", real_connect)
    monkeypatch.setattr(socket, "create_connection", real_create)

    assert code == 0
    tcp = [c for c in connects if c[0] in (socket.AF_INET, socket.AF_INET6)]
    assert tcp == []
    assert not [c for c in connects if c[0] == "create_connection"]
    local = [c for c in connects if c[0] == socket.AF_UNIX]
    assert len(local) >= 1
    assert all(address == default_socket for _, address in local)
    assert len(on_socket.requests) == 1
    assert REQUEST.encode() in on_socket.requests[0]
    assert at_url.connections == 0 and at_url.requests == []
    assert "Proposed command" in out
    assert "df -h" in out
