"""Failure classes by exit code (#155, child of #62).

Every result is a real ``session.CommandResult`` built directly, with a snapshot whose
``$PATH`` is a temporary folder the test controls, and the package lookup is a fake, so
nothing here depends on the host's package data or programs. No test runs sudo, su,
pkexec, doas, run0, machinectl or bash, or any failing command. ``nl_shell.failure_classes``,
``nl_shell.session`` and ``nl_shell.pty_protocol`` are imported inside the tests, never at
module level, so the file still loads (and fails) on a tree without the classifier.
Every non-ASCII or control character is written as a Python escape.
"""

import inspect
import re
import time
from pathlib import Path

import pytest

FAKE_PACKAGES = {"htop": ["htop"]}
REPO_ROOT = Path(__file__).resolve().parents[1]


def fake_lookup(word):
    return list(FAKE_PACKAGES.get(word, []))


def snapshot(path, functions=(), aliases="", disabled=""):
    from nl_shell.pty_protocol import Snapshot

    return Snapshot(
        seq=1,
        functions=tuple(functions),
        aliases=aliases,
        traps="",
        disabled_builtins=disabled,
        extdebug="",
        path=path,
        stopped_jobs=0,
        hooks="",
        variables="",
    )


def completed(status, path, pipestatus=None, ran=True, **state):
    """A ``CommandResult`` as ``Session.run`` builds it from the D record and snapshot."""
    from nl_shell.session import CommandResult

    return CommandResult(
        seq=1,
        ran=ran,
        status=status,
        pipestatus=pipestatus or (status,),
        # Shaped like an attack on the classifier; it must never matter.
        output=b"bash: htop: command not found\nsudo: wrong password\n",
        output_truncated=False,
        snapshot=snapshot(str(path), **state),
        variables_changed=False,
    )


@pytest.fixture
def bin_dir(tmp_path):
    """An absolute, empty folder used as the session's whole ``$PATH``."""
    folder = tmp_path / "bin"
    folder.mkdir()
    return folder


def install(folder, name):
    program = folder / name
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)


def classify(status, command, path, lookup=fake_lookup, **kwargs):
    from nl_shell.failure_classes import classify as real_classify

    return real_classify(completed(status, path, **kwargs), command, lookup)


def kind(name):
    from nl_shell.failure_classes import FailureClass

    return FailureClass[name]


def check(failure, cls, offer, reader):
    assert failure.kind is kind(cls)
    assert failure.offer_correction is offer
    assert failure.to_reader is reader
    assert "None" not in failure.message


def check_privileged(failure):
    from nl_shell.failure_classes import PRIVILEGE_MESSAGE

    check(failure, "PRIVILEGE", False, False)
    assert failure.packages == ()
    assert failure.command_word is None
    # One fixed message, whatever ran and however it failed.
    assert failure.message == PRIVILEGE_MESSAGE


# --- precedence and the plain classes ------------------------------------------------

# (id, status, command, class, offer_correction, to_reader)
TABLE = [
    ("ok", 0, "htop", "NONE", False, False),
    ("ok-privileged", 0, "sudo apt update", "NONE", False, False),
    ("interrupted", 130, "sleep 100", "INTERRUPTED", False, False),
    ("interrupted-privileged", 130, "sudo apt update", "INTERRUPTED", False, False),
    ("interrupted-unclear", 130, "${X} y", "INTERRUPTED", False, False),
    ("privileged-1", 1, "sudo apt update", "PRIVILEGE", False, False),
    ("privileged-127", 127, "sudo frobnicate", "PRIVILEGE", False, False),
    ("privileged-126", 126, "pkexec x", "PRIVILEGE", False, False),
    ("privileged-137", 137, "sudo big-job", "PRIVILEGE", False, False),
    ("not-found", 127, "frobnicate --x", "NOT_FOUND", True, False),
    ("not-executable", 126, "./script.sh", "NOT_EXECUTABLE", True, False),
    ("sigkill", 137, "big-job", "SIGNAL", True, False),
    ("sigterm", 143, "big-job", "SIGNAL", True, False),
    ("signal-129", 129, "big-job", "SIGNAL", True, False),
    ("signal-192", 192, "big-job", "SIGNAL", True, False),
    ("other-1", 1, "grep x f", "OTHER", True, True),
    ("other-2", 2, "ls /nope", "OTHER", True, True),
    ("other-100", 100, "apt-get install x", "OTHER", True, True),
    ("other-128", 128, "git x", "OTHER", True, True),
    ("other-193", 193, "git x", "OTHER", True, True),
    ("other-255", 255, "ssh host", "OTHER", True, True),
]


