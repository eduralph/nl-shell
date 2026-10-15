---
created: 14.10.2026 20:48
type: adr
status: Proposed
tags:
  - adr
---
# 0020. Configuration and file locations

## Context

#16 (prompts YAML), #18 (session log), #48 (installer) and #77 (teacher client) each need
to know where files live and how settings are given. Nothing decided it (#63, #70). One
risk in particular: a config file copied from a development machine, carrying a LAN
teacher URL, would silently send the user's state block over the network, breaking
ADR-0003 (locally complete).

## Decision

1. **XDG base directories**, with the standard fallbacks:

   | What | Where | Mode |
   |---|---|---|
   | Config (`config.toml`) and the editable prompts (`prompts.yaml`) | `$XDG_CONFIG_HOME/nl-shell/` (`~/.config/nl-shell/`) | user-writable |
   | Model weights and the local reference index | `$XDG_DATA_HOME/nl-shell/` (`~/.local/share/nl-shell/`) | user-writable |
   | Session log (JSONL) | `$XDG_STATE_HOME/nl-shell/sessions/` (`~/.local/state/nl-shell/`) | directory `0700`, files `0600` |
   | Model server socket | `$XDG_RUNTIME_DIR/nl-shell/model.sock` | `0600` (ADR-0018) |
   | Caches (dpkg ownership cache for the gate) | `$XDG_CACHE_HOME/nl-shell/` | — |

2. **Config format:** TOML for settings, YAML only for the prompts (multi-line text is
   easier to edit there). Defaults ship in the package; the user's files override them.
3. **The model endpoint defaults to the local socket.** Any other URL, including a
   loopback TCP port, is used only when the user passes `--dev-teacher` on the command
   line for that session. A URL in `config.toml` alone is refused with a message. This
   keeps a copied config from breaking ADR-0003.
4. **`:status` shows** which files were loaded, the endpoint in use, and the backend.
5. **Settings that change safety behaviour** are listed here with their defaults, so a
   reviewer can see every switch in one place:

   | Key | Default | Effect |
   |---|---|---|
   | `sudo.keep_credentials` | `false` | keep the sudo credential cache open after a privileged command (ADR-0015 point 6) |

## Consequences

- Tests use these paths under the temporary HOME (#73), so no test touches real config,
  logs or weights.
- Session logs are private to the user by default; secrets scrubbing (#20) still applies.
- Using a LAN teacher needs a flag every session. That is intended friction.
