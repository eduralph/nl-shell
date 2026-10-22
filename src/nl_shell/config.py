"""The user's ``config.toml`` (ADR-0020 §Decision items 1 and 2).

It lives at ``$XDG_CONFIG_HOME/nl-shell/config.toml``. Per the XDG Base Directory spec, an
``XDG_CONFIG_HOME`` that is unset, empty or not an absolute path is ignored in favour of
``~/.config``. A missing file means "no settings". Keys this version does not know are
ignored; a known key with the wrong type, or a file that is not valid TOML, is an error
that names the file.

The keys are ``target`` (ADR-0023 "Choosing the target"), ``teacher_url`` and
``teacher_model`` (the dev-teacher endpoint, used only with ``--dev-teacher``:
ADR-0020 §3), and ``[model] timeout_s``, the model request deadline in seconds
(ADR-0018 §7, ADR-0020 §5; default ``DEFAULT_MODEL_TIMEOUT_S``, at most
``MAX_MODEL_TIMEOUT_S``).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "config.toml"
DEFAULT_MODEL_TIMEOUT_S = 10.0
# The longest `[model] timeout_s` accepted: one hour. A longer deadline is a typo, not a
# setting, and would leave the REPL waiting on a stuck server for as long.
MAX_MODEL_TIMEOUT_S = 3600.0


class ConfigError(Exception):
    """config.toml could not be used; the message names the file and the problem."""


@dataclass(frozen=True)
class Config:
    target: str | None = None
    teacher_url: str | None = None
    teacher_model: str | None = None
    model_timeout_s: float = DEFAULT_MODEL_TIMEOUT_S


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
    target = _string(doc, "target", path)
    teacher_url = _string(doc, "teacher_url", path)
    teacher_model = _string(doc, "teacher_model", path)
    model = doc.get("model", {})
    if not isinstance(model, dict):
        raise ConfigError(f"{path}: `model` must be a table")
    timeout_s = _timeout(model.get("timeout_s", DEFAULT_MODEL_TIMEOUT_S), path)
    return Config(
        target=target,
        teacher_url=teacher_url,
        teacher_model=teacher_model,
        model_timeout_s=timeout_s,
    )


def _string(doc: Mapping[str, object], key: str, path: Path) -> str | None:
    value = doc.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"{path}: `{key}` must be a string")
    return value


def _timeout(value: object, path: Path) -> float:
    """``[model] timeout_s`` as a float in (0, MAX_MODEL_TIMEOUT_S]."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path}: `model.timeout_s` must be a number of seconds")
    # Compare before converting: TOML allows integers too large for a float.
    if not (0 < value <= MAX_MODEL_TIMEOUT_S):  # NaN fails this too
        raise ConfigError(
            f"{path}: `model.timeout_s` must be more than 0 and at most "
            f"{MAX_MODEL_TIMEOUT_S:g} seconds"
        )
    return float(value)
