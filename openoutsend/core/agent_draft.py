"""State for ``send --agent-draft``, carried from the CLI to the one place a call to
``AI_MODEL`` would otherwise fire.

OpenOutFind's mirror of this (``--agent-qualify``) threads state through a contextvar,
because its call sits four layers below the CLI (``job -> cycle -> top_up ->
qualify``). This one is three ordinary parameters away
(``_send -> run_send_pass -> _open_conversations``), so a plain dataclass passed
down says the same thing without module-level state.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentDraft:
    active: bool = False
    subject: str | None = None
    body: str | None = None

    @property
    def answered(self) -> bool:
        return bool(self.subject and self.body)


OFF = AgentDraft()