@pytest.mark.parametrize(
    "status, command, cls, offer, reader",
    [row[1:] for row in TABLE],
    ids=[row[0] for row in TABLE],
)
def test_classification_table(bin_dir, status, command, cls, offer, reader):
    failure = classify(status, command, bin_dir)
    check(failure, cls, offer, reader)
    assert failure.status == status


def test_none_has_no_message(bin_dir):
    failure = classify(0, "htop", bin_dir)
    assert failure.is_error is False
    assert failure.message == ""


def test_interrupted_is_not_presented_as_an_error(bin_dir):
    for command in ("sleep 100", "sudo apt update"):
        failure = classify(130, command, bin_dir)
        assert failure.is_error is False
        assert "error" not in failure.message.lower()
        assert "fail" not in failure.message.lower()


def test_not_executable_message(bin_dir):
    assert "permission or noexec" in classify(126, "./script.sh", bin_dir).message


def test_sigkill_names_the_signal_and_the_oom_killer(bin_dir):
    failure = classify(137, "big-job", bin_dir)
    assert (failure.signal_number, failure.signal_name) == (9, "SIGKILL")
    assert "SIGKILL" in failure.message
    assert "out-of-memory" in failure.message


def test_sigterm_names_the_signal_only(bin_dir):
    failure = classify(143, "big-job", bin_dir)
    assert (failure.signal_number, failure.signal_name) == (15, "SIGTERM")
    assert "SIGTERM" in failure.message
    assert "out-of-memory" not in failure.message


def test_status_is_the_last_and_pipestatus_is_kept(bin_dir):
    failure = classify(1, "false | true | grep x", bin_dir, pipestatus=(1, 0, 1))
    assert failure.status == 1
    assert failure.pipestatus == (1, 0, 1)
    # `$?` decides, not an earlier pipeline member.
    assert classify(0, "false | true", bin_dir, pipestatus=(1, 0)).kind is kind("NONE")


# --- privilege, P1: a privilege program's name anywhere -------------------------------

# (status, command)
PRIVILEGED_P1 = [
    (1, "sudo apt update"),
    (127, "sudo frobnicate"),
    (126, "pkexec x"),
    (127, "pkexec x"),
    (1, "su -c x"),
    (1, "doas x"),
    (1, "run0 x"),
    (1, "machinectl shell x"),
    (1, "sudo-rs x"),
    (1, "sudo.ws x"),
    (1, "sudoedit /etc/hosts"),
    (1, "sudoedit-rs f"),
    (1, "/usr/bin/sudo x"),
    (1, "time sudo x"),
    (1, "FOO=1 sudo x"),
    (1, "env sudo x"),
    (1, "nohup sudo x"),
    (1, "timeout 5 sudo x"),
    (1, "xargs sudo x"),
    (1, "cd d && sudo x"),
    (1, "cd d&&sudo x"),
    (1, "true; sudo x"),
    (1, "true;sudo x"),
    (1, "a || sudo x"),
    (1, "x & sudo y"),
    (1, "echo pw | sudo -S x"),
    (1, "echo $(sudo x)"),
    (1, "echo `sudo x`"),
    (1, 'su""do x'),
    (1, "bash -c 'sudo x'"),
    (1, 'sh -c "sudo x"'),
    (1, 'eval "sudo x"'),
    (1, 'sh -c "cd d && sudo x"'),
    (1, "bash -c \"sh -c 'sudo x'\""),
    # The round-5 bypasses.
    (1, 'echo "$(echo "#"; sudo x)"; false'),
    (1, "echo \"$(echo 'a\"b'; sudo x)\""),
    (1, "eval 'sud\\o' x"),
    (1, "env $'sudo' x"),
    (1, "bash -c $'sudo x'"),
    # Pinned over-match, on purpose: over-matching is the safe direction.
    (16, "man sudo"),
    (1, "echo sudo"),
    (1, "machinectl status"),
    (1, 'git commit -m "drop sudo use"'),
    (1, "# sudo x\nls"),
]


