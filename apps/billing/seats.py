"""How many seats a workspace is billed for.

One definition, used by both the checkout that opens a subscription and the task that keeps it
up to date. They were separate before, and had already drifted: one read the membership status
through its enum and the other through a bare string. A disagreement between them is a customer
being charged for a different number of people than the page told them about.
"""

from __future__ import annotations

from apps.accounts.models import Organization, OrganizationMembership
from apps.billing.models import OrganizationSubscription


def get_organization_seat_count(organization: Organization, plan: str) -> int:
    """Seats to bill for `organization` on `plan`.

    Only the Business plan is sold per member. Everything else is one workspace, one seat,
    whatever the team looks like.
    """
    if plan != OrganizationSubscription.PlanEnum.BUSINESS:
        return 1
    return max(
        organization.memberships.filter(
            status=OrganizationMembership.StatusEnum.ACTIVE,
            user__is_active=True,
        ).count(),
        1,
    )
