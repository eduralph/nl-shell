"""The target's ``[command_provider]`` and the package lookup over it (child of #62).

The loader rows write a fake target folder under ``tmp_path``. The reader rows build a
SQLite file under ``tmp_path`` with the same two tables as the shipped target's data,
``commands(command, pkgID)`` and ``packages(pkgID, name)``, so they never depend on the
host's package data; only the last test reads the host's file and is skipped without it.
The modules under test are imported inside the tests, so the file still loads (and fails)
on a tree without them.
"""

import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SHIPPED = "ubuntu-26.04"
HOST_DB = Path("/var/lib/command-not-found/commands.db")

BASE = """\
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
QUERY = (
    "SELECT packages.name FROM commands JOIN packages ON packages.pkgID = commands.pkgID "
    "WHERE commands.command = ? ORDER BY packages.name"
)
PROVIDER = f"""
[command_provider]
format = "sqlite"
path = "/data/commands.sqlite"
query = "{QUERY}"
"""


def load(tmp_path, text):
    from nl_shell import targets

    folder = tmp_path / "targets" / "testos-1"
    folder.mkdir(parents=True)
    (folder / "target.toml").write_text(text, encoding="utf-8")
    return targets.load("testos-1", tmp_path / "targets")


def make_db(path, rows):
    """A database at ``path``; ``rows`` are (command, pkgID, name) triples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE commands (command TEXT, pkgID INTEGER)")
        db.execute("CREATE TABLE packages (pkgID INTEGER, name)")
        for command, pkg_id, name in rows:
            db.execute("INSERT INTO commands VALUES (?, ?)", (command, pkg_id))
            db.execute("INSERT INTO packages VALUES (?, ?)", (pkg_id, name))
    db.close()
    return path


def lookup_for(path):
    from nl_shell.command_provider import provider_lookup
    from nl_shell.targets import CommandProvider

    return provider_lookup(CommandProvider(format="sqlite", path=str(path), query=QUERY))


# Loader -------------------------------------------------------------------------------


def test_valid_provider_loads(tmp_path):
    from nl_shell.targets import CommandProvider

    target = load(tmp_path, BASE + PROVIDER)
    assert target.command_provider == CommandProvider(
        format="sqlite", path="/data/commands.sqlite", query=QUERY
    )


def test_target_without_provider_has_none(tmp_path):
    target = load(tmp_path, BASE)
    assert target.command_provider is None


@pytest.mark.parametrize(
    "table",
    [
        pytest.param(PROVIDER.replace('"sqlite"', '"csv"'), id="unknown-format"),
        pytest.param(PROVIDER.replace("/data/commands.sqlite", "data/c.sqlite"), id="relative"),
        pytest.param(PROVIDER.replace("= ?", "= 'x'"), id="no-placeholder"),
        pytest.param(PROVIDER.replace("= ?", "= ? OR commands.command = ?"), id="two-?"),
        pytest.param(PROVIDER.replace('format = "sqlite"\n', ""), id="missing-format"),
        pytest.param(
            PROVIDER.replace('path = "/data/commands.sqlite"\n', ""), id="missing-path"
        ),
        pytest.param(PROVIDER.replace(f'query = "{QUERY}"\n', ""), id="missing-query"),
        pytest.param(PROVIDER.replace('"sqlite"', "1"), id="non-string-format"),
        pytest.param(PROVIDER.replace('"/data/commands.sqlite"', "[]"), id="non-string-path"),
        pytest.param(PROVIDER.replace('"sqlite"', '""'), id="empty-format"),
        pytest.param(PROVIDER.replace('"/data/commands.sqlite"', '""'), id="empty-path"),
        pytest.param(PROVIDER.replace(f'"{QUERY}"', '""'), id="empty-query"),
    ],
)
def test_invalid_provider_is_refused(tmp_path, table):
    from nl_shell.targets import TargetError

    with pytest.raises(TargetError, match="command_provider"):
        load(tmp_path, BASE + table)


def test_provider_that_is_not_a_table_is_refused(tmp_path):
    from nl_shell.targets import TargetError

    # A plain key has to come before the first table header.
    with pytest.raises(TargetError, match="`command_provider` must be a table"):
        load(tmp_path, 'command_provider = "sqlite"\n' + BASE)


def test_shipped_target_has_sqlite_provider():
    from nl_shell import targets

    provider = targets.load(SHIPPED, REPO / "targets").command_provider
    assert provider is not None
    assert provider.format == "sqlite"
    assert provider.path == "/var/lib/command-not-found/commands.db"


# Reader -------------------------------------------------------------------------------


def test_finds_the_package(tmp_path):
    db = make_db(tmp_path / "c.db", [("htop", 1, "htop")])
    assert lookup_for(db)("htop") == ("htop",)


def test_two_packages_come_back_sorted(tmp_path):
    from nl_shell.command_provider import provider_lookup
    from nl_shell.targets import CommandProvider

    db = make_db(tmp_path / "c.db", [("vi", 1, "nvi"), ("vi", 2, "vim")])
    # The result is sorted whatever order the query returns rows in.
    reversed_query = QUERY.replace("ORDER BY packages.name", "ORDER BY packages.name DESC")
    for query in (QUERY, reversed_query):
        provider = CommandProvider(format="sqlite", path=str(db), query=query)
        assert provider_lookup(provider)("vi") == ("nvi", "vim")


