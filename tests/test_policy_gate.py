"""The policy gate's shell layer (issue #196, spec ``docs/design/specs/policy-gate/v1.md``).

One general test per rule of the shell layer (Rules 1-6), one regression table, and the
other spec rules this layer owns. ``nl_shell.gate`` is imported inside each test, never
at module top, so a missing package is a failing test, not a collection error.

Rule 6 runs on every result: ``classify`` below checks every reason it returns.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS = REPO_ROOT / "targets" / "ubuntu-26.04" / "gate-vectors.toml"
CLASSES = ("read-only", "mutating", "session-altering", "destructive", "privileged")
RANK = {
    "read-only": 0,
    "mutating": 1,
    "session-altering": 1,
    "destructive": 2,
    "privileged": 3,
}
COULD_NOT = "could not class"
ESC = "\x1b"


def gate():
    import nl_shell.gate

    return nl_shell.gate


def shell():
    from nl_shell.gate import parse

    return parse


def assert_clean(reasons) -> None:
    """Rule 6: no reason holds a C0/C1 control or a hidden character."""
    from nl_shell import render

    for reason in reasons:
        for char in reason:
            code = ord(char)
            assert not (code < 0x20 or 0x7F <= code <= 0x9F), (reason, hex(code))
            assert not render.is_hidden(char), (reason, hex(code))


def classify(command, origin="typed", label=None):
    result = gate().classify(command, origin=origin, label=label)
    assert_clean(result.reasons)
    return result


def has_reason(result, needle: str) -> bool:
    return any(needle in reason for reason in result.reasons)


@dataclass(frozen=True)
class Case:
    command: str
    cls: str | None  # None: a rejected case that asserts the rejection only
    rejected: bool
    reason: str
    origin: str = "typed"
    label: str | None = None
    decision_c: bool = False


def check(case: Case):
    result = classify(case.command, case.origin, case.label)
    assert result.rejected is case.rejected, (case, result)
    if case.cls is not None:
        assert result.cls == case.cls, (case, result)
    assert has_reason(result, case.reason), (case, result)
    if not case.decision_c:
        assert not has_reason(result, COULD_NOT), (case, result)
    return result


# --- inputs --------------------------------------------------------------------------

# Rule 1: one input per feature tree-sitter-bash reads differently from bash.
UNTRUSTED = {
    "U1": "if true; then",
    "U2": "case x in",
    "U3": "echo a\\\nb",
    "U4": "cat <<\\E\nx\nE",
    "U5": 'cat <<E"F"\nx\nEF',
    "U6": "cat <<$'E'\nx\nE",
    "U7": "cat <<-E\n\tx\nE",
    "U8": "cat <<A <<B\na\nA\nb\nB",
    "U9": "ls ?(a)",
    "U10": "ls *(a)",
    "U11": "ls +(a)",
    "U12": "ls @(a)",
    "U13": "ls !(a)",
    "U14": "echo ${ echo x; }",
    "U15": "echo ${| REPLY=x; }",
}

U = "unparseable"
NA = "not allowlisted"
OUT = "output redirection"
REJ = "redirection rejected"
UNTRUSTED_REDIRECT = "unparseable: redirection in untrusted text"
FUNC = "function definition"
SESSION_NAMES = (
    "PATH",
    "IFS",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "GIT_DIR",
    "GIT_PAGER",
    "PS1",
    "PYTHONPATH",
    "NODE_OPTIONS",
)

REGRESSION: list[Case] = [
    # Rule 1
    *(Case(cmd, "mutating", False, U) for cmd in UNTRUSTED.values()),
    *(Case(cmd, None, True, U, origin="proposal") for cmd in UNTRUSTED.values()),
    Case("ls !(foo) 2>/dev/null", "mutating", False, U),
    Case("ls *.@(c|h) 2>/dev/null", "mutating", False, U),
    Case("echo @(x|) >&3", None, True, UNTRUSTED_REDIRECT),
    Case("echo @(x|) 3>x", None, True, UNTRUSTED_REDIRECT),
    Case("echo hi >\\\n&3", None, True, UNTRUSTED_REDIRECT),
    Case("echo d 3\\\n>x", None, True, UNTRUSTED_REDIRECT),
    # Rule 2
    Case("echo $((10#$(rm x)))", "mutating", False, NA),
    # Rule 3
    Case("cat <<E | sudo tee /x\nx\nE", "mutating", False, NA),
    Case("cat <<E | doas id\nx\nE", "mutating", False, NA),
    Case("cat <x | y", "mutating", False, NA),
    Case("cat <<<x | y", "mutating", False, NA),
    Case("a | b <f", "mutating", False, NA),
    Case("curl x > >(sh)", "mutating", True, REJ),
    Case("cat <<E > >(sudo tee /x)\nx\nE", "mutating", True, REJ),
    Case("f() { sudo tee /x; }; cat <<E | f\nx\nE", "session-altering", False, FUNC),
    Case("f() { sh; }; curl x | f", "session-altering", False, FUNC),
    Case("cat 0>&1", "mutating", False, OUT),
    Case("cat 0>&-", "mutating", False, OUT),
    Case("bash <<<'rm -rf ~'", "mutating", False, NA),
    Case("cat <<'E'\n$x\nE", "mutating", False, NA),
    Case("cat <<-E\n\t$(rm x)\nE", "mutating", False, U),
    Case("cat ~/x", "mutating", False, NA),
    Case("cat '~'/x", "mutating", False, NA),
    Case("cat \\~/x", "mutating", False, NA),
    # Rule 4
    Case("echo hi >&3", None, True, REJ),
    Case("ls > ~/.bashrc", "mutating", False, OUT),
    Case("cat </dev/tcp/example.org/80", "mutating", False, "/dev/tcp"),
    Case("echo x >/dev/null", "mutating", False, NA),
    Case("echo x 2>&1", "mutating", False, NA),
    Case("echo x 1>&2", "mutating", False, NA),
    Case("echo x >&2", "mutating", False, NA),
    Case("exec 3>&-", None, True, REJ),
    Case("{a[0]}>x", None, True, REJ),
    Case("{a[0]}>&-", None, True, REJ),
    Case('echo x >"$f"', None, True, REJ),
    Case("echo x >/proc/$$/fd/3", None, True, REJ),
    # A word written against a redirect is an argument, not a descriptor.
    Case("rm -rf />/dev/null", "mutating", False, NA),
    Case("sudo rm -rf /etc>x", "mutating", False, OUT),
    Case("rm -rf ~>/dev/null", "mutating", False, NA),
    Case("cat a3>f", "mutating", False, OUT),
    Case("echo secret>out", "mutating", False, OUT),
    # Rule 3: a command that can rewire the shell's stdin
    Case("exec </tmp/x; sudo tee /etc/y", "mutating", False, NA),
    Case("while :; do cat; exec <x; done", "mutating", False, NA),
    # Rule 6
    Case("function f$'\x1b' { :; }", "session-altering", False, FUNC),
    # §1.1
    Case("ls \u202etxt.exe", None, True, "hidden character", origin="proposal"),
    Case("ls \u202etxt.exe", "mutating", False, "hidden character"),
    Case("ls \udcff", None, True, "hidden character", origin="proposal"),
    # §1.7
    Case("true", "destructive", False, "model labelled", label="destructive"),
    Case("true", "mutating", False, NA, label="read-only"),
    # §2
    Case("PAGER=sh git log", "mutating", False, "prefix assignment"),
    Case("LD_PRELOAD=./x.so ls", "mutating", False, "prefix assignment"),
    Case("LESSOPEN='|sh %s' less f", "mutating", False, "prefix assignment"),
    Case("$'\\x72m' -rf ~", "mutating", False, "command word is not a literal"),
    Case("$(echo rm) -rf ~", "mutating", False, "command word is not a literal"),
    Case("diff <(ls) <(ls)", "mutating", False, "process substitution"),
    Case("/tmp/rm x", "mutating", False, "path outside the system directories"),
    Case("./rm x", "mutating", False, "path outside the system directories"),
    Case("/usr/bin/rm x", "mutating", False, NA),
    Case("//usr/bin/rm x", "mutating", False, NA),
    # §3
    Case("f() { :; }", "session-altering", False, FUNC),
    *(
        Case(f"{name}=x", "session-altering", False, f"assignment to {name}")
        for name in SESSION_NAMES
    ),
    Case("export PATH=./bin", "session-altering", False, "assignment to PATH"),
    # A quoted declaration operand is still an assignment after quote removal.
    Case("export 'PATH=/tmp/evil'", "session-altering", False, "assignment to PATH"),
    Case('export "PATH"=/tmp/evil', "session-altering", False, "assignment to PATH"),
    # With only a redirect and no command word, the assignment stays in the shell.
    Case("PATH=/tmp/evil >/dev/null", "session-altering", False, "assignment to PATH"),
    Case("LD_PRELOAD=/tmp/x.so 2>/dev/null", "session-altering", False, "LD_PRELOAD"),
    Case("IFS=x 2>&1", "session-altering", False, "assignment to IFS"),
    Case("PATH=x >/dev/null; cat", "session-altering", False, "assignment to PATH"),
    # §5
    Case("SUDO_PROMPT=x sudo true", None, True, "SUDO_"),
    Case("SUDO_ASKPASS=/x sudo true", None, True, "SUDO_"),
    Case("export SUDO_PROMPT=x", None, True, "SUDO_"),
    Case("export 'SUDO_PROMPT=x'", None, True, "SUDO_"),
    Case("export 'SUDO_ASKPASS=x'", None, True, "SUDO_"),
    Case('declare -x "SUDO_ASKPASS=/x"', None, True, "SUDO_"),
    Case("export S'UDO_PROMPT'=x", None, True, "SUDO_"),
    Case("export SUDO_'ASKPASS'=/x", None, True, "SUDO_"),
    Case('export "$x"', None, True, "operand the gate cannot name"),
    # walk coverage
    Case("ls | wc; (pwd) && { id; }", "mutating", False, NA),
    Case("if a; then b; elif c; then d; else e; fi", "mutating", False, NA),
    Case("while a; do b; done; for i in 1; do c; done", "mutating", False, NA),
    Case("case x in a) b;; esac", "mutating", False, NA),
    Case("echo $(id) `pwd`", "mutating", False, NA),
    Case("declare -x A=1; unset A", "mutating", False, NA),
]


def vector_cases() -> list[Case]:
    from nl_shell.gate import vectors

    return [
        Case(v.command, v.cls, v.rejected, v.reason or "", origin=v.origin, label=v.label)
        for v in vectors.load(VECTORS)
    ]


# Inputs that, together, hold every named node kind of the pinned grammar.
KIND_COVERAGE = [
    "for ((i=0; i<3; i++)); do echo $i; done",
    "echo $(( a ? b : c )) $(( (1+2) )) $(( -x )) $(( x++ ))",
    "[[ $x =~ ^a ]] && [[ -n x ]] && [ -f x ]",
    'echo $"x" ${a[0]} ${x:-y} $? {a,b}',
    "case x in @(a|b)) :;; esac",
    "a=(1 2); b=1 c=2; ! true",
    "echo x 2>&1 # comment",
    "cat <<E\n$x y\nE",
    "cat <<<x",
    "echo 'a' \"b $c\"",
    "unset a; export b=1",
    "echo {1..3}; a=$\"x\"",
]


# --- Rule 1 --------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(UNTRUSTED))
def test_rule1_untrusted_tree(name):
    command = UNTRUSTED[name]
    proposal = classify(command, "proposal")
    assert proposal.rejected, proposal
    assert has_reason(proposal, U), proposal
    typed = classify(command)
    assert not typed.rejected, typed
    assert RANK[typed.cls] >= RANK["mutating"], typed
    assert has_reason(typed, U), typed
    assert not has_reason(typed, COULD_NOT), typed


def test_rule1_names_each_feature():
    parse = shell()
    expected = {
        "U1": "missing token",
        "U2": "syntax error",
        "U3": "backslash-newline",
        "U4": "here-doc delimiter",
        "U5": "here-doc delimiter",
        "U6": "here-doc delimiter",
        "U7": "<<-",
        "U8": "more than one here-doc",
        "U9": "extglob",
        "U10": "extglob",
        "U11": "extglob",
        "U12": "extglob",
        "U13": "extglob",
        "U14": "${ }",
        "U15": "${ }",
    }
    for name, feature in expected.items():
        data = UNTRUSTED[name].encode()
        found = parse.untrusted_features(parse.parse_source(data), data)
        assert any(feature in f for f in found), (name, found)
        assert has_reason(classify(UNTRUSTED[name]), feature), name
    # Quoted, the same characters are trusted.
    for command in ("echo '?(a)' \"*(b)\" \\@\\(c\\)", "echo 'a\\\nb'", "cat <<'E'\nx\nE"):
        data = command.encode()
        assert parse.untrusted_features(parse.parse_source(data), data) == (), command


def test_rule1_untrusted_redirects():
    for command in ("ls !(foo) 2>/dev/null", "ls *.@(c|h) 2>/dev/null"):
        result = check(Case(command, "mutating", False, U))
        assert not has_reason(result, UNTRUSTED_REDIRECT), result
    for command in (
        "echo @(x|) >&3",
        "echo @(x|) 3>x",
        "echo hi >\\\n&3",
        "echo d 3\\\n>x",
    ):
        check(Case(command, None, True, UNTRUSTED_REDIRECT))
    # A here-doc the parser misread is exempt only with no descriptor or a literal 0-2.
    for command in ("cat <<A {x}<<B\na\nA\nb\nB", "cat 3<<E\"F\"\nx\nEF"):
        check(Case(command, None, True, UNTRUSTED_REDIRECT))
    assert not classify("cat <<A 2<<B\na\nA\nb\nB").rejected
    # A redirect Rule 4 alone accepts is rejected in untrusted text.
    assert classify("ls @(a) >out").rejected
    assert classify("ls >out").rejected is False
    # Untrusted text proves no stdin fact.
    parse = shell()
    layer = parse.shell_layer("cat <x; ls @(a)")
    assert {i.stdin.kind for i in layer.invocations} == {"unknown"}


# --- Rule 2 --------------------------------------------------------------------------


def _tree_counts(command: str) -> tuple[int, set[str]]:
    parse = shell()
    tree = parse.parse_source(command.encode("utf-8", "surrogatepass"))
    count, kinds, stack = 0, set(), [tree.root_node]
    while stack:
        node = stack.pop()
        if node.is_named:
            kinds.add("ERROR" if node.is_error else node.type)
            if node.type in ("command", "declaration_command", "unset_command"):
                count += 1
        stack.extend(node.children)
    return count, kinds


def _grammar_kinds() -> set[str]:
    import tree_sitter
    import tree_sitter_bash

    language = tree_sitter.Language(tree_sitter_bash.language())
    return {
        language.node_kind_for_id(i)
        for i in range(language.node_kind_count)
        if language.node_kind_is_named(i) and language.node_kind_is_visible(i)
    }


def all_inputs() -> list[str]:
    seen, out = set(), []
    cases = REGRESSION + vector_cases()
    for command in [c.command for c in cases if not c.decision_c] + KIND_COVERAGE + [
        t[0] for t in rule4_table()
    ]:
        if command not in seen:
            seen.add(command)
            out.append(command)
    return out


def test_rule2_one_invocation_per_simple_command():
    parse = shell()
    for command in all_inputs():
        expected, _ = _tree_counts(command)
        layer = parse.shell_layer(command)
        assert len(layer.invocations) == expected, command
    layer = parse.shell_layer("echo $((10#$(rm x)))")
    assert [i.words[0].text for i in layer.invocations] == ["rm", "echo"]


def test_rule2_every_kind_has_a_handler():
    parse = shell()
    kinds = _grammar_kinds()
    assert len(kinds) > 50
    missing = sorted(k for k in kinds | {"ERROR"} if k not in parse.HANDLERS)
    assert missing == []


def test_rule2_removed_handler_is_decision_c(monkeypatch):
    parse = shell()
    seen: dict[str, str] = {}
    for command in all_inputs():
        for kind in _tree_counts(command)[1]:
            seen.setdefault(kind, command)
    uncovered = sorted(_grammar_kinds() - set(seen))
    assert uncovered == [], uncovered
    for kind, command in sorted(seen.items()):
        with monkeypatch.context() as m:
            m.delitem(parse.HANDLERS, kind)
            result = classify(command)
        assert result.cls == "privileged" and result.rejected, (kind, result)
        assert has_reason(result, COULD_NOT), (kind, result)


@pytest.mark.parametrize(
    "kind,command",
    [
        ("pipeline", "ls | wc"),
        ("command_substitution", "echo $(id)"),
        ("redirected_statement", "ls >x"),
        ("compound_statement", "{ ls; }"),
        ("heredoc_redirect", "cat <<E | sh\nx\nE"),
        ("program", "ls"),
    ],
)
def test_rule2_skipped_children_are_decision_c(monkeypatch, kind, command):
    parse = shell()
    assert not has_reason(classify(command), COULD_NOT)
    monkeypatch.setitem(parse.HANDLERS, kind, lambda walker, node, ctx, depth, bound=(): [])
    result = classify(command)
    assert (result.cls, result.rejected) == ("privileged", True), result
    assert has_reason(result, COULD_NOT), result


# --- Rule 3 --------------------------------------------------------------------------


def stdin_of(command: str) -> dict[str, tuple[str, str | None]]:
    layer = shell().shell_layer(command)
    out = {}
    for invocation in layer.invocations:
        out[invocation.words[0].text] = (invocation.stdin.kind, invocation.stdin.value)
    return out


def test_rule3_stdin_sources():
    assert stdin_of("cat <<E | sudo tee /x\nx\nE")["sudo"][0] == "pipe"
    assert stdin_of("cat <<E | doas id\nx\nE")["doas"][0] == "pipe"
    assert stdin_of("cat <x | y")["y"][0] == "pipe"
    assert stdin_of("cat <<<x | y")["y"][0] == "pipe"
    assert stdin_of("a | b <f") == {"a": ("terminal", None), "b": ("file", "f")}
    assert stdin_of("curl x > >(sh)")["sh"][0] == "pipe"
    assert stdin_of("cat <<E > >(sudo tee /x)\nx\nE")["sudo"][0] == "pipe"
    assert stdin_of("f() { sudo tee /x; }; cat <<E | f\nx\nE")["sudo"][0] == "unknown"
    assert stdin_of("f() { sh; }; curl x | f")["sh"][0] == "unknown"
    assert stdin_of("cat 0>&1")["cat"][0] == "unknown"
    assert stdin_of("cat 0>&-")["cat"][0] == "unknown"
    assert stdin_of("bash <<<'rm -rf ~'")["bash"] == ("literal", "rm -rf ~")
    assert stdin_of("cat <<'E'\n$x\nE")["cat"] == ("literal", "$x\n")
    assert stdin_of("cat <<E\nplain\nE")["cat"] == ("literal", "plain\n")
    assert stdin_of("cat <<E\n$x\nE")["cat"][0] == "unknown"
    assert stdin_of("cat <<-E\n\t$(rm x)\nE")["cat"][0] != "literal"
    assert stdin_of("cat")["cat"] == ("terminal", None)
    assert stdin_of("cat <$f")["cat"][0] == "unknown"
    assert stdin_of("cat <&3")["cat"][0] == "unknown"
    assert stdin_of("cat <>f")["cat"][0] == "unknown"
    assert stdin_of("{ cat; } <f")["cat"] == ("file", "f")
    # A command that can change the shell's own stdin leaves every other command's
    # stdin unproven, whether it runs after it in the text or on a later loop pass.
    assert stdin_of("{ sudo tee /etc/y; } </tmp/x")["sudo"] == ("file", "/tmp/x")
    assert stdin_of("exec </tmp/x; sudo tee /etc/y")["sudo"][0] == "unknown"
    assert stdin_of("while :; do cat; exec <x; done")["cat"][0] == "unknown"
    assert stdin_of("command exec <x; cat")["cat"][0] == "unknown"
    assert stdin_of("eval x; cat")["cat"][0] == "unknown"
    assert stdin_of("$x <f; cat")["cat"][0] == "unknown"
    # A trap handler runs code the gate does not see before later commands.
    assert stdin_of("trap 'exec <<<payload' DEBUG; cat")["cat"][0] == "unknown"
    # `exec` with no redirect does not, and neither does a bare redirect.
    assert stdin_of("exec; cat")["cat"] == ("terminal", None)
    assert stdin_of("PATH=x >/dev/null")[""][0] == "unknown"  # untrusted (MISSING)


def test_words_next_to_redirects_are_kept():
    # The invocation carries every word bash passes, also one written right against a
    # redirect operator (bash: `printf "<%s>" a /etc>out` writes `<a></etc>`).
    parse = shell()

    def words(command):
        (invocation,) = parse.shell_layer(command).invocations
        return [w.text for w in invocation.words], [
            (r.descriptor, r.operator) for r in invocation.redirects
        ]

    assert words("rm -rf />/dev/null") == (["rm", "-rf", "/"], [(None, ">")])
    assert words("sudo rm -rf /etc>x") == (["sudo", "rm", "-rf", "/etc"], [(None, ">")])
    assert words("rm -rf ~>/dev/null") == (["rm", "-rf", "~"], [(None, ">")])
    assert words("cat a3>f") == (["cat", "a3"], [(None, ">")])
    assert words('echo "3">f') == (["echo", "3"], [(None, ">")])
    assert words("echo x}>f") == (["echo", "x}"], [(None, ">")])
    # A real descriptor the tree left as a word is still taken as the descriptor.
    assert words("cat 0>&-") == (["cat"], [("0", ">&-")])
    assert words("echo {x}>f") == (["echo"], [("{x}", ">")])
    # No command word: the assignment is standalone, not a one-command prefix.
    (invocation,) = parse.shell_layer("PATH=/tmp/evil >/dev/null").invocations
    assert [(a.name, a.role) for a in invocation.assignments] == [("PATH", "standalone")]
    assert [w.text for w in invocation.words] == [""]


def test_rule3_literal_flag():
    parse = shell()

    def second_word(command):
        return parse.shell_layer(command).invocations[-1].words[1]

    assert not second_word("cat ~/x").literal
    assert second_word("cat '~'/x").literal
    assert second_word("cat \\~/x").literal
    assert second_word("cat '~'/x").text == "~/x"
    for command in (
        "cat $x",
        "cat `x`",
        "cat *.c",
        "cat a?",
        "cat [ab]",
        "cat {a,b}",
        'cat "$x"',
        "cat $'x'",
        "cat x=~/y",
        "cat a:~/y",
    ):
        assert not second_word(command).literal, command
    for command in ("cat plain", "cat 'a b'", 'cat "a b"', "cat a\\*b", "cat a=b"):
        assert second_word(command).literal, command
    value = parse.shell_layer("A=x:~/b").assignments[0].value
    assert not value.literal


# --- Rule 4 --------------------------------------------------------------------------

R4_OPERATORS = ("<", ">", ">>", ">|", "&>", "&>>", "<>", ">&", "<&")
R4_DESCRIPTORS = ("", "0", "1", "2", "3", "1" * 40, "{fd}")
R4_TARGETS = (
    "0",
    "1",
    "2",
    "-",
    "1-",
    "3",
    "$x",
    "3-",
    "out.txt",
    "~/f",
    "/dev/null",
    "/dev/fd/3",
    "/dev/fd/03",
    "//dev/fd/3",
    "/proc/self/fd/3",
    "/proc/self/root/dev/fd/3",
    "/proc/self/cwd/x",
    '"$f"',
    "/dev/f?/3",
    "/dev/@(fd)/3",
    "/dev/fd/3@(x|)",
    "../x",
)
R4_NON_LITERAL = {"$x", '"$f"', "/dev/f?/3", "/dev/@(fd)/3", "/dev/fd/3@(x|)"}


def rule4_allows(fd: str, op: str, target: str) -> bool:
    """Rule 4, written out from the brief."""
    if fd not in ("", "0", "1", "2"):
        return False
    if op in (">&", "<&"):
        if target in ("0", "1", "2", "-", "0-", "1-", "2-"):
            return True
        if op == "<&" or fd or re.fullmatch(r"[0-9]+-?", target):
            return False
    if target in R4_NON_LITERAL:
        return False
    path = target[1:] if target.startswith("~") else target
    if ".." in path.split("/"):
        return False
    if target.startswith("~") or not target.startswith("/"):
        # A relative or `~` target that may be a descriptor of 3 or more (`3`, `fd/3`).
        parts = [p for p in path.split("/") if p]
        if parts and parts[-1].isdigit() and int(parts[-1]) >= 3:
            if len(parts) == 1 or parts[-2] == "fd":
                return False
    if target.startswith("/"):
        norm = re.sub(r"/+", "/", target)
        if norm in ("/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"):
            return True
        if norm.startswith(("/dev/tcp/", "/dev/udp/")):
            return True
        if norm.startswith(("/dev/", "/proc/", "/sys/")):
            return False
    return True


def rule4_table() -> list[tuple[str, bool]]:
    # tree-sitter-bash 0.25.1 has no `<>` after a command's arguments: every `<>` input
    # here parses with an ERROR node, so Rule 1's untrusted-redirect clause rejects it
    # whatever Rule 4 says (`test_rule4_judge_alone` checks Rule 4 on its own).
    return [
        (f"echo x {fd}{op}{target}", op != "<>" and rule4_allows(fd, op, target))
        for op in R4_OPERATORS
        for fd in R4_DESCRIPTORS
        for target in R4_TARGETS
    ]


def test_rule4_redirect_table():
    wrong = []
    for command, allowed in rule4_table():
        result = classify(command)
        assert not has_reason(result, COULD_NOT), (command, result)
        if result.rejected is allowed:
            wrong.append((command, allowed, result.reasons))
    assert wrong == []


def test_rule4_judge_alone():
    parse = shell()
    wrong = []
    for op in R4_OPERATORS:
        for fd in R4_DESCRIPTORS:
            for target in R4_TARGETS:
                accepted = parse.judge_redirect(fd or None, op, parse.make_word(target))[0]
                if accepted is not rule4_allows(fd, op, target):
                    wrong.append((fd, op, target, accepted))
    assert wrong == []
    for op in (">&-", "<&-"):
        for fd in R4_DESCRIPTORS:
            accepted = parse.judge_redirect(fd or None, op, None)[0]
            assert accepted is (fd in ("", "0", "1", "2")), (fd, op)


def test_rule4_spec_rows_and_exempt_forms():
    check(Case("echo hi >&3", None, True, REJ))
    check(Case("ls > ~/.bashrc", "mutating", False, OUT))
    check(Case("cat </dev/tcp/example.org/80", "mutating", False, "/dev/tcp"))
    for redirect in (">/dev/null", "2>&1", "1>&2", ">&2", "2>/dev/null"):
        result = classify(f"echo x {redirect}")
        assert result.reasons == (NA,), (redirect, result)
    for command in ("exec 3>&-", "{a[0]}>x", "{a[0]}>&-", "echo {fd}>x", "echo x 3>&1-"):
        assert classify(command).rejected, command


def test_rule4_relative_targets_that_may_be_a_descriptor():
    # With the cwd at /dev/fd (or ~ there), `>3` writes to fd 3, the control channel.
    for command in (
        "printf p >3",
        "printf p >03",
        "printf p 2>fd/3",
        "cat <self/fd/4",
        "printf p >~/3",
        "cd /dev/fd && printf 'p\\n' >3",
        "HOME=/dev/fd; printf 'p\\n' >~/3",
    ):
        check(Case(command, None, True, "descriptor 3 or higher"))
    for command in ("printf p >2", "printf p >out/3", "printf p >3x", "printf p >~/f"):
        assert not classify(command).rejected, command


def test_rule4_relative_targets_after_the_cwd_moves():
    moved = "the command moves where the gate cannot place it"
    for command in (
        "cd /dev && echo x >sda",
        'cd "$d" && echo x >f',
        "cd - && echo x >f",
        "cd .. && echo x >f",
        "popd; echo x >f",
        "CDPATH=/dev; cd fd; echo x >f",
        "HOME=/dev; echo x >~/sda",
        "eval x; echo y >f",
        "trap 'cd /dev' DEBUG; echo y >f",
        "f() { cd /dev; }; f; echo y >sda",
        "$cmd; echo y >f",
        "while :; do echo y >f; cd /dev; done",
    ):
        check(Case(command, None, True, moved))
    # A literal move outside /dev, /proc and /sys keeps relative targets placed; where
    # the session's own cwd is, is the live layer's check (§8.1).
    for command in (
        "cd /tmp && echo x >out.txt",
        "cd build && make >log",
        "cd && echo x >f",
        "cd -P /tmp && echo x >f",
        "pushd /tmp; echo x >f",
        "HOME=/home/u; echo x >~/f",
        "cd /dev; echo x >/dev/null",
        "eval x; echo y >/tmp/abs",
    ):
        assert not classify(command).rejected, command


def test_single_bracket_test_redirects():
    # tree-sitter-bash reads `>`/`<` inside `[ ]` as test operators; bash runs `[` as a
    # command and performs them as redirections.
    for command in ("[ x > /dev/fd/3 ]", "[ a < b ]", "[ -n x ] && [ y > z ]"):
        for origin in ("typed", "proposal"):
            check(Case(command, None, True, "inside [ ]", origin=origin))
    parse = shell()
    assert any("inside [ ]" in f for f in parse.shell_layer("[ x > y ]").untrusted)
    # `[[ ]]` compares strings, and an escaped `\>` in `[ ]` is an argument.
    for command in ("[[ a < b ]]", "[ a \\> b ]"):
        assert not classify(command).rejected, command


# --- Rule 5 --------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["$[", "a=(", "${"])
def test_rule5_size_cap_at_8_kib(unit):
    command = (unit * 8192)[:8192]
    assert len(command.encode()) == 8192
    start = time.monotonic()
    result = gate().classify(command)
    assert time.monotonic() - start < 5.0
    assert_clean(result.reasons)
    assert not has_reason(result, "input over 8 KiB"), result


def test_rule5_over_cap_is_never_parsed(monkeypatch):
    parse = shell()
    calls = []

    def boom(data):
        calls.append(len(data))
        raise AssertionError("parser reached")

    monkeypatch.setattr(parse, "parse_source", boom)
    for command in ("a" * 8193, "é" * 4096 + "a"):
        result = classify(command)
        assert (result.cls, result.rejected) == ("privileged", True), result
        assert result.reasons == (COULD_NOT, "input over 8 KiB")
    assert calls == []


def test_rule5_work_budget_and_failures(monkeypatch):
    parse = shell()
    deep = "echo " + "$(echo " * 150 + ")" * 150
    chain = "echo " + "$(:)" * 1500
    for command in (deep, chain):
        assert len(command.encode()) <= 8192
        result = classify(command)
        assert (result.cls, result.rejected) == ("privileged", True), result
        assert has_reason(result, COULD_NOT), result
    assert has_reason(classify(deep), "nesting too deep")
    assert has_reason(classify(chain), "work budget")
    with monkeypatch.context() as m:
        m.setattr(parse, "untrusted_features", lambda *a: 1 / 0)
        result = classify("ls")
        assert (result.cls, result.rejected) == ("privileged", True)
        assert result.reasons == (COULD_NOT, "internal error (ZeroDivisionError)")
    with monkeypatch.context() as m:
        from nl_shell.gate import rules

        m.setattr(rules, "classify", lambda inv: (_ for _ in ()).throw(RuntimeError("x")))
        assert classify("ls").reasons[0] == COULD_NOT
    for bad in (None, 3, b"ls"):
        assert gate().classify(bad).reasons[0] == COULD_NOT
    assert gate().classify("ls", origin="model").reasons[0] == COULD_NOT
    assert gate().classify("ls", label="safe").reasons[0] == COULD_NOT


# --- Rule 6 --------------------------------------------------------------------------


def test_rule6_reasons_never_carry_raw_input():
    result = classify("function f$'\x1b' { :; }")
    joined = "".join(result.reasons)
    assert ESC not in joined
    assert has_reason(result, "\\x1b"), result
    for command in (
        "f\x1b() { :; }",
        "echo x >'a\x1bb'",
        "echo x >'\u202e'",
        "echo x 3>'a\nb'",
        "PAGER\u200b=x ls",
        "ls \x9b",
    ):
        for origin in ("typed", "proposal"):
            classify(command, origin)
    for command in all_inputs():
        for origin in ("typed", "proposal"):
            classify(command, origin)


# --- regression table ----------------------------------------------------------------


@pytest.mark.parametrize("case", REGRESSION, ids=lambda c: repr(c.command)[:60])
def test_regression_table(case):
    check(case)


def test_regression_table_from_vectors():
    cases = vector_cases()
    assert len(cases) >= 9
    for case in cases:
        check(case)


def test_nothing_is_read_only():
    for command in all_inputs():
        for origin in ("typed", "proposal"):
            assert classify(command, origin).cls != "read-only", command


# --- other spec rules ----------------------------------------------------------------


def read_only_for(word: str):
    """A command layer that calls ``word`` read-only and everything else mutating."""
    from nl_shell.gate import rules

    def fake(invocation):
        if invocation.words[0].text == word:
            return rules.Verdict("read-only")
        return rules.Verdict("mutating", (NA,))

    return fake


def test_model_label(monkeypatch):
    from nl_shell.gate import live, rules

    assert classify("true", label="destructive").cls == "destructive"
    assert classify("true", label="read-only").cls == "mutating"
    monkeypatch.setattr(rules, "classify", read_only_for("true"))
    assert classify("true", label="destructive").cls == "destructive"
    result = classify("true", label="read-only")
    assert result.cls == "mutating"
    assert has_reason(result, live.NOT_IMPLEMENTED)


def test_live_stub(monkeypatch):
    from nl_shell.gate import live, rules

    assert live.NOT_IMPLEMENTED == "live-session resolution not implemented"
    monkeypatch.setattr(rules, "classify", read_only_for("ls"))
    result = classify("ls")
    assert (result.cls, result.rejected) == ("mutating", False)
    assert result.reasons == (live.NOT_IMPLEMENTED,)
    invocation = shell().shell_layer("ls").invocations[0]
    direct = live.resolve(invocation, rules.Verdict("read-only"))
    assert (direct.cls, direct.reasons) == ("mutating", (live.NOT_IMPLEMENTED,))
    kept = rules.Verdict("destructive", ("y",))
    assert live.resolve(invocation, kept) == kept


def test_walk_reaches_every_simple_command(monkeypatch):
    from nl_shell.gate import rules

    seen = []

    def record(invocation):
        seen.append((invocation.kind, invocation.words[0].text))
        return rules.Verdict("mutating", (NA,))

    monkeypatch.setattr(rules, "classify", record)
    command = (
        "a | b; c && (d) || { e; }; if f; then g; elif h; then i; else j; fi; "
        "while k; do l; done; for x in 1; do m; done; case x in y) n;; esac; "
        "fn() { o; }; echo $(p) `q`; export R=1; unset S"
    )
    classify(command)
    words = {w for _, w in seen}
    assert set("abcdefghijklmnopq") <= words
    assert ("declaration", "export") in seen
    assert ("unset", "unset") in seen


def test_shell_layer_reasons():
    assert has_reason(classify("ls >out"), OUT)
    assert not has_reason(classify("/usr/bin/rm x"), "path outside")
    assert not has_reason(classify("//usr/bin/rm x"), "path outside")
    assert has_reason(classify("/usr/bin/../../tmp/rm x"), "path outside")
    assert has_reason(classify("PATH=x"), "assignment to PATH")
    assert has_reason(classify("declare PS4=x"), "assignment to PS4")
    assert has_reason(classify("PERL5LIB=x"), "assignment to PERL5LIB")
    assert classify("PAGER=sh git log").cls == "mutating"  # a prefix only lasts one command
    assert classify("A=1 SUDO_PROMPT=x").rejected


def test_result_has_session_log_shape():
    result = classify("ls >out")
    assert result.as_log() == {
        "class": "mutating",
        "reasons": [OUT + ": >out", NA],
        "rejected": False,
    }


# --- vectors loader ------------------------------------------------------------------


def test_vectors_file_loads():
    from nl_shell.gate import vectors

    loaded = vectors.load_target("ubuntu-26.04")
    assert loaded == vectors.load(VECTORS)
    assert {v.row for v in loaded} >= {7, 13, 14, 15, 16, 23, 25, 30, 31}
    text = VECTORS.read_text(encoding="utf-8")
    assert "#192" in text and "landed so far" in text


GOOD = 'command = "ls"\norigin = "typed"\nrejected = false\nclass = "mutating"\n'


@pytest.mark.parametrize(
    "body,needle",
    [
        (GOOD.replace('command = "ls"\n', ""), "missing required key `command`"),
        (GOOD.replace('origin = "typed"\n', ""), "missing required key `origin`"),
        (GOOD.replace("rejected = false\n", ""), "missing required key `rejected`"),
        (GOOD.replace("rejected = false", "rejected = true"), "`class` must be absent"),
        (GOOD.replace('class = "mutating"\n', ""), "missing `class`"),
        (GOOD.replace('"mutating"', '"harmless"'), "`class` must be one of"),
        (GOOD + 'label = "harmless"\n', "`label` must be one of"),
        (GOOD.replace('"typed"', '"model"'), "`origin` must be one of"),
        (GOOD + 'colour = "red"\n', "unknown key `colour`"),
    ],
)
def test_vectors_loader_rejects(tmp_path, body, needle):
    from nl_shell.gate import vectors

    path = tmp_path / "gate-vectors.toml"
    path.write_text(f"[[vector]]\n{GOOD}\n[[vector]]\nrow = 4\n{body}", encoding="utf-8")
    with pytest.raises(vectors.VectorError) as info:
        vectors.load(path)
    message = str(info.value)
    assert needle in message
    assert "vector 2" in message and "row 4" in message


def test_vectors_loader_rejects_invalid_toml(tmp_path):
    from nl_shell.gate import vectors

    path = tmp_path / "gate-vectors.toml"
    path.write_text("[[vector]\ncommand = ", encoding="utf-8")
    with pytest.raises(vectors.VectorError, match="not valid TOML"):
        vectors.load(path)