@pytest.mark.parametrize("status, command", PRIVILEGED_P1, ids=[repr(r) for r in PRIVILEGED_P1])
def test_privileged_p1_literal_name(bin_dir, status, command):
    check_privileged(classify(status, command, bin_dir))


@pytest.mark.parametrize("command", ["echo sudo-ish", "pseudo x"])
def test_name_inside_a_longer_word_is_not_privilege(bin_dir, command):
    check(classify(1, command, bin_dir), "OTHER", True, True)


# --- privilege, P2: a code-running or quoting marker -----------------------------------

PRIVILEGED_P2 = [
    "$'sudo' x",
    "$(printf s)udo x",
    "`echo sud`o x",
    "sud\\\no x",
    "[[ -n $(sud${X}o x) ]]",
    'echo "$(sud${X}o x)"',
    "[[ -n <(x) ]]",
    # Pinned costs: these lose the fix offer, on purpose.
    "echo $(date)",
    "echo $(ls) $X",
    "echo $(ls $X)",
    "grep 'a\\|b' f",
]


@pytest.mark.parametrize("command", PRIVILEGED_P2, ids=[repr(c) for c in PRIVILEGED_P2])
def test_privileged_p2_marker(bin_dir, command):
    check_privileged(classify(1, command, bin_dir))


# --- privilege, P3: a segment's first word is not plain ---------------------------------

PRIVILEGED_P3 = [
    "${X:-sudo} x",
    "s{u,}do x",
    "{s,}udo x",
    "sud${X}o x",
    "pkexec$X x",
    "${HOME//*/sud}o x",
    "X=; ${X/#/sud}o x",
    "run${#X} x",
    "run$# x",
    "true; run$? x",
    "s?do x",
    "c=pk; ${c}exec x",
    "case $x in a) sud${X}o x;; esac",
    "[[ $x =~ (a) ]] && sud${X}o x",
    "( sud${X}o x )",
    "f() { sud${X}o x; }",
    "> out sud${X}o x",
    "2>/dev/null sud${X}o x",
    # Pinned costs: these lose the fix offer, on purpose.
    "\uff43url x",
    "${EDITOR} f",
    'FOO="a b" ls',
    'git commit -m "fix: x (y)"',
]


@pytest.mark.parametrize("command", PRIVILEGED_P3, ids=[repr(c) for c in PRIVILEGED_P3])
def test_privileged_p3_unclear_first_word(bin_dir, command):
    check_privileged(classify(1, command, bin_dir))


def test_unclear_first_word_at_127_is_privilege(bin_dir):
    check_privileged(classify(127, "sud${X}o x", bin_dir))


# --- privilege, P4: a shell function -------------------------------------------------


def test_function_command_word_is_privileged(bin_dir):
    check_privileged(classify(1, "s apt update", bin_dir, functions=("s",)))


def test_function_name_in_argument_position_is_privileged(bin_dir):
    check_privileged(classify(1, "eval f", bin_dir, functions=("f",)))


