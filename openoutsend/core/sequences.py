# openoutsend/core/sequences.py
"""**sequences** — a folder of prompt files, one per email, as the thing this sends.

A sequence is a directory holding one Markdown file per step. The filename carries
everything the engine reads, and the body is the prompt and nothing else::

    E<number>[-<days>d]-<name>.md

- ``E<number>`` is the order. Numbers compare as numbers, so ``E10`` follows ``E9``.
- ``<days>d`` is the gap in business days: ``E0`` counts from the deal's ``start_at``
  (the ingest time when the piped line has none), every later step from the previous
  step's send. No segment means ``0d``. The planner reads them (``planner.opening_step``,
  ``planner.steps_due``).
- ``<name>`` is for the person reading the folder.

**The body is rendered by Jinja with ``StrictUndefined``.** ``{{ field }}`` is filled in
before the model reads the prompt, and a field the deal does not carry is an error for
that deal, never an empty string. ``{{ prompt_line }}`` draws a move from the prompt-line
pool (``core/prompt_lines.py``), and only a step that names it gets one drawn.

**A folder is checked when it loads, not when a step is due.** A bad name, two files
with one number, a gap in the numbering, a ``0d`` after ``E0`` or a Jinja syntax error
is a ``SequenceError`` naming the file — the same rule as a bad prompt line: a typo is
answered, never guessed at. A ``README.md`` and anything not ending in ``.md`` are not
steps and are left alone, so a folder can carry its own notes and fixtures.

**Cold outreach is the built-in sequence**, shipped as ``openoutsend/sequences/cold/``.
An operator changes it the way they override a prompt line: a copy at
``state_dir()/sequences/cold/`` wins over the shipped folder, whole.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import jinja2
from jinja2 import meta

from openoutsend.defaults import state_dir

SHIPPED_DIR = Path(__file__).resolve().parent.parent / "sequences"

COLD = "cold"

STEP_FILE = re.compile(r"^E(?P<number>\d+)(?:-(?P<days>\d+)d)?-(?P<name>.+)\.md$")

# Not a step: the folder's own notes. Every other ``.md`` must be one.
NOT_A_STEP = {"README.md"}

_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)


class SequenceError(Exception):
    """A sequence folder is malformed, or a step cannot be rendered for a deal."""


@dataclass(frozen=True)
class Step:
    """One email of a sequence: where it sits, when it is due, and its prompt."""

    number: int
    days: int
    name: str
    path: Path
    source: str

    @property
    def uses_prompt_line(self) -> bool:
        """Whether the body names ``{{ prompt_line }}``, so the send draws one."""
        return "prompt_line" in meta.find_undeclared_variables(_ENV.parse(self.source))

    def render(self, **fields) -> str:
        """The prompt as the model will read it, with *fields* filled in.

        A field the body names and *fields* lacks raises ``SequenceError`` naming both,
        because a step sent with a blank where the reader's name should be is worse
        than a step not sent.
        """
        try:
            return _ENV.from_string(self.source).render(**fields).strip()
        except jinja2.UndefinedError as exc:
            raise SequenceError(f"{self.path}: {exc.message}") from exc


@dataclass(frozen=True)
class Sequence:
    """The steps of one sequence, in order, under the name a deal records."""

    name: str
    steps: tuple[Step, ...]

    @property
    def first(self) -> Step:
        return self.steps[0]

    def step(self, number: int) -> Step:
        """The step numbered *number*. A deal never records one its sequence lacks."""
        return self.steps[number]

    def after(self, number: int) -> Step | None:
        """The step following *number*, or ``None`` after the last one."""
        following = number + 1
        return self.steps[following] if following < len(self.steps) else None


# ── Loading ───────────────────────────────────────────────────────


def load(directory: Path, name: str) -> Sequence:
    """Read and check every step file in *directory*. Raises ``SequenceError``."""
    if not directory.is_dir():
        raise SequenceError(f"{directory}: no such sequence folder")

    steps = sorted(
        (_load_step(path) for path in directory.glob("*.md") if path.name not in NOT_A_STEP),
        key=lambda step: step.number,
    )
    if not steps:
        raise SequenceError(f"{directory}: no step files (expected E0-<name>.md)")
    _check_numbering(steps)
    return Sequence(name=name, steps=tuple(steps))


def cold() -> Sequence:
    """The built-in cold sequence — the operator's copy if there is one."""
    override = operator_dir() / COLD
    return load(override if override.is_dir() else SHIPPED_DIR / COLD, COLD)


def for_deal(deal) -> Sequence:
    """The sequence *deal* is enrolled in."""
    if deal.sequence == COLD:
        return cold()
    return load(Path(deal.sequence), deal.sequence)


def operator_dir() -> Path:
    """Where an operator's own copies live. Not created — absent simply means none."""
    return state_dir() / "sequences"


def _load_step(path: Path) -> Step:
    """Parse one filename and check its body compiles, or say which file is wrong."""
    match = STEP_FILE.match(path.name)
    if match is None:
        raise SequenceError(f"{path}: not a step file name (expected E<number>[-<days>d]-<name>.md)")

    try:
        source = path.read_text(encoding="utf-8")
        _ENV.parse(source)
    except OSError as exc:
        raise SequenceError(f"{path}: {exc}") from exc
    except jinja2.TemplateSyntaxError as exc:
        raise SequenceError(f"{path}:{exc.lineno}: {exc.message}") from exc

    return Step(
        number=int(match["number"]),
        days=int(match["days"] or 0),
        name=match["name"],
        path=path,
        source=source,
    )


def _check_numbering(steps: list[Step]) -> None:
    """``E0``, ``E1``, … with no gap and no repeat, and a delay on every step after ``E0``."""
    for expected, step in enumerate(steps):
        if step.number < expected:
            raise SequenceError(f"{step.path}: a second E{step.number}")
        if step.number > expected:
            raise SequenceError(f"{step.path}: E{expected} is missing before it")
        if step.number and not step.days:
            raise SequenceError(
                f"{step.path}: a step after E0 needs a delay of at least 1d (E{step.number}-<days>d-…)")
