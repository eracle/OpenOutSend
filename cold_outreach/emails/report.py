# cold_outreach/emails/report.py
"""What the receivers said, counted off the log.

*What is my bounce rate?* was unanswerable while delivery was recorded only inside
an exception path — a hard bounce is not an exception, so the numerator did not
exist. It is now a count over `DeliveryEvent`, read back by `warmth.py`: a box
bouncing above tolerance has its capacity halved.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from cold_outreach.emails.models import DeliveryEvent

# The window a rate is quoted over. A month: long enough that one bad afternoon
# does not dominate, short enough that a repaired domain is not judged by its worst
# week forever.
RATE_WINDOW_DAYS = 30


def bounce_rate(mailbox=None, *, days: int = RATE_WINDOW_DAYS) -> float:
    """Bounces per accepted send over the trailing window; 0.0 with nothing sent.

    Accepted sends are the denominator rather than *attempted* ones: a send the
    receiver never took responsibility for cannot bounce, and counting it would
    flatter a box whose failures are all at the front door.
    """
    events = DeliveryEvent.objects.filter(
        occurred_at__gte=timezone.now() - timedelta(days=days))
    if mailbox is not None:
        events = events.filter(message__mailbox=mailbox)

    accepted = events.filter(status=DeliveryEvent.Status.ACCEPTED).count()
    if not accepted:
        return 0.0
    bounced = events.filter(status=DeliveryEvent.Status.BOUNCED).count()
    return bounced / accepted
