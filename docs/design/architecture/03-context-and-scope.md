---
created: 14.10.2026 19:22
type: architecture
status: living
tags:
  - architecture
  - context
---
# 3. Context and scope

> Living document.

## 3.1 System context

```mermaid
graph LR
    user(["User at the terminal"])
    nls["nl-shell"]
    bash["bash (PTY)"]
    sudo["sudo-rs"]
    sys["The system:<br/>files, packages, services"]
    docs["Local reference docs<br/>man, --help, tldr snapshot"]
    hist["~/.bash_history"]
    teacher["Teacher model on LAN<br/>(development only)"]

    user -->|"requests, confirms, sudo password"| nls
    nls -->|"confirmed commands"| bash
    bash --> sudo
    bash --> sys
    bash -->|"output (untrusted)"| nls
    nls -->|"reads"| docs
    nls <-->|"reads for retrieval, appends executed commands"| hist
    nls -.->|"optional, dev only"| teacher
```

## 3.2 External interfaces

| Interface | Direction | Trust |
|---|---|---|
| Terminal (keyboard in, screen out) | both | Input trusted; anything rendered from command output is not |
| PTY to bash | both | Commands out are gated; output in is untrusted |
| sudo prompt via the PTY | in | Passthrough only; nothing captured (ADR-0006) |
| Local reference docs | in | Local-reference taint |
| `~/.bash_history` | both | Read as trusted (it is the user's own); appended after each run |
| llama.cpp server or bindings | both | Local, no network for the command model |
| Teacher endpoint (dev only) | both | Model output from another host; not in the shipped config |

## 3.3 Out of context

No web access, no cloud APIs, no remote hosts (multi-host over SSH is backlog), no
graphical UI.
