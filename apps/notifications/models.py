"""Who has asked not to be written to.

Only one kind of email SignaCore sends is not transactional: the note that follows a signature,
which goes to somebody who never signed up for anything and was only ever a recipient. That one
needs a way out, and the way out has to work without an account, because the person reading it
does not have one.

Addresses are stored as the same digest used everywhere else, never in the clear. A suppression
list of plaintext addresses would be a list of everybody who has ever signed anything, which is
precisely the thing worth not keeping.
"""

from __future__ import annotations

from django.db import models

from utils.identity import email_digest


class EmailSuppression(models.Model):
    class ReasonEnum(models.TextChoices):
        UNSUBSCRIBED = "UNSUBSCRIBED", "Unsubscribed"
        BOUNCED = "BOUNCED", "Bounced"
        COMPLAINED = "COMPLAINED", "Marked as spam"

    email_hash = models.CharField(max_length=64, unique=True, db_index=True)
    reason = models.CharField(max_length=32, choices=ReasonEnum.choices, default=ReasonEnum.UNSUBSCRIBED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.reason} {self.email_hash[:12]}"

    @classmethod
    def is_suppressed(cls, email: str) -> bool:
        if not email:
            return True
        return cls.objects.filter(email_hash=email_digest(email)).exists()

    @classmethod
    def suppress(cls, email: str, *, reason: str = ReasonEnum.UNSUBSCRIBED) -> None:
        if not email:
            return
        cls.objects.get_or_create(email_hash=email_digest(email), defaults={"reason": reason})