def test_duplicate_names_collapse(tmp_path):
    db = make_db(tmp_path / "c.db", [("htop", 1, "htop"), ("htop", 2, "htop")])
    assert lookup_for(db)("htop") == ("htop",)


def test_path_with_uri_characters(tmp_path):
    db = make_db(tmp_path / "a?b#c%41d" / "c.db", [("htop", 1, "htop")])
    assert lookup_for(db)("htop") == ("htop",)


def test_unknown_word_gives_nothing(tmp_path):
    db = make_db(tmp_path / "c.db", [("htop", 1, "htop")])
    assert lookup_for(db)("no-such-command") == ()


def test_missing_file_gives_nothing_and_is_not_created(tmp_path):
    db = tmp_path / "absent.db"
    assert lookup_for(db)("htop") == ()
    assert not db.exists()


def test_not_sqlite_gives_nothing(tmp_path):
    db = tmp_path / "c.db"
    db.write_bytes(b"this is not a database\n" * 100)
    assert lookup_for(db)("htop") == ()


def test_schema_without_tables_gives_nothing(tmp_path):
    db = tmp_path / "c.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE other (x)")
    conn.close()
    assert lookup_for(db)("htop") == ()


def test_lookups_leave_the_file_unchanged(tmp_path):
    db = make_db(tmp_path / "c.db", [("htop", 1, "htop")])
    before = db.read_bytes()
    lookup = lookup_for(db)
    for word in ("htop", "x", "'; DROP TABLE commands; --"):
        lookup(word)
    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.db"]


def test_read_only_mode_is_used(tmp_path, monkeypatch):
    from nl_shell import command_provider

    db = make_db(tmp_path / "c.db", [("htop", 1, "htop")])
    seen = []
    real = sqlite3.connect

    def connect(database, *args, **kwargs):
        seen.append((database, kwargs.get("uri")))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(command_provider.sqlite3, "connect", connect)
    assert lookup_for(db)("htop") == ("htop",)
    assert seen == [(db.as_uri() + "?mode=ro&immutable=1", True)]


def test_wal_mode_file_gets_no_sidecar_files(tmp_path):
    db = make_db(tmp_path / "c.db", [("htop", 1, "htop")])
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.close()  # the last close removes -wal and -shm
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.db"]
    assert lookup_for(db)("htop") == ("htop",)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.db"]


def test_wal_mode_file_in_a_read_only_directory_is_read(tmp_path):
    folder = tmp_path / "data"
    db = make_db(folder / "c.db", [("htop", 1, "htop")])
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    folder.chmod(0o555)
    try:
        assert lookup_for(db)("htop") == ("htop",)
    finally:
        folder.chmod(0o755)


@pytest.mark.parametrize("word", ["htop'", "htop; DROP TABLE commands", "ht%", "'"])
def test_word_is_a_bound_parameter(tmp_path, word):
    db = make_db(tmp_path / "c.db", [("htop", 1, "htop")])
    assert lookup_for(db)(word) == ()
    # Still intact afterwards.
    assert lookup_for(db)("htop") == ("htop",)


def test_no_provider_finds_nothing():
    from nl_shell.command_provider import provider_lookup

    lookup = provider_lookup(None)
    assert lookup("htop") == ()
    assert lookup("ls") == ()


# Package-name rule ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("HTOP", id="upper"),
        pytest.param("htop; rm -rf ~", id="shell"),
        pytest.param("htop\nevil", id="newline-inside"),
        pytest.param("htop\n", id="trailing-newline"),
        pytest.param("-htop", id="leading-dash"),
        pytest.param(42, id="int"),
        pytest.param(b"htop", id="bytes"),
        pytest.param(None, id="null"),
    ],
)
def test_bad_names_are_dropped(tmp_path, name):
    db = make_db(tmp_path / "c.db", [("htop", 1, name)])
    assert lookup_for(db)("htop") == ()


@pytest.mark.parametrize("name", ["libc6", "g++", "python3.12"])
def test_good_names_are_kept(tmp_path, name):
    db = make_db(tmp_path / "c.db", [("x", 1, name)])
    assert lookup_for(db)("x") == (name,)


def test_bad_name_does_not_hide_good_one(tmp_path):
    db = make_db(tmp_path / "c.db", [("htop", 1, "htop\n"), ("htop", 2, "htop")])
    assert lookup_for(db)("htop") == ("htop",)


# The module names no OS and no path ------------------------------------------------------


@pytest.mark.parametrize("text", ["ubuntu", "/var/", "commands.db"])
def test_module_names_no_os_or_path(text):
    import nl_shell.command_provider as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert text not in source.lower()


# Supplementary: the host's real data ----------------------------------------------------


@pytest.mark.skipif(not HOST_DB.is_file(), reason=f"{HOST_DB} not present on this host")
def test_real_lookup_finds_htop():
    from nl_shell import targets
    from nl_shell.command_provider import provider_lookup

    provider = targets.load(SHIPPED, REPO / "targets").command_provider
    assert "htop" in provider_lookup(provider)("htop")
