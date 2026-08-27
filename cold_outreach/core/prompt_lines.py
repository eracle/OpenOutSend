# cold_outreach/core/prompt_lines.py
"""**prompt lines** — the part of the opener's prompt that varies, as files on disk.

A prompt line is exactly what its name says: a piece of prompt. It drops into the first
email's system prompt and describes the move that email makes. Everything around it —
who the operator is, the product, the Mom Test discipline, and every hard rule in
``agents/outreach.py`` — is identical whichever line is chosen, so what differs between
two sends is the move and nothing else. That is what makes comparing them later
meaningful rather than merely possible.

**Two directories, and the operator's wins.** Shipped lines live beside this package;
an operator's live under ``state_dir()/prompt_lines``. An id defined in both resolves to
theirs, so a shipped line can be overridden without editing an installed package and
without losing the edit on the next upgrade.

**Selection is random, and that is the honest state of it.** There is no scoring here
and none is faked — a uniform draw, recorded on the message that goes out. The recording
is the part worth doing today: choose at random and write down what was chosen, and
comparing them becomes a query over the log rather than an experiment somebody had to
design in advance. Retrofit it later and every send in between is lost.

**Identity is the id *and* the text.** ``digest`` hashes the prompt, so an edited line
does not silently pool with the version before it. Editing a file is free; discovering
afterwards that two different messages share one name is not, because the old rows
cannot be re-attributed.
"""
from __future__ import annotations

import hashlib
import logging
import random
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cold_outreach.settings import state_dir

logger = logging.getLogger(__name__)

SHIPPED_DIR = Path(__file__).resolve().parent.parent / "prompt_lines"

REQUIRED_KEYS = ("id", "when", "prompt")


class PromptLineError(Exception):
    """A prompt-line file is unreadable, or a named line does not exist."""


@dataclass(frozen=True)
class PromptLine:
    """One move: an id, a note on which leads it suits, and the prompt itself."""

    id: str
    when: str
    prompt: str
    source: Path

    @property
    def digest(self) -> str:
        """A short content hash of the prompt — the version half of the identity."""
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:12]


def operator_dir() -> Path:
    """Where an operator's own lines live. Not created — absent simply means none."""
    return state_dir() / "prompt_lines"


def load_all() -> dict[str, PromptLine]:
    """Every prompt line by id, the operator's copy winning over a shipped one."""
    lines: dict[str, PromptLine] = {}
    for directory in (SHIPPED_DIR, operator_dir()):
        for path in sorted(directory.glob("*.toml")):
            line = _load(path)
            if line.id in lines:
                logger.debug("prompt line %s from %s overrides %s",
                             line.id, path, lines[line.id].source)
            lines[line.id] = line
    return lines


def choose(name: str | None = None, *, rng: random.Random | None = None) -> PromptLine | None:
    """The line to open with: *name* if given, otherwise one at random.

    Returns ``None`` when there are none at all, which is not an error — the opener has
    a complete prompt without one, and an install whose directory was emptied should
    still be able to send.

    Naming one that does not exist **is** an error, and it lists what there is: the
    house rule everywhere else is that a typo is answered, never guessed at.
    """
    lines = load_all()
    if name:
        try:
            return lines[name]
        except KeyError:
            known = ", ".join(sorted(lines)) or "none installed"
            raise PromptLineError(f"no prompt line named '{name}' — have: {known}") from None
    if not lines:
        logger.warning("no prompt lines in %s or %s — opening without one",
                       SHIPPED_DIR, operator_dir())
        return None
    return (rng or random).choice([lines[key] for key in sorted(lines)])


def _load(path: Path) -> PromptLine:
    """Parse one file, or say which file is wrong and what it is missing."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PromptLineError(f"{path}: {exc}") from exc

    missing = [key for key in REQUIRED_KEYS if not str(raw.get(key, "")).strip()]
    if missing:
        raise PromptLineError(f"{path}: missing or empty {', '.join(missing)}")

    return PromptLine(
        id=str(raw["id"]).strip(),
        when=str(raw["when"]).strip(),
        prompt=str(raw["prompt"]).strip(),
        source=path,
    )