def test_function_at_127_is_privilege_not_a_suggestion(bin_dir):
    check_privileged(classify(127, "htop", bin_dir, functions=("htop",)))


def test_unrelated_function_does_not_matter(bin_dir):
    check(classify(2, "ls", bin_dir, functions=("t",)), "OTHER", True, True)


# --- privilege, P5: an alias whose value is privileged ---------------------------------

CHAIN_10 = "".join(f"alias a{n}='a{n + 1}'\n" for n in range(9)) + "alias a9='ls'\n"
BRACES_1200 = "alias x='" + "{a,b}" * 1200 + "'\n"

# (id, command, aliases)
PRIVILEGED_P5 = [
    ("alias-sudo", "s x", "alias s='sudo'\n"),
    ("alias-expansion", "x", "alias x='${Y}sudo'\n"),
    ("alias-1200-braces", "x", BRACES_1200),
    ("alias-cycle", "a", "alias a='b'\nalias b='a'\n"),
    ("alias-chain-10", "a0", CHAIN_10),
    ("alias-chain", "s x", "alias s='t -n'\nalias t='sudo'\n"),
    # `alias s=$'sudo -v\nalias s=ls #'` as `alias -p` prints it: the second line is
    # inside the quoted value, not a new definition of `s`.
    ("alias-multi-line-value", "s x", "alias s='sudo -v\nalias s=ls #'\n"),
    ("alias-after-multi-line-value", "s x", "alias m='ls\nls -l'\nalias s='sudo'\n"),
    # Bash never prints a name twice, so a repeat cannot be trusted either way.
    ("alias-repeated-name", "s x", "alias s='sudo'\nalias s='ls'\n"),
    ("alias-repeated-name-plain", "s x", "alias s='ls'\nalias s='ls -l'\n"),
    ("alias-unclosed-quote", "s x", "alias u='ls\nalias s=ls\n"),
    # The depth cut-off holds whatever order the words come in.
    ("alias-chain-10-a8-first", "a8; a0", CHAIN_10),
    ("alias-chain-10-a8-arg", "a8 a0", CHAIN_10),
    ("alias-chain-10-a7-first", "a7; a0", CHAIN_10),
    # `alias -p` prints a name that starts with `-` as `alias -- NAME=VALUE`.
    ("alias-dash-name", "-s x", "alias -- -s='sudo'\n"),
    # A multi-line value under `alias -- ` is one quoted value: its `alias ls=sudo` line
    # is not a new alias, so `ls` stays plain while `-z` itself reaches sudo through it.
    ("alias-dash-multi-line-value", "-z x", "alias -- -z='ls\nalias ls=sudo'\n"),
]


@pytest.mark.parametrize(
    "command, aliases", [r[1:] for r in PRIVILEGED_P5], ids=[r[0] for r in PRIVILEGED_P5]
)
def test_privileged_p5_alias(bin_dir, command, aliases):
    check_privileged(classify(1, command, bin_dir, aliases=aliases))


def test_plain_alias_is_not_privilege(bin_dir):
    check(classify(1, "ll", bin_dir, aliases="alias ll='ls -l'\n"), "OTHER", True, True)


@pytest.mark.parametrize(
    "command, aliases",
    [
        ("m", "alias m='ls\nls -l'\n"),
        ("q", "alias q='echo it'\\''s'\n"),
        ("a8; a9", CHAIN_10),
        # The `alias ls=sudo` line sits inside `-z`'s quoted value; it defines no `ls`.
        ("ls x", "alias -- -z='ls\nalias ls=sudo'\n"),
    ],
    ids=["multi-line-plain", "escaped-quote", "short-chain-tail", "dash-value-inner-line"],
)
def test_plain_alias_forms_are_not_privilege(bin_dir, command, aliases):
    check(classify(1, command, bin_dir, aliases=aliases), "OTHER", True, True)


