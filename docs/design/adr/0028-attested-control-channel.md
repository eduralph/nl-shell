---
created: 28.10.2026 18:05
type: adr
status: Proposed
supersedes: ADR-0014 (control channel transport, point 1)
tags:
  - adr
---
# 0028. The control channel attests its writer: an AF_UNIX socket with SO_PASSCRED

## Context

ADR-0014 puts the session's start and done records and its snapshot on a pipe that is
file descriptor 3 in bash. The argument for trusting a record is indirect: printed text
cannot reach fd 3; a redirection to fd 3 is rejected by the gate; and a process that
writes to its inherited fd 3 on purpose is "code running as the user, outside the threat
model". The threat model lists the remainder as a known gap, *fake fd-3 records* (#135):
every child a confirmed command starts — `make`, a repository's script, a background
job — inherits fd 3 and can write a `D` record that ends capture early, or a snapshot with
`H` equal to the baseline while the hooks were replaced. ADR-0027 keeps the fd-3
rejection as an over-approximating text scan, because the pipe itself cannot tell nl-shell
who wrote.

There are two writers to tell apart from the hooks, and they are not alike:

- **Other processes** (children of a command, background jobs, anything that inherited
  fd 3). These have a different pid from the session's bash.
- **The confirmed command's own text**, running in the main shell. This has the same pid
  as the hooks and every power they have: it can read any variable, so a per-session
  token in bash memory would be visible to it, and it can run any builtin, so a
  challenge-response over a second descriptor would be answerable by it. No mechanism
  inside the process separates the hooks from the text. What separates them is that
  the text is visible, finite and confirmed by the user, and scanned by the gate.

Facts checked on Linux 6.18 with bash 5.2: when the receiving end of an AF_UNIX
`SOCK_STREAM` socket has `SO_PASSCRED` set, the kernel attaches `SCM_CREDENTIALS` (pid,
uid, gid of the process that called `write`) to every write, including a plain `>&3`
from a bash builtin; a `recvmsg` never returns bytes from two different writers in one
read; `printf … >&3` from the main shell carries bash's pid, and the same `printf` inside
`( … )`, inside `$( … )`, from `/usr/bin/printf` or from a child `bash -c` carries the
child's pid. bash writes to a socket descriptor exactly as to a pipe; ptyprocess passes
either. Bash 5.3 (the Ubuntu 26.04 release) has the non-forking `${ cmd; }` command
substitution.

## Decision

1. **The control channel is an AF_UNIX `SOCK_STREAM` socket pair**, not a pipe. nl-shell
   sets `SO_PASSCRED` on its end before it starts the session and reads records with
   `recvmsg`, taking the kernel-attested pid of each read's writer.
2. **A `D` record, every snapshot record and `E` MUST carry bash's own pid.** A record
   of these types whose attested pid is not the session's bash pid is *foreign*: it is
   not parsed and not counted, and the session degrades with the reason "a foreign
   process wrote to the control channel", naming the pid and its `/proc/<pid>/comm`
   when still readable. The sequencing rules of ADR-0014 §4 then apply to attested
   records only.
3. **The hooks write every such record from the main shell process.** `__nls_enc` no
   longer writes from a subshell: it computes the escaped text in the main shell (the
   `LC_ALL=C` escaping runs in a function-local `declare`, which bash scopes without a
   fork) and the `printf … >&3` is the main shell's. The `$( … )` bodies in `__nls_snap`
   keep producing text; they write nothing to fd 3.
4. **`S` is advisory.** `PS0` expands `$( … )` in a fork, so `S` arrives with a child pid
   on bash 5.2 and older; on 5.3 the rcfile uses `${ … ; }` and `S` is attested too. In
   either case `S` only decides whether "nothing ran" is reported for a `D` without an
   `S`; it never ends capture, never carries state and never grants anything, and a
   forged or missing `S` can at most degrade the session (out of order). nl-shell MUST
   NOT rely on `S` for any safety-relevant decision; the spec records this explicitly.
5. **The gate's fd-3 rejection stays** (ADR-0027 point 4, spec `policy-gate/v2` §5), for
   the one writer this ADR cannot separate: the confirmed command text itself. Its
   justification changes from "the only defence" to "the defence for the same-pid case".
6. **Process identity is also checked out of band.** The pid alone is not identity after
   `exec`: a confirmed `exec prog` keeps the pid and the descriptor. nl-shell records
   bash's start time (`/proc/<pid>/stat` field 22) and `/proc/<pid>/exe` at session
   start and compares both at every `E` together with the existing fd 0–2 check
   (pty-protocol §3, outside checks); a difference degrades the session. An `exec bash`
   keeps `exe` but loses the hooks, and the prompt rule (§4.2) degrades it as today.

Alternatives considered:

- **A per-session token in the records** (`D <n> <status> <token>`). Rejected: the token
  lives in bash memory, where the command text can read it, and child processes can
  read it from `/proc/<pid>/environ` if exported or ask bash to print it. It adds a
  secret to manage and separates nothing the kernel does not already separate better.
- **A FIFO opened per write by path**, so no descriptor is inherited. Rejected: any
  same-user process can open the path too, and the hooks then depend on a path in a
  variable the text can read.
- **`SO_PEERCRED`** on a connected socket. Rejected: it attests the peer at connect time,
  which is bash; children inherit the connected socket and write as it.
- **Accept `D` only once bash is observed blocked at a readline prompt** (extending the
  prompt rule into a positive proof). Not adopted as the primary mechanism: it rests on
  terminal-flag reasoning the protocol already marks as fragile (#149), and the ordering
  of a forged sequence followed by a real prompt is observably identical to the real
  one. It remains a defence in depth that the existing outside checks already provide
  in part.

## Consequences

- "Only the session's main shell can write records" becomes kernel-enforced instead of
  argued from the gate. #135 closes for every process that is not bash itself:
  children of confirmed commands, background jobs, programs that write to an inherited
  fd 3 by accident (some build tools and test runners use fd 3). The last group used to
  be a malformed-record degrade with an opaque reason; it now names the process.
- The gap that remains is the confirmed text, caught by ADR-0027's scan. The threat
  model's *fake fd-3 records* entry is rewritten to say exactly that.
- `session.py` changes its transport (`socketpair` + `recvmsg` instead of a pipe) and the
  reference rcfile changes in `__nls_enc` and, on bash 5.3, in `PS0`; the rcfile/`RCFILE`
  equality test and the hook tests follow. Spec `pty-protocol/v1` becomes `v2`: §1.1 the
  transport, §1.3 the main-shell rule for writes, §2 the attested-pid requirement, §3 the
  start-time and `exe` fields in the outside checks, §4.2 the foreign-writer degrade.
- The CI bash matrix (5.1, 5.2, 5.3) gains one case: `S` attested on 5.3 and advisory
  below; the vectors in pty-protocol §5 gain a foreign-writer row (a child writes a
  well-formed `D` → degraded, named) and a same-pid row (the main shell's own `printf
  'D …' >&3`, which this ADR deliberately does not catch and the gate scan must).
- Linux-only, like the outside checks already are (`SO_PASSCRED` is Linux; the BSDs have
  `LOCAL_CREDS` with different semantics). ADR-0023's targets are Linux, so nothing is
  foreclosed that was open.
- Reversal is cheap: the record format is unchanged, so going back to a pipe is a
  transport swap and the loss of point 2.
