import logging

from celery import shared_task

from apps.documents.models import Document
from apps.notifications.services import (
    send_account_login_email,
    send_account_verification_email,
    send_account_welcome_email,
    send_admin_account_created_email,
    send_admin_password_changed_email,
    send_completion_email,
    send_invitation_email,
)
from apps.notifications.services import send_organization_invitation as send_organization_invitation_email
from apps.notifications.services import (
    send_otp_email_message,
    send_password_reset_email,
    send_progress_email,
    send_signed_copy_email,
    send_subscription_activated_email,
)
from apps.signing.models import SigningRequest

logger = logging.getLogger(__name__)


@shared_task(name="tasks.notifications.send_invitation_emails")
def send_invitation_emails(document_id: str) -> None:
    document = (
        Document.objects.select_related("created_by", "created_by__signacore_profile")
        .prefetch_related("signing_requests")
        .filter(pk=document_id)
        .first()
    )
    if document is None:
        return None

    for signing_request in document.signing_requests.all():
        send_invitation_email(signing_request)
    return None


@shared_task(name="tasks.notifications.send_invitation_email_for_request")
def send_invitation_email_for_request(signing_request_id: str) -> None:
    signing_request = SigningRequest.objects.select_related("document").filter(pk=signing_request_id).first()
    if signing_request is None:
        return None

    send_invitation_email(signing_request)
    return None


@shared_task(name="tasks.notifications.send_organization_invitation")
def send_organization_invitation(
    email: str,
    organization_name: str,
    inviter_name: str,
    role: str,
    token: str,
) -> None:
    send_organization_invitation_email(email, organization_name, inviter_name, role, token)
    return None


@shared_task(name="tasks.notifications.send_completion_emails")
def send_completion_emails(document_id: str) -> None:
    document = (
        Document.objects.select_related("created_by", "created_by__signacore_profile")
        .prefetch_related("signing_requests")
        .filter(pk=document_id)
        .first()
    )
    if document is None:
        return None

    send_completion_email(document)
    return None


@shared_task(name="tasks.notifications.send_signed_copy")
def send_signed_copy(signing_request_id: str) -> None:
    signing_request = SigningRequest.objects.select_related("document").filter(pk=signing_request_id).first()
    if signing_request is None:
        return None

    send_signed_copy_email(signing_request)
    return None


@shared_task(name="tasks.notifications.send_otp_email")
def send_otp_email(signing_request_id: str, otp_code: str) -> None:
    signing_request = SigningRequest.objects.select_related("document").filter(pk=signing_request_id).first()
    if signing_request is None:
        return None

    send_otp_email_message(signing_request, otp_code)
    return None


@shared_task(name="tasks.notifications.notify_admin_progress")
def notify_admin_progress(document_id: str, signing_request_id: str) -> None:
    document = (
        Document.objects.select_related("created_by", "created_by__signacore_profile").filter(pk=document_id).first()
    )
    signing_request = SigningRequest.objects.select_related("document").filter(pk=signing_request_id).first()
    if document is None or signing_request is None:
        return None

    send_progress_email(document, signing_request)
    return None


@shared_task(name="tasks.notifications.send_admin_account_created")
def send_admin_account_created(email: str, username: str, temporary_password: str, login_url: str) -> None:
    send_admin_account_created_email(email, username, temporary_password, login_url)
    return None


@shared_task(name="tasks.notifications.send_admin_password_changed")
def send_admin_password_changed(email: str, username: str, temporary_password: str, login_url: str) -> None:
    send_admin_password_changed_email(email, username, temporary_password, login_url)
    return None


@shared_task(name="tasks.notifications.send_account_verification")
def send_account_verification(user_id: int) -> None:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.select_related("signacore_profile").filter(pk=user_id, is_active=False).first()
    if user is None:
        return None

    send_account_verification_email(user)
    return None


@shared_task(name="tasks.notifications.send_account_welcome")
def send_account_welcome(user_id: int) -> None:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.select_related("signacore_profile").filter(pk=user_id, is_active=True).first()
    if user is None:
        return None

    send_account_welcome_email(user)
    return None


@shared_task(name="tasks.notifications.send_account_login_alert")
def send_account_login_alert(user_id: int, method: str) -> None:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.select_related("signacore_profile").filter(pk=user_id, is_active=True).first()
    if user is None:
        return None

    send_account_login_email(user, method)
    return None


@shared_task(name="tasks.notifications.send_subscription_activated_email")
def send_subscription_activated(organization_id: str) -> None:
    send_subscription_activated_email(organization_id)
    return None


@shared_task(name="tasks.notifications.sync_organization_seat_quantity")
def sync_organization_seat_quantity(organization_id: str) -> None:
    """Bill a Business workspace for the people in it after the team changes.

    Seats are set once at checkout and would otherwise stay at whatever the workspace looked
    like that day, so a team that grows keeps paying for the size it signed up at.
    """
    from django.conf import settings

    from apps.accounts.models import Organization
    from apps.billing.models import OrganizationSubscription
    from apps.billing.seats import get_organization_seat_count
    from services.stripe_client import StripeAPIClient

    organization = Organization.objects.filter(pk=organization_id).first()
    subscription = OrganizationSubscription.objects.filter(organization=organization).first() if organization else None
    if not subscription or not subscription.stripe_subscription_id or not settings.STRIPE_SECRET_KEY:
        return None

    quantity = get_organization_seat_count(organization, subscription.plan)
    if subscription.stripe_subscription_quantity == quantity and subscription.stripe_subscription_item_id:
        return None

    client = StripeAPIClient()
    item_id = subscription.stripe_subscription_item_id
    if not item_id:
        # Subscriptions opened before the id was stored, and any whose webhook was missed. Ask
        # Stripe once and keep the answer rather than fetching it on every team change.
        remote = client.retrieve_subscription(subscription.stripe_subscription_id)
        items = remote.get("items") if isinstance(remote.get("items"), dict) else {}
        item_data = items.get("data") if isinstance(items.get("data"), list) else []
        first_item = item_data[0] if item_data and isinstance(item_data[0], dict) else {}
        item_id = str(first_item.get("id") or "")
        if not item_id:
            logger.warning(
                "Stripe subscription has no item to bill seats against",
                extra={
                    "organization_id": organization_id,
                    "stripe_subscription_id": subscription.stripe_subscription_id,
                },
            )
            return None
        subscription.stripe_subscription_item_id = item_id
        if isinstance(first_item.get("quantity"), int):
            subscription.stripe_subscription_quantity = first_item["quantity"]
        subscription.save(update_fields=["stripe_subscription_item_id", "stripe_subscription_quantity", "updated_at"])
        if subscription.stripe_subscription_quantity == quantity:
            return None

    updated = client.update_subscription_item_quantity(item_id, quantity)
    # Record what Stripe says it is now billing rather than what we asked for, so a request that
    # was adjusted at the other end does not leave us believing something untrue.
    subscription.stripe_subscription_quantity = (
        updated.get("quantity") if isinstance(updated.get("quantity"), int) else quantity
    )
    subscription.save(update_fields=["stripe_subscription_quantity", "updated_at"])
    return None


@shared_task(name="tasks.notifications.send_password_reset")
def send_password_reset(user_id: int) -> None:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.select_related("signacore_profile").filter(pk=user_id).first()
    if user is None:
        return None

    send_password_reset_email(user, has_password=user.has_usable_password())
    return None