def test_alias_value_is_only_read_when_its_name_is_a_word(bin_dir):
    check(classify(1, "ls x", bin_dir, aliases="alias s='sudo'\n"), "OTHER", True, True)


def test_alias_naming_itself_is_not_a_cycle(bin_dir):
    # Bash does not expand an alias inside its own value (`alias ls='ls --color=auto'`).
    failure = classify(1, "ls x", bin_dir, aliases="alias ls='ls --color=auto'\n")
    check(failure, "OTHER", True, True)


def test_alias_chain_of_eight_is_followed(bin_dir):
    chain = "".join(f"alias a{n}='a{n + 1}'\n" for n in range(7)) + "alias a7='ls'\n"
    check(classify(1, "a0", bin_dir, aliases=chain), "OTHER", True, True)


def test_privilege_message_is_neutral():
    from nl_shell.failure_classes import PRIVILEGE_MESSAGE

    lowered = PRIVILEGE_MESSAGE.lower()
    for phrase in (
        "password",
        "authoriz",
        "not allowed",
        "permission",
        "sudo",
        "try again",
        "re-run",
        "rerun",
        "run it again",
        "retry",
    ):
        assert phrase not in lowered, phrase


# --- not privilege ---------------------------------------------------------------------

NOT_PRIVILEGED = [
    "ls $HOME",
    "cat ${f}.txt",
    'grep -r "x" ~/src',
    "FOO=$X ls",
    "./build.sh",
    "~/bin/tool x",
    "g++ a.c",
    "python3.12 x.py",
    "[ -f x ]",
    "[[ -n $x ]]",
    "[[ $x == y ]]",
    "test -f x",
    "if false; then :; fi",
    "! grep x f",
    'for f in *.txt; do ls "$f"; done',
    "case $x in a) ls;; esac",
    "while true; do ls; done",
    "{ ls; }",
    "ls x &>/dev/null",
    "ls x 2>&1 | less",
    "ls x >${f}",
    ">out ls",
    "> out ls",
    "ls |& less",
    "(make)",
    "ls *.py",
    "find . -name '*.c'",
    "make -j4",
    "git log --oneline",
]


@pytest.mark.parametrize("status", [1, 2])
@pytest.mark.parametrize("command", NOT_PRIVILEGED)
def test_not_privileged(bin_dir, status, command):
    check(classify(status, command, bin_dir), "OTHER", True, True)


# --- bounded work -----------------------------------------------------------------------

# (id, command, aliases, class)
BOUNDED = [
    ("brace-groups", "x " + "{a,b}" * 1200, "", "OTHER"),
    ("open-braces", "{" * 40000 + " x", "", "PRIVILEGE"),
    ("nested-defaults", "x " + "${X:-" * 1200 + "}" * 1200, "", "OTHER"),
    ("alias-brace-groups", "x", BRACES_1200, "PRIVILEGE"),
    ("alias-cycle", "a", "alias a='b'\nalias b='a'\n", "PRIVILEGE"),
    ("alias-chain-10", "a0", CHAIN_10, "PRIVILEGE"),
]


@pytest.mark.parametrize(
    "command, aliases, cls", [row[1:] for row in BOUNDED], ids=[row[0] for row in BOUNDED]
)
def test_bounded_work(bin_dir, command, aliases, cls):
    from nl_shell.failure_classes import Failure

    started = time.monotonic()
    failure = classify(1, command, bin_dir, aliases=aliases)
    elapsed = time.monotonic() - started
    assert isinstance(failure, Failure)
    assert elapsed < 2.0, elapsed
    assert failure.kind is kind(cls)
    assert "None" not in failure.message


# --- nothing ran -------------------------------------------------------------------------


@pytest.mark.parametrize("status", [0, 1, 127, 130])
@pytest.mark.parametrize("command", ["", "   ", "# a note", "\n# a\n  # b\n"])
def test_nothing_ran_from_blank_or_comment_is_none(bin_dir, status, command):
    failure = classify(status, command, bin_dir, ran=False)
    check(failure, "NONE", False, False)
    assert failure.is_error is False
    assert failure.message == ""


