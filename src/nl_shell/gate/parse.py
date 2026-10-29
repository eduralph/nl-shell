"""The advisory walk (ADR-0027 point 5, spec ``policy-gate/v2`` §6).

It parses a command with the pinned tree-sitter-bash and walks the tree with one handler
per node kind (``HANDLERS``). Each simple command becomes an ``Invocation``; assignments,
function definitions, redirections and process substitutions are collected on the
``ShellLayer``.

What this layer is for, and what it is not: its findings may **raise** a command's class
and word the confirm (a destructive command, a session-altering builtin, an output
redirection, the inner command of a wrapper). They can never grant ``read-only`` — only
the grammar (``nl_shell.gate.grammar``) can — and they never decide a rejection, which is
the text scan's job (``nl_shell.gate.scan``). So the tree does not have to be proven
faithful to bash: a parse error, a construct the walk does not handle, a budget overrun
or any internal error simply yields a layer with ``analysed = False`` and nothing in it,
and the command is confirmed as ``mutating`` with no further detail. ``shell_layer``
never raises.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace

import tree_sitter
import tree_sitter_bash

from nl_shell.gate.types import (
    CLASSES,
    PIPE,
    RANK,
    TERMINAL,
    UNKNOWN,
    Assignment,
    Invocation,
    Redirect,
    Stdin,
    Word,
)

__all__ = [
    "CLASSES",
    "RANK",
    "Assignment",
    "Invocation",
    "Redirect",
    "ShellLayer",
    "Stdin",
    "Word",
    "has_device_socket",
    "lex_word",
    "make_word",
    "parse_source",
    "shell_layer",
]

# Proposals are capped at 2000 characters (proposal.py), so these bounds bind only typed
# input. tree-sitter-bash's parse time grows with the square of the input, which is why
# the size cap is checked before parsing. Exceeding a bound ends the analysis, nothing
# more.
MAX_INPUT_BYTES = 8 * 1024
MAX_WORK = 4096
MAX_DEPTH = 100

LANGUAGE = tree_sitter.Language(tree_sitter_bash.language())

ERROR_KIND = "ERROR"
SIMPLE_KINDS = ("command", "declaration_command", "unset_command")
REDIRECT_KINDS = ("file_redirect", "heredoc_redirect", "herestring_redirect")
# Anonymous tokens that are redirect operators.
REDIRECT_OPERATORS = frozenset(
    (">", ">>", ">|", "&>", "&>>", "<>", ">&", "<&", ">&-", "<&-", "<", "<<", "<<-", "<<<")
)
_OUTPUT_FILE_OPERATORS = (">", ">>", ">|", "&>", "&>>")
_INPUT_OPERATORS = ("<", "<&", "<&-")  # default descriptor 0
_DEV_SOCKETS = ("/dev/tcp/", "/dev/udp/")
_PLAIN_DELIMITER = re.compile(r"[A-Za-z0-9_]+")
_QUOTED_DELIMITER = re.compile(r"'[^']*'|\"[^\"\\$`]*\"")
_LOW_FD = ("0", "1", "2")


class _Stop(Exception):
    """The walk hit a bound or a construct it does not handle; the layer stays empty."""


def named_kinds() -> tuple[str, ...]:
    """Every named node kind of the pinned grammar, plus ``ERROR``."""
    kinds = {
        LANGUAGE.node_kind_for_id(i)
        for i in range(LANGUAGE.node_kind_count)
        if LANGUAGE.node_kind_is_named(i) and LANGUAGE.node_kind_is_visible(i)
    }
    kinds.add(ERROR_KIND)
    return tuple(sorted(kinds))


def parse_source(source: bytes) -> tree_sitter.Tree:
    """Parse ``source`` with the pinned grammar (the one entry point to the parser)."""
    return tree_sitter.Parser(LANGUAGE).parse(source)


@dataclass
class ShellLayer:
    """What the walk found in one command. ``analysed`` is False when the walk did not
    finish; everything is then empty."""

    analysed: bool = True
    invocations: list[Invocation] = field(default_factory=list)
    assignments: list[Assignment] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    process_substitutions: int = 0
    words: list[Word] = field(default_factory=list)


# --- the literal flag ----------------------------------------------------------------


def lex_word(source: str) -> tuple[str, frozenset[str]]:
    """Quote-remove ``source`` as bash would and say what bash would expand in it.

    Returns the text and the set of causes that make it non-literal (empty means
    literal).
    """
    out: list[str] = []
    causes: set[str] = set()
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == "\\":
            if i + 1 < n:
                if source[i + 1] == "\n":
                    causes.add("line continuation")
                else:
                    out.append(source[i + 1])
                i += 2
            else:
                out.append(c)
                i += 1
            continue
        if c == "'":
            end = source.find("'", i + 1)
            if end < 0:
                causes.add("unclosed quote")
                out.append(source[i + 1 :])
                break
            out.append(source[i + 1 : end])
            i = end + 1
            continue
        if c == '"':
            i += 1
            closed = False
            while i < n:
                d = source[i]
                if d == '"':
                    closed = True
                    i += 1
                    break
                if d == "\\" and i + 1 < n and source[i + 1] in '$`"\\\n':
                    if source[i + 1] == "\n":
                        causes.add("line continuation")
                    else:
                        out.append(source[i + 1])
                    i += 2
                    continue
                if d in "$`":
                    causes.add("expansion")
                out.append(d)
                i += 1
            if not closed:
                causes.add("unclosed quote")
            continue
        if c in "$`":
            causes.add("expansion")
        elif c in "*?[":
            causes.add("glob")
        elif c in "{}":
            causes.add("brace")
        elif c in "()":
            causes.add("pattern or substitution")
        elif c in " \t\n;&|<>":
            causes.add("metacharacter")
        elif c == "~":
            if i == 0 or source[i - 1] in "=:":
                causes.add("tilde")
        out.append(c)
        i += 1
    return "".join(out), frozenset(causes)


def make_word(source: str) -> Word:
    text, causes = lex_word(source)
    return Word(text=text, literal=not causes, source=source)


def has_device_socket(text: str) -> bool:
    collapsed = re.sub(r"/+", "/", text)
    return any(name in collapsed for name in _DEV_SOCKETS)


# --- redirects -----------------------------------------------------------------------


def judge_redirect(
    descriptor: str | None, operator: str, target: Word | None
) -> tuple[bool, bool]:
    """``(exempt, device_socket)`` for a file redirect. Exempt are the forms that write
    nowhere new: ``>/dev/null``, ``2>&1``, ``1>&2``, ``>&2``."""
    if target is None or not target.literal:
        return False, False
    text = target.text
    if operator in (">&", "<&"):
        exempt = (descriptor, operator, text) in (
            ("2", ">&", "1"),
            ("1", ">&", "2"),
            (None, ">&", "2"),
        )
        return exempt, False
    path = re.sub(r"/+", "/", text)
    if path == "/dev/null" and operator in _OUTPUT_FILE_OPERATORS:
        return True, False
    return False, has_device_socket(path)


def _is_output(descriptor: str | None, operator: str, kind: str) -> bool:
    if kind != "file_redirect":
        return False
    if operator in _INPUT_OPERATORS and descriptor in (None, "0"):
        return False
    return True


def _iter_nodes(root: tree_sitter.Node) -> Iterator[tree_sitter.Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


# --- the walk ------------------------------------------------------------------------


@dataclass(frozen=True)
class Ctx:
    stdin: Stdin
    in_function: bool = False
    role: str | None = None  # how a variable_assignment child is used


Handler = Callable[["Walker", tree_sitter.Node, Ctx, int, tuple], object]


class Walker:
    def __init__(self, source: bytes, layer: ShellLayer) -> None:
        self.source = source
        self.layer = layer
        self.work = 0

    def text(self, node: tree_sitter.Node) -> str:
        return self.source[node.start_byte : node.end_byte].decode("utf-8", "surrogatepass")

    def span(self, start: int, end: int) -> str:
        return self.source[start:end].decode("utf-8", "surrogatepass")

    def visit(self, node: tree_sitter.Node, ctx: Ctx, depth: int, bound: tuple = ()):
        self.work += 1
        if self.work > MAX_WORK or depth > MAX_DEPTH:
            raise _Stop
        kind = ERROR_KIND if node.is_error else node.type
        handler = HANDLERS.get(kind)
        if handler is None:
            raise _Stop
        return handler(self, node, ctx, depth, bound)

    # Descriptor written right before a redirect operator that the tree did not attach
    # (`cat 0>&-` parses as the argument `0` and the redirect `>&-`).
    def adjacent_token(self, start: int) -> tuple[int, str] | None:
        i = start
        while i > 0 and self.source[i - 1] not in b" \t\n;&|()<>":
            i -= 1
        if i == start:
            return None
        return i, self.span(i, start)


def _children(walker: Walker, node, ctx: Ctx, depth: int, bound: tuple = ()) -> None:
    if bound:
        ctx = _apply_compound(walker, bound, ctx, depth)
    for child in node.named_children:
        walker.visit(child, ctx, depth + 1)


def _apply_compound(walker: Walker, bound: tuple, ctx: Ctx, depth: int) -> Ctx:
    """Redirects on a compound command: record them, and let them set the stdin the
    commands inside inherit."""
    stdin = ctx.stdin
    for node in bound:
        for record in walker.visit(node, ctx, depth + 1):
            stdin = _stdin_after(record, stdin)
    return replace(ctx, stdin=stdin)


def _stdin_after(record: Redirect, stdin: Stdin) -> Stdin:
    if record.kind in ("heredoc_redirect", "herestring_redirect"):
        if record.descriptor in (None, "0"):
            return record.feed or UNKNOWN
        if record.descriptor in _LOW_FD:
            return stdin
        return UNKNOWN
    if record.descriptor is not None and not record.descriptor.isdigit():
        return UNKNOWN
    if record.descriptor is None:
        fd = "0" if record.operator in _INPUT_OPERATORS or record.operator == "<>" else "1"
    else:
        fd = record.descriptor.lstrip("0") or "0"
    if fd != "0":
        return stdin
    if record.operator == "<" and record.target is not None and record.target.literal:
        return Stdin("file", record.target.text)
    return UNKNOWN


def _pipeline(walker: Walker, node, ctx: Ctx, depth: int, bound: tuple = ()) -> None:
    stages, later = [], False
    for child in node.children:
        if not child.is_named:
            if child.type in ("|", "|&"):
                later = True
            continue
        stages.append((child, later))
    # A trailing redirect on a pipeline belongs to its last stage, as in bash.
    last = max((i for i, (c, _) in enumerate(stages) if c.type != "comment"), default=-1)
    for i, (child, is_later) in enumerate(stages):
        stage_ctx = replace(ctx, stdin=PIPE) if is_later else ctx
        walker.visit(child, stage_ctx, depth + 1, bound if i == last else ())
    if bound and last < 0:
        _apply_compound(walker, bound, ctx, depth)


def _negated(walker: Walker, node, ctx: Ctx, depth: int, bound: tuple = ()) -> None:
    inner = node.named_children
    if bound and len(inner) == 1:
        walker.visit(inner[0], ctx, depth + 1, bound)
        return
    _children(walker, node, ctx, depth, bound)


def _redirected(walker: Walker, node, ctx: Ctx, depth: int, bound: tuple = ()) -> None:
    body = node.child_by_field_name("body")
    mine = tuple(c for c in node.named_children if body is None or c.id != body.id)
    redirects = tuple(c for c in mine if c.type in REDIRECT_KINDS)
    others = tuple(c for c in mine if c.type not in REDIRECT_KINDS)
    for other in others:
        walker.visit(other, ctx, depth + 1)
    if body is None:
        _apply_compound(walker, redirects + bound, ctx, depth)
        return
    walker.visit(body, ctx, depth + 1, redirects + bound)


def _simple(walker: Walker, node, ctx: Ctx, depth: int, bound: tuple = ()) -> None:
    kind = {"command": "command", "declaration_command": "declaration"}.get(
        node.type, "unset"
    )
    word_nodes: list = []  # name and argument children, named or not
    prefix: list = []
    redirect_nodes: list = list(bound)
    others: list = []
    unread: list = []  # walked, but not words (a zero-width MISSING command name)
    keyword = None
    seen_name = False
    has_name = False
    for i, child in enumerate(node.children):
        fname = node.field_name_for_child(i)
        if not child.is_named:
            if fname == "argument":
                word_nodes.append(child)
            elif keyword is None and kind != "command" and not seen_name:
                keyword = child
                seen_name = True
            continue
        if child.type in REDIRECT_KINDS:
            redirect_nodes.append(child)
        elif fname == "name" and child.end_byte == child.start_byte:
            # `PATH=x >/dev/null` parses with a MISSING command name: bash runs no
            # command there, so this is no name.
            unread.append(child)
            seen_name = True
        elif fname == "name":
            word_nodes.append(child)
            seen_name = True
            has_name = True
        elif child.type == "variable_assignment" and kind == "command" and not seen_name:
            prefix.append(child)
        elif fname == "argument" or kind != "command":
            word_nodes.append(child)
        else:
            others.append(child)
    redirect_nodes.sort(key=lambda n: n.start_byte)

    # Descriptors the tree left as a separate word (`cat 0>&-`). Only a token that is a
    # descriptor by the same test `_descriptor` uses is taken out of the words; any other
    # word written against a redirect (`rm -rf />/dev/null`) stays an argument.
    fd_tokens = set()
    for r in redirect_nodes:
        if r.child_by_field_name("descriptor") is None:
            token = walker.adjacent_token(r.start_byte)
            if token is not None and _is_descriptor_token(token[1]):
                fd_tokens.add((token[0], r.start_byte))
    fd_words = [w for w in word_nodes if (w.start_byte, w.end_byte) in fd_tokens]
    word_nodes = [w for w in word_nodes if (w.start_byte, w.end_byte) not in fd_tokens]

    # With no command word (`PATH=x >/dev/null`) the assignment is not a one-command
    # prefix: bash keeps it in the shell, so it is standalone and §3 applies.
    prefix_role = "prefix" if has_name or kind != "command" else "standalone"
    assignments = []
    for child in prefix:
        assignments.append(walker.visit(child, replace(ctx, role=prefix_role), depth + 1))
    role = "declaration" if kind == "declaration" else None
    for child in word_nodes:
        if child.is_named:
            result = walker.visit(child, replace(ctx, role=role), depth + 1)
            if isinstance(result, Assignment):
                assignments.append(result)
    if kind == "declaration":
        # An operand that is not one `variable_assignment` node is still read by bash
        # as `name=value` after quote removal (`export 'SUDO_PROMPT=x'`). Pieces with no
        # space between them are one bash word (`export S'UDO_PROMPT'=x`).
        operands: list[list] = []
        for child in sorted(word_nodes, key=lambda n: n.start_byte):
            if operands and operands[-1][-1].end_byte == child.start_byte:
                operands[-1].append(child)
            else:
                operands.append([child])
        for pieces in operands:
            if len(pieces) == 1 and pieces[0].type == "variable_assignment":
                continue
            operand = _declared(walker.span(pieces[0].start_byte, pieces[-1].end_byte))
            if operand is not None:
                assignments.append(operand)
                walker.layer.assignments.append(operand)
    for child in others + unread + [w for w in fd_words if w.is_named]:
        walker.visit(child, ctx, depth + 1)
    records: list[Redirect] = []
    for child in redirect_nodes:
        records.extend(walker.visit(child, ctx, depth + 1))

    # Words: tree-sitter splits some bash words in two (`$"x"`); join pieces with no
    # space between them, as bash does.
    words: list[Word] = []
    if keyword is not None:
        words.append(make_word(walker.text(keyword)))
    groups: list[list[int]] = []
    for child in sorted(word_nodes + others, key=lambda n: n.start_byte):
        if groups and groups[-1][1] == child.start_byte:
            groups[-1][1] = child.end_byte
        else:
            groups.append([child.start_byte, child.end_byte])
    for start, end in groups:
        words.append(make_word(walker.span(start, end)))
    if kind == "command" and not has_name:
        words.insert(0, Word(text="", literal=False, source=""))
    walker.layer.words.extend(words)

    stdin = ctx.stdin
    for record in records:
        stdin = _stdin_after(record, stdin)
    walker.layer.invocations.append(
        Invocation(
            kind=kind,
            words=tuple(words),
            assignments=tuple(a for a in assignments if a is not None),
            redirects=tuple(records),
            stdin=stdin,
            in_function=ctx.in_function,
        )
    )


_DECLARED = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?\+?=(.*)", re.S)


def _declared(source: str) -> Assignment | None:
    """A declaration operand that is not a ``variable_assignment`` node, as bash reads it
    after quote removal: ``export 'SUDO_PROMPT=x'`` assigns ``SUDO_PROMPT``. An operand
    whose name the gate cannot read (``export "$x"``) gives the name ``""``; a literal
    option or plain name (``-x``, ``A``) gives ``None``."""
    word = make_word(source)
    match = _DECLARED.match(word.text)
    if match is not None:
        value = Word(text=match.group(2), literal=word.literal, source=source)
        return Assignment(name=match.group(1), value=value, role="declaration")
    if word.literal:
        return None
    return Assignment(name="", value=None, role="declaration")


def _assignment(walker: Walker, node, ctx: Ctx, depth: int, bound: tuple = ()):
    name_node = node.child_by_field_name("name")
    value_node = node.child_by_field_name("value")
    if name_node is not None and name_node.type == "subscript":
        inner = name_node.child_by_field_name("name")
        name_node = inner if inner is not None else name_node
    name = walker.text(name_node) if name_node is not None else walker.text(node)
    value = make_word(walker.text(value_node)) if value_node is not None else None
    record = Assignment(name=name, value=value, role=ctx.role or "standalone")
    walker.layer.assignments.append(record)
    if value is not None:
        walker.layer.words.append(value)
    _children(walker, node, replace(ctx, role=None), depth, bound)
    return record


def _function(walker: Walker, node, ctx: Ctx, depth: int, bound: tuple = ()) -> None:
    name = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    start = name.start_byte if name is not None else node.start_byte
    end = body.start_byte if body is not None else node.end_byte
    shown = re.sub(r"\s*\(\s*\)\s*$", "", walker.span(start, end).strip())
    walker.layer.functions.append(shown)
    inner = replace(ctx, stdin=UNKNOWN, in_function=True, role=None)
    redirects = [c for c in node.named_children if c.type in REDIRECT_KINDS]
    for child in node.named_children:
        if child.type in REDIRECT_KINDS:
            continue
        walker.visit(child, inner, depth + 1)
    for child in list(bound) + redirects:
        walker.visit(child, ctx, depth + 1)


def _command_substitution(walker: Walker, node, ctx, depth, bound=()) -> None:
    _children(walker, node, replace(ctx, role=None), depth, bound)


def _process_substitution(walker: Walker, node, ctx, depth, bound=()) -> None:
    walker.layer.process_substitutions += 1
    opener = node.children[0].type if node.child_count else ""
    inner = replace(ctx, role=None, stdin=PIPE if opener == ">(" else ctx.stdin)
    _children(walker, node, inner, depth, bound)


def _is_descriptor_token(text: str) -> bool:
    """Whether a token written right before a redirect operator is its descriptor: all
    digits (``0>&-``) or a ``{name}`` form (``{fd}>x``, ``{a[0]}>&-``). Anything else is an
    ordinary word, as in bash (``echo secret>out`` keeps ``secret`` as an argument)."""
    return text.isdigit() or (len(text) > 2 and text[0] == "{" and text[-1] == "}")


def _descriptor(walker: Walker, node) -> str | None:
    child = node.child_by_field_name("descriptor")
    if child is not None:
        return walker.text(child)
    token = walker.adjacent_token(node.start_byte)
    if token is None or not _is_descriptor_token(token[1]):
        return None
    return token[1]


def _operator(node) -> str:
    for child in node.children:
        if not child.is_named and child.type in REDIRECT_OPERATORS:
            return child.type
    return ""


def _file_redirect(walker: Walker, node, ctx, depth, bound=()) -> list[Redirect]:
    _children(walker, node, replace(ctx, role=None), depth)
    descriptor = _descriptor(walker, node)
    operator = _operator(node)
    targets = node.children_by_field_name("destination")
    target = make_word(walker.text(targets[0])) if len(targets) == 1 else None
    exempt, socket = judge_redirect(descriptor, operator, target)
    op_node = next((c for c in node.children if c.type == operator), None)
    op_start = op_node.start_byte if op_node is not None else node.start_byte
    record = Redirect(
        kind="file_redirect",
        descriptor=descriptor,
        operator=operator,
        target=target,
        output=_is_output(descriptor, operator, "file_redirect"),
        exempt=exempt,
        device_socket=socket or (target is not None and has_device_socket(target.text)),
        feed=None,
        display=(descriptor or "") + walker.span(op_start, node.end_byte),
    )
    walker.layer.redirects.append(record)
    return [record]


def _heredoc_redirect(walker: Walker, node, ctx, depth, bound=()) -> list[Redirect]:
    descriptor = _descriptor(walker, node)
    operator = _operator(node)
    delimiter = body = None
    nested: list[Redirect] = []
    piped = False
    for i, child in enumerate(node.children):
        fname = node.field_name_for_child(i)
        if not child.is_named:
            if child.type in ("|", "|&"):
                piped = True
            continue
        if child.type == "heredoc_start":
            delimiter = child
            walker.visit(child, ctx, depth + 1)
        elif child.type == "heredoc_body":
            body = child
            walker.visit(child, replace(ctx, role=None), depth + 1)
        elif child.type in REDIRECT_KINDS or fname == "redirect":
            result = walker.visit(child, ctx, depth + 1)
            nested.extend(result if isinstance(result, list) else [])
        elif child.type in ("heredoc_end", "file_descriptor"):
            walker.visit(child, ctx, depth + 1)
        else:
            # The rest of the command line, which tree-sitter hangs under the here-doc.
            if piped or child.type == "pipeline":
                rest = PIPE
            elif ctx.stdin.kind == "pipe":
                rest = UNKNOWN
            else:
                rest = ctx.stdin
            walker.visit(child, replace(ctx, stdin=rest, role=None), depth + 1)
    feed = UNKNOWN
    if delimiter is not None and body is not None and operator == "<<":
        delim = walker.text(delimiter)
        text = walker.text(body)
        if delim[:1] in ("'", '"') and _QUOTED_DELIMITER.fullmatch(delim):
            feed = Stdin("literal", text)
        elif _PLAIN_DELIMITER.fullmatch(delim) and not any(c in text for c in "$`\\"):
            feed = Stdin("literal", text)
    record = Redirect(
        kind="heredoc_redirect",
        descriptor=descriptor,
        operator=operator,
        target=None,
        output=False,
        exempt=False,
        device_socket=False,
        feed=feed,
        display=(descriptor or "") + operator + (walker.text(delimiter) if delimiter else ""),
    )
    walker.layer.redirects.append(record)
    return [record, *nested]


def _herestring_redirect(walker: Walker, node, ctx, depth, bound=()) -> list[Redirect]:
    _children(walker, node, replace(ctx, role=None), depth)
    descriptor = _descriptor(walker, node)
    pieces = [c for c in node.named_children if c.type != "file_descriptor"]
    word = None
    if pieces:
        word = make_word(walker.span(pieces[0].start_byte, pieces[-1].end_byte))
    feed = Stdin("literal", word.text) if word is not None and word.literal else UNKNOWN
    if word is not None:
        walker.layer.words.append(word)
    record = Redirect(
        kind="herestring_redirect",
        descriptor=descriptor,
        operator="<<<",
        target=word,
        output=False,
        exempt=False,
        device_socket=word is not None and has_device_socket(word.text),
        feed=feed,
        display=walker.text(node),
    )
    walker.layer.redirects.append(record)
    return [record]


HANDLERS: dict[str, Handler] = {kind: _children for kind in named_kinds()}
HANDLERS.update(
    {
        ERROR_KIND: _children,
        "pipeline": _pipeline,
        "negated_command": _negated,
        "redirected_statement": _redirected,
        "command": _simple,
        "declaration_command": _simple,
        "unset_command": _simple,
        "variable_assignment": _assignment,
        "function_definition": _function,
        "command_substitution": _command_substitution,
        "process_substitution": _process_substitution,
        "file_redirect": _file_redirect,
        "heredoc_redirect": _heredoc_redirect,
        "herestring_redirect": _herestring_redirect,
    }
)


def shell_layer(source: str) -> ShellLayer:
    """Parse and walk ``source``. Never raises: when the walk cannot finish, the layer
    comes back with ``analysed = False`` and nothing in it."""
    try:
        data = source.encode("utf-8", "surrogatepass")
        if len(data) > MAX_INPUT_BYTES:
            raise _Stop
        tree = parse_source(data)
        layer = ShellLayer()
        walker = Walker(data, layer)
        walker.visit(tree.root_node, Ctx(stdin=TERMINAL), 0)
        if any(_may_rewire_stdin(i) for i in layer.invocations):
            # A command that can change the shell's own stdin (`exec </x; sudo tee /y`)
            # makes every other command's stdin unproven.
            layer.invocations = [
                i if _may_rewire_stdin(i) else replace(i, stdin=UNKNOWN)
                for i in layer.invocations
            ]
        return layer
    except Exception:
        return ShellLayer(analysed=False)


def _head(invocation: Invocation) -> Word | None:
    """The command word, with ``command`` and ``builtin`` in front looked through; None
    when there is no command word."""
    words = list(invocation.words)
    while words and words[0].literal and words[0].text in _BUILTIN_PREFIXES:
        words.pop(0)
        while words and words[0].literal and words[0].text.startswith("-"):
            words.pop(0)
    if not words or not words[0].source:
        return None
    return words[0]


_SHELL_RUNNERS = ("eval", "source", ".")
# Builtins that install code to run later, which the gate does not see (`trap '…' DEBUG`
# runs before every later command).
_CODE_LATER = ("trap",)
_BUILTIN_PREFIXES = ("command", "builtin")


def _may_rewire_stdin(invocation: Invocation) -> bool:
    """Whether ``invocation`` can change the stdin of the shell itself: ``exec`` with
    any redirect, ``eval`` / ``source`` / ``.`` / ``trap`` (code the gate does not see), or
    a command word the gate cannot read. ``command`` and ``builtin`` in front are looked
    through."""
    head = _head(invocation)
    if head is None:
        return False  # no command word: a redirect alone applies to nothing
    if not head.literal:
        return True
    if head.text in _SHELL_RUNNERS or head.text in _CODE_LATER:
        return True
    return head.text == "exec" and bool(invocation.redirects)
