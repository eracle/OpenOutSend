# openoutsend/core/sequences.py
"""**sequences** — a folder of step files, one per email, as the thing this sends.

A sequence is a directory holding one Jinja file per step. The filename carries
everything the engine reads::

    E<number>[-<days>d]-<name>.<kind>.j2

- ``E<number>`` is the order. Numbers compare as numbers, so ``E10`` follows ``E9``.
- ``<days>d`` is the gap in business days: ``E0`` counts from the deal's ``start_at``
  (the ingest time when the piped line has none), every later step from the previous
  step's send. No segment means ``0d``. The planner reads them (``planner.opening_step``,
  ``planner.steps_due``).
- ``<name>`` is for the person reading the folder.
- ``<kind>`` is what the file renders to, and so whether a model is called:
  ``prompt`` is instructions the model writes the email from, ``email`` is the email
  itself, sent as written. An email step that opens a thread starts with a
  ``Subject: …`` line and a blank line; one sent in the thread has none, because it goes
  under the opener's subject.

**The body is rendered by Jinja with ``StrictUndefined``.** ``{{ field }}`` is filled in
before the model reads the prompt or the email goes, and a field the deal does not carry
is an error for that deal, never an empty string. ``{{ prompt_line }}`` draws a move from
the prompt-line pool (``core/prompt_lines.py``), and only a prompt that names it gets one
drawn.

**A folder is checked when it loads, not when a step is due.** A bad name, two files
with one number, a gap in the numbering, a ``0d`` after ``E0``, a Jinja syntax error, a
subject line where one cannot go or a missing one where it must, or a step still named the
old way (``….md``) is a ``SequenceError`` naming the file — the same rule as a bad prompt
line: a typo is answered, never guessed at. Any other file is not a step and is left
alone, so a folder can carry its own notes and fixtures.

**Cold outreach is the built-in sequence**, shipped as ``openoutsend/sequences/cold/``.
An operator changes it the way they override a prompt line: a copy at
``state_dir()/sequences/cold/`` wins over the shipped folder, whole.

**A deal knows its sequence by the folder's name**, not where the folder sits (``key``).
A run finds the folder from ``--sequence``, or under ``state_dir()/sequences/``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import jinja2
from jinja2 import meta

from openoutsend.defaults import state_dir
from openoutsend.errors import OutsendError

SHIPPED_DIR = Path(__file__).resolve().parent.parent / "sequences"

COLD = "cold"

PROMPT = "prompt"
EMAIL = "email"

STEP_FILE = re.compile(
    r"^E(?P<number>\d+)(?:-(?P<days>\d+)d)?-(?P<name>.+)\.(?P<kind>prompt|email)\.j2$")

# A step under the naming before ``<kind>.j2``: refused by name rather than skipped, so a
# folder that was never renamed fails loudly instead of losing its steps.
OLD_STEP_FILE = re.compile(r"^E\d+(?:-\d+d)?-.+\.md$")

SUBJECT = re.compile(r"^Subject:[ \t]*(?P<subject>\S.*?)[ \t]*\n[ \t]*\n(?P<body>.*)$", re.DOTALL)

# Jinja comments a step may open with, which render to nothing and so precede its subject.
LEADING_COMMENTS = re.compile(r"^\s*(?:\{#.*?#\}\s*)*", re.DOTALL)

_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)

# The folders this process was handed with ``--sequence``, by the name a deal records.
_FOLDERS: dict[str, Path] = {}


class SequenceError(OutsendError):
    """A sequence folder is malformed, or a step cannot be rendered for a deal.

    An ``OutsendError``, because a typo in a folder the operator named is an answer
    for them, not a traceback.
    """


@dataclass(frozen=True)
class Step:
    """One email of a sequence: where it sits, when it is due, and what it renders to."""

    number: int
    days: int
    name: str
    path: Path
    source: str
    kind: str = PROMPT

    @property
    def is_email(self) -> bool:
        """Whether the file is the email itself, sent as written with no model call."""
        return self.kind == EMAIL

    @property
    def uses_prompt_line(self) -> bool:
        """Whether a prompt names ``{{ prompt_line }}``, so the send draws one."""
        return (not self.is_email
                and "prompt_line" in meta.find_undeclared_variables(_ENV.parse(self.source)))

    def render(self, **fields) -> str:
        """The file with *fields* filled in: the prompt the model reads, or the email.

        A field the body names and *fields* lacks raises ``SequenceError`` naming both,
        because a step sent with a blank where the reader's name should be is worse
        than a step not sent.
        """
        try:
            return _ENV.from_string(self.source).render(**fields).strip()
        except jinja2.UndefinedError as exc:
            raise SequenceError(f"{self.path}: {exc.message}") from exc

    def written(self, **fields) -> tuple[str, str]:
        """An email step's ``(subject, body)``, rendered; the subject is ``""`` in a thread."""
        return _split_subject(self.render(**fields))

    def opening(self, **fields) -> tuple[str, str]:
        """``written``, for a step that opens the thread, so it must carry a subject.

        The loader refuses an ``E0`` without one; this catches a later step that opens
        the thread because ``E0`` was past its grace when the lead arrived.
        """
        subject, body = self.written(**fields)
        if not subject:
            raise SequenceError(f"{self.path}: opens a thread, so it needs a Subject: line")
        return subject, body


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

    for path in directory.iterdir():
        if OLD_STEP_FILE.match(path.name):
            raise SequenceError(f"{path}: rename it {path.stem}.prompt.j2 — a step file is "
                                f"<name>.prompt.j2 (the model writes from it) or "
                                f"<name>.email.j2 (sent as written)")
    steps = sorted(
        (_load_step(path) for path in directory.glob("*.j2")),
        key=lambda step: step.number,
    )
    if not steps:
        raise SequenceError(f"{directory}: no step files (expected E0-<name>.prompt.j2 "
                            f"or E0-<name>.email.j2)")
    _check_numbering(steps)
    return Sequence(name=name, steps=tuple(steps))