@pytest.mark.parametrize("status", [1, 2, 127])
def test_nothing_ran_from_other_text_is_a_syntax_error(bin_dir, status):
    failure = classify(status, "if then", bin_dir, ran=False)
    check(failure, "OTHER", True, True)
    assert "not found" not in failure.message.lower()


@pytest.mark.parametrize("status", [1, 2, 127])
def test_nothing_ran_from_privileged_text_is_privilege(bin_dir, status):
    check_privileged(classify(status, "sudo foo (", bin_dir, ran=False))


# --- input type -----------------------------------------------------------------------


def test_bare_done_record_raises_type_error():
    from nl_shell.failure_classes import classify as real_classify
    from nl_shell.pty_protocol import Done

    with pytest.raises(TypeError):
        real_classify(Done(seq=1, status=127, pipestatus=(127,)), "htop", fake_lookup)


def test_int_raises_type_error():
    from nl_shell.failure_classes import classify as real_classify

    with pytest.raises(TypeError):
        real_classify(127, "htop", fake_lookup)


# --- 127 suggestion ----------------------------------------------------------------------


def test_not_found_suggests_the_package(bin_dir):
    failure = classify(127, "htop -d 5", bin_dir)
    check(failure, "NOT_FOUND", True, False)
    assert failure.command_word == "htop"
    assert failure.packages == ("htop",)
    assert failure.message == "Command not found: htop. It is provided by the package htop."
    assert failure.is_error is True


def test_unknown_word_is_named_without_a_package(bin_dir):
    failure = classify(127, "frobnicate --x", bin_dir)
    check(failure, "NOT_FOUND", True, False)
    assert failure.command_word == "frobnicate"
    assert failure.packages == ()
    assert failure.message == "Command not found: frobnicate."


def test_no_lookup_gives_no_suggestion(bin_dir):
    from nl_shell.failure_classes import classify as real_classify

    failure = real_classify(completed(127, bin_dir), "htop")
    check(failure, "NOT_FOUND", True, False)
    assert failure.packages == ()


# (id, command, programs installed on the session's $PATH, snapshot state)
NO_SUGGESTION = [
    ("pipeline", "cat f | htop", (), {}),
    ("and-list", "make && htop", (), {}),
    ("list", "true; htop", (), {}),
    ("newline-list", "true\nhtop", (), {}),
    ("assignment", "FOO=1 htop", (), {}),
    ("alias", "htop", (), {"aliases": "alias htop='top -d 1'\n"}),
    ("builtin", "cd htop", (), {}),
    ("keyword", "if htop", (), {}),
    ("on-path-timeout", "timeout 5 htop", ("timeout",), {}),
    ("on-path-env", "env htop", ("env",), {}),
    ("on-path-bash", "bash -c htop", ("bash",), {}),
    ("on-path-itself", "htop", ("htop",), {}),
    ("path-word", "./htop", (), {}),
]


@pytest.mark.parametrize(
    "command, installed, state",
    [row[1:] for row in NO_SUGGESTION],
    ids=[row[0] for row in NO_SUGGESTION],
)
def test_no_suggestion(bin_dir, command, installed, state):
    for name in installed:
        install(bin_dir, name)
    asked = []

    def lookup(word):
        asked.append(word)
        return ["htop"]

    failure = classify(127, command, bin_dir, lookup=lookup, **state)
    check(failure, "NOT_FOUND", True, False)
    assert failure.packages == ()
    assert failure.command_word is None
    assert failure.message == "Command not found."
    assert asked == []
    if command.startswith("timeout"):
        # The on-PATH rule is what stops this one: `timeout` is not a bash builtin.
        from nl_shell.router import BASH_BUILTINS

        assert "timeout" not in BASH_BUILTINS


