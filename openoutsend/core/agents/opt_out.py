# openoutsend/core/agents/opt_out.py
"""Did this reply ask us to stop? One narrow question, asked when no agent answers replies.

With ``OUTSEND_REPLIES=agent`` the outreach agent reads every reply and can answer
``suppress`` (``emails/steps/reply.py``). With ``person`` nothing automated answers, but
a worded unsubscribe — "please take me off your list" — must still be honoured before
the next step goes. The plus-address and the ``List-Unsubscribe`` header are caught by
the mail pass without a model; a sentence in a reply can only be read.

So this asks one thing of the reply's text alone, and nothing about how to answer it.
Declining an offer is not asking to stop: "not interested" keeps the sequence going,
because the person answering the thread decides what to do with it.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from openoutsend.core.llm import get_llm_model, run_agent_sync

logger = logging.getLogger(__name__)

OPT_OUT_PROMPT = """\
You read one email reply to a message we sent. Answer one question: does the writer ask \
us to stop contacting them?

Yes for "stop", "unsubscribe", "take me off your list", "remove me", "do not email me \
again", in any language or wording. No for declining an offer while saying nothing about \
further email ("not interested, thanks"), for questions, for out-of-office notes and for \
anything else."""


class OptOutVerdict(BaseModel):
    """The one answer the model gives."""

    asks_to_stop: bool = Field(description="True only when the writer asks not to be contacted again.")


def asks_to_stop(reply_text: str) -> bool:
    """Whether *reply_text* asks us to stop emailing its writer."""
    agent = Agent(
        get_llm_model(),
        system_prompt=OPT_OUT_PROMPT,
        output_type=OptOutVerdict,
        model_settings={"temperature": 0, "timeout": 60},
    )
    verdict = run_agent_sync(agent.run(reply_text)).output
    if verdict is None:
        raise RuntimeError("LLM returned an unparseable opt-out verdict")
    return verdict.asks_to_stop