def cold() -> Sequence:
    """The built-in cold sequence — the operator's copy if there is one."""
    override = operator_dir() / COLD
    return load(override if override.is_dir() else SHIPPED_DIR / COLD, COLD)


def key(folder: str | None) -> str:
    """The name a deal records for the sequence in *folder* — ``cold`` when none is given.

    The folder's own name, never its path: a path is where this run found the folder,
    and the same sequence run from a moved checkout or a rebuilt image would otherwise
    be a new sequence, enrolling every lead in it again. The folder is remembered for
    this process (``for_deal``), and loaded here, so a bad one fails the command that
    named it, before a line is ingested or a mail sent.
    """
    if folder is None:
        return COLD
    path = Path(folder).expanduser().resolve()
    name = path.name
    if name == COLD:
        raise SequenceError(f"{path}: a sequence folder cannot be named {COLD!r}, "
                            f"the built-in sequence's name — copy it to "
                            f"{operator_dir() / COLD} to change the built-in one")
    known = _FOLDERS.get(name)
    if known is not None and known != path:
        raise SequenceError(f"{path}: this run already named {known}, and a deal knows "
                            f"its sequence by the folder's name, {name!r}")
    load(path, name)
    _FOLDERS[name] = path
    return name


def for_deal(deal) -> Sequence:
    """The sequence *deal* is enrolled in, found by its name.

    The folder this run named with ``--sequence`` first, then the operator's own
    folder of that name (``operator_dir``), so a pass over every sequence finds the
    ones kept there without being told.
    """
    name = deal.sequence
    if name == COLD:
        return cold()
    folder = _FOLDERS.get(name, operator_dir() / name)
    if not folder.is_dir():
        raise SequenceError(f"sequence {name!r}: no folder of that name in {operator_dir()} "
                            f"— name it with --sequence")
    return load(folder, name)


def operator_dir() -> Path:
    """Where an operator's own copies live. Not created — absent simply means none."""
    return state_dir() / "sequences"


def _load_step(path: Path) -> Step:
    """Parse one filename and check its body compiles, or say which file is wrong."""
    match = STEP_FILE.match(path.name)
    if match is None:
        raise SequenceError(f"{path}: not a step file name "
                            f"(expected E<number>[-<days>d]-<name>.prompt.j2 or .email.j2)")

    try:
        source = path.read_text(encoding="utf-8")
        _ENV.parse(source)
    except OSError as exc:
        raise SequenceError(f"{path}: {exc}") from exc
    except jinja2.TemplateSyntaxError as exc:
        raise SequenceError(f"{path}:{exc.lineno}: {exc.message}") from exc

    step = Step(
        number=int(match["number"]),
        days=int(match["days"] or 0),
        name=match["name"],
        path=path,
        source=source,
        kind=match["kind"],
    )
    if step.is_email:
        _check_subject(step)
    return step


def _check_subject(step: Step) -> None:
    """``E0`` opens the thread, so it needs a subject; a later step goes under it, so none."""
    has_subject = bool(SUBJECT.match(LEADING_COMMENTS.sub("", step.source)))
    if step.number == 0 and not has_subject:
        raise SequenceError(f"{step.path}: an opening email starts with a Subject: line, "
                            f"then a blank line")
    if step.number and has_subject:
        raise SequenceError(f"{step.path}: a later step goes under the opener's subject — "
                            f"drop its Subject: line")


def _split_subject(text: str) -> tuple[str, str]:
    """``(subject, body)`` of a rendered email; ``("", text)`` when it has no subject line."""
    match = SUBJECT.match(text)
    if match is None:
        return "", text
    return match["subject"], match["body"].strip()


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