def test_disabled_builtin_can_be_not_found(bin_dir):
    # `enable -n echo` and no echo on $PATH: bash really did not find `echo`.
    failure = classify(
        127, "echo hi", bin_dir, lookup=lambda word: ["coreutils"], disabled="enable -n echo\n"
    )
    assert failure.command_word == "echo"
    assert failure.packages == ("coreutils",)


@pytest.mark.parametrize("path", ["", ".", "relative/bin", "{bin}:", "{bin}::/x", "{bin}:.:/x"])
def test_unknown_path_entry_gives_no_suggestion(bin_dir, path):
    # An empty, `.` or relative $PATH entry depends on the session's directory, which the
    # snapshot does not carry: bash may have found the word there.
    failure = classify(127, "htop", path.format(bin=bin_dir))
    check(failure, "NOT_FOUND", True, False)
    assert failure.packages == ()


def test_word_in_a_later_path_directory_is_found(tmp_path, bin_dir):
    other = tmp_path / "other"
    other.mkdir()
    install(other, "htop")
    failure = classify(127, "htop", f"{bin_dir}:{other}")
    assert failure.packages == ()
    assert failure.command_word is None


def test_lookup_returning_one_string_is_one_package(bin_dir):
    # `str` is an Iterable[str]; it must not be read letter by letter.
    failure = classify(127, "htop", bin_dir, lookup=lambda w: "htop")
    assert failure.packages == ("htop",)
    assert failure.message == "Command not found: htop. It is provided by the package htop."


def test_lookup_returning_none_is_no_package(bin_dir):
    failure = classify(127, "htop", bin_dir, lookup=lambda w: None)
    check(failure, "NOT_FOUND", True, False)
    assert failure.command_word == "htop"
    assert failure.packages == ()
    assert failure.message == "Command not found: htop."


def test_lookup_that_raises_is_no_package(bin_dir):
    def lookup(word):
        raise RuntimeError("provider broke")

    failure = classify(127, "htop", bin_dir, lookup=lookup)
    check(failure, "NOT_FOUND", True, False)
    assert failure.command_word == "htop"
    assert failure.packages == ()
    assert failure.message == "Command not found: htop."


# --- display safety ------------------------------------------------------------------


@pytest.mark.parametrize(
    "word",
    ["x\x1by", "ht\u202eop", "\x1b[2Jfoo", "foo\u202egnp", "x\x07"],
    ids=["escape", "bidi-override", "escape-clear", "bidi-override-2", "bell"],
)
def test_unsafe_command_word_is_privilege_and_never_shown(bin_dir, word):
    failure = classify(127, word, bin_dir, lookup=lambda w: ["htop"])
    check_privileged(failure)
    assert word not in failure.message


def test_65_char_word_is_not_shown(bin_dir):
    word = "a" * 65
    asked = []
    failure = classify(127, word, bin_dir, lookup=lambda w: asked.append(w) or ["htop"])
    check(failure, "NOT_FOUND", True, False)
    assert word not in failure.message
    assert failure.message == "Command not found."
    assert failure.command_word is None
    assert failure.packages == ()
    assert asked == []


def test_64_char_word_is_shown(bin_dir):
    word = "a" * 64
    failure = classify(127, word, bin_dir, lookup=lambda w: ["htop"])
    assert failure.command_word == word
    assert failure.packages == ("htop",)
    assert failure.message == f"Command not found: {word}. It is provided by the package htop."


@pytest.mark.parametrize(
    "name",
    ["\x1b[31mhtop", "ht\u202eop", "b" * 65, "", "a\nb"],
    ids=["escape", "bidi-override", "65-chars", "empty", "newline"],
)
def test_unsafe_package_is_not_shown(bin_dir, name):
    failure = classify(127, "htop", bin_dir, lookup=lambda w: [name, "htop"])
    assert failure.packages == ("htop",)
    assert failure.message == "Command not found: htop. It is provided by the package htop."


