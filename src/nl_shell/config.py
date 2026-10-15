"""The user's ``config.toml`` (ADR-0020 §Decision items 1 and 2).

It lives at ``$XDG_CONFIG_HOME/nl-shell/config.toml``. Per the XDG Base Directory spec, an
``XDG_CONFIG_HOME`` that is unset, empty or not an absolute path is ignored in favour of
``~/.config``. A missing file means "no settings". Keys this version does not know are
ignored; a known key with the wrong type, or a file that is not valid TOML, is an error
that names the file.

Today the only key is ``target`` (ADR-0023 "Choosing the target"). Later issues (#77)
add their keys to ``Config`` and ``load``.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "config.toml"


class ConfigError(Exception):
    """config.toml could not be used; the message names the file and the problem."""


@dataclass(frozen=True)
class Config:
    target: str | None = None


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """Where config.toml is read from, given the environment (``os.environ`` by default)."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_CONFIG_HOME", "")
    if xdg and os.path.isabs(xdg):
        base = Path(xdg)
    else:
        home = env.get("HOME") or str(Path.home())
        base = Path(home) / ".config"
    return base / "nl-shell" / CONFIG_NAME


def load(path: Path | None = None) -> Config:
    """The settings in ``path`` (``config_path()`` by default); defaults if missing."""
    path = config_path() if path is None else path
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror or exc}") from None
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not valid TOML: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from None
    target = doc.get("target")
    if target is not None and not isinstance(target, str):
        raise ConfigError(f"{path}: `target` must be a string")
    return Config(target=target)