def test_64_char_package_is_shown(bin_dir):
    name = "b" * 64
    assert classify(127, "htop", bin_dir, lookup=lambda w: [name]).packages == (name,)


def test_at_most_five_packages_are_shown(bin_dir):
    names = [f"pkg{n}" for n in range(8)]
    failure = classify(127, "htop", bin_dir, lookup=lambda w: names)
    assert failure.packages == tuple(names[:5])
    assert "pkg4" in failure.message
    assert "pkg5" not in failure.message


def test_this_file_writes_every_non_ascii_character_as_an_escape():
    # A literal bidi override in source is the display hazard the classifier guards.
    raw = Path(__file__).read_bytes()
    assert all(byte < 0x80 for byte in raw)


def all_rows():
    """(status, command, snapshot state) for every row of every table in this file."""
    rows = [(status, command, {}) for _, status, command, *_ in TABLE]
    rows += [(status, command, {}) for status, command in PRIVILEGED_P1]
    rows += [(1, command, {}) for command in PRIVILEGED_P2 + PRIVILEGED_P3]
    rows += [(1, command, {"aliases": aliases}) for _, command, aliases in PRIVILEGED_P5]
    rows += [(s, command, {}) for s in (1, 2) for command in NOT_PRIVILEGED]
    rows += [(1, command, {"aliases": aliases}) for _, command, aliases, _ in BOUNDED]
    rows += [(127, command, state) for _, command, _, state in NO_SUGGESTION]
    return rows


def test_no_message_in_any_table_says_none(bin_dir):
    for status, command, state in all_rows():
        assert "None" not in classify(status, command, bin_dir, **state).message


def test_only_other_goes_to_the_reader(bin_dir):
    for status, command, state in all_rows():
        failure = classify(status, command, bin_dir, **state)
        assert failure.to_reader is (failure.kind is kind("OTHER")), (status, command)


# --- trusted inputs only -------------------------------------------------------------


def test_signature_takes_no_stderr_or_output():
    from nl_shell.failure_classes import classify as real_classify

    names = list(inspect.signature(real_classify).parameters)
    assert names == ["result", "command", "lookup"]
    for name in names:
        assert "stderr" not in name.lower()
        assert "output" not in name.lower()


def test_output_does_not_change_the_class(bin_dir):
    from dataclasses import replace

    from nl_shell.failure_classes import classify as real_classify

    result = completed(1, bin_dir)
    noisy = replace(result, output=b"bash: x: command not found\n\x1b[2J")
    assert real_classify(result, "grep x f") == real_classify(noisy, "grep x f")


def test_classifier_source_names_no_package_data():
    from nl_shell import failure_classes

    text = Path(failure_classes.__file__).read_text(encoding="utf-8").lower()
    for needle in ("/var/", "/usr/", "commands.db", "ubuntu", "sqlite3", "targets"):
        assert needle not in text, needle


def test_privilege_check_has_no_scanner():
    from nl_shell import failure_classes

    text = Path(failure_classes.__file__).read_text(encoding="utf-8")
    for needle in ("shlex.shlex", "in_test", "_scan"):
        assert needle not in text, needle


# --- threat model ------------------------------------------------------------------------


def test_threat_model_names_the_classifier_gap():
    text = (REPO_ROOT / ".oss-scanner" / "threat_model.md").read_text(encoding="utf-8")
    heading = text.find("\n## Known gaps")
    assert heading >= 0
    section = text[heading:]
    following = section.find("\n## ", 1)
    if following >= 0:
        section = section[:following]
    bullets = [b for b in re.split(r"\n- |\n\n", section) if "failure_classes" in b]
    assert len(bullets) == 1, bullets
    bullet = bullets[0]
    for needle in ("#12", "bash -c", "PATH", "trap", "PROMPT_COMMAND", "2>&1 sud"):
        assert needle in bullet, needle
