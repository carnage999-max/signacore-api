from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.html import escape
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.documents.models import Document
from apps.signing.models import SigningRequest


def send_email(
    subject: str,
    body: str,
    recipients: list[str],
    attachments: list[tuple[str, bytes, str]] | None = None,
    html_body: str | None = None,
) -> None:
    if not recipients:
        return

    message = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
    )
    if html_body:
        message.attach_alternative(html_body, "text/html")
    for attachment in attachments or []:
        message.attach(*attachment)
    message.send(fail_silently=False)


def build_signing_link(signing_request: SigningRequest) -> str:
    base_url = settings.SIGNACORE_SIGNER_PORTAL_URL.rstrip("/")
    return f"{base_url}/sign/{signing_request.id}/"


def build_invitation_html(signing_request: SigningRequest, signing_link: str) -> str:
    signer_name = escape(signing_request.signer_name or "there")
    document_title = escape(signing_request.document.title)
    expires_at = escape(f"{signing_request.expires_at:%B %d, %Y at %I:%M %p %Z}")
    safe_link = escape(signing_link)

    return f"""\
<!doctype html>
<html lang="en">
  <body style="margin:0;background:#f4f0e8;color:#112235;font-family:Avenir Next,Segoe UI,Arial,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f0e8;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;overflow:hidden;border:1px solid #d8d0c2;border-radius:28px;background:#fffdf8;box-shadow:0 24px 70px rgba(17,34,53,0.14);">
            <tr>
              <td style="padding:34px 34px 26px;background:linear-gradient(135deg,#123a5f,#10283d 62%,#271d1a);color:#fffdf8;">
                <div style="font-size:12px;font-weight:900;letter-spacing:0.16em;text-transform:uppercase;color:#d9a94f;">SignaCore</div>
                <h1 style="margin:16px 0 0;font-family:Georgia,serif;font-size:34px;line-height:1.02;color:#fffdf8;">Signature requested</h1>
                <p style="margin:14px 0 0;color:rgba(255,253,248,0.74);font-size:15px;line-height:1.6;">Se7en Inc. sent you a document to review and sign securely.</p>
              </td>
            </tr>
            <tr>
              <td style="padding:34px;">
                <p style="margin:0 0 18px;font-size:16px;line-height:1.6;">Hello {signer_name},</p>
                <p style="margin:0 0 24px;font-size:16px;line-height:1.6;">Please review and complete the document below. You will verify your email before signing.</p>
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 28px;border:1px solid #e5ddcf;border-radius:18px;background:#fbf8f1;">
                  <tr>
                    <td style="padding:18px 20px;">
                      <div style="font-size:12px;font-weight:900;letter-spacing:0.12em;text-transform:uppercase;color:#6b7280;">Document</div>
                      <div style="margin-top:6px;font-size:20px;font-weight:800;color:#112235;">{document_title}</div>
                      <div style="margin-top:12px;font-size:13px;font-weight:700;color:#5d6d80;">Expires {expires_at}</div>
                    </td>
                  </tr>
                </table>
                <a href="{safe_link}" style="display:inline-block;border-radius:999px;background:#123a5f;color:#fffdf8;font-size:15px;font-weight:900;text-decoration:none;padding:15px 22px;">Review and sign</a>
                <p style="margin:28px 0 0;font-size:13px;line-height:1.6;color:#5d6d80;">If the button does not work, copy this link into your browser:</p>
                <p style="margin:8px 0 0;font-size:13px;line-height:1.6;word-break:break-all;color:#123a5f;">{safe_link}</p>
              </td>
            </tr>
            <tr>
              <td style="padding:18px 34px 28px;color:#7a8493;font-size:12px;line-height:1.6;">
                If you were not expecting this request, ignore this email.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def send_invitation_email(signing_request: SigningRequest) -> None:
    signer_name = signing_request.signer_name or "there"
    subject = f"Signature requested: {signing_request.document.title}"
    signing_link = build_signing_link(signing_request)
    body = (
        f"Hello {signer_name},\n\n"
        "Se7en Inc. sent you a document to review and sign in Signacore.\n\n"
        f"Document: {signing_request.document.title}\n"
        f"Signing link: {signing_link}\n"
        f"Link expires: {signing_request.expires_at:%Y-%m-%d %H:%M %Z}\n\n"
        "If you were not expecting this request, ignore this email.\n"
    )
    send_email(
        subject,
        body,
        [signing_request.signer_email],
        html_body=build_invitation_html(signing_request, signing_link),
    )


def send_otp_email_message(signing_request: SigningRequest, otp_code: str) -> None:
    signer_name = signing_request.signer_name or "there"
    subject = f"Your Signacore verification code for {signing_request.document.title}"
    body = (
        f"Hello {signer_name},\n\n"
        "Use the code below to continue signing your document in Signacore.\n\n"
        f"Verification code: {otp_code}\n"
        f"Expires in: {settings.OTP_EXPIRY_MINUTES} minutes\n\n"
        "If you did not request this code, ignore this email.\n"
    )
    send_email(subject, body, [signing_request.signer_email])


def send_completion_email(document: Document) -> None:
    if not document.signed_pdf:
        return

    recipients = list(
        dict.fromkeys(
            [
                *(request.signer_email for request in document.signing_requests.all()),
                getattr(document.created_by, "email", "") or "",
            ]
        )
    )
    recipients = [recipient for recipient in recipients if recipient]
    if not recipients:
        return

    with document.signed_pdf.open("rb") as signed_pdf_handle:
        attachment_bytes = signed_pdf_handle.read()

    subject = f"Completed document: {document.title}"
    body = (
        "The document below has been fully signed in Signacore.\n\n"
        f"Document: {document.title}\n"
        "The completed PDF is attached.\n"
    )
    send_email(
        subject,
        body,
        recipients,
        attachments=[
            (
                f"{document.title.replace(' ', '-').lower()}-signed.pdf",
                attachment_bytes,
                "application/pdf",
            )
        ],
    )


def send_progress_email(document: Document, signing_request: SigningRequest) -> None:
    admin_email = getattr(document.created_by, "email", "") or ""
    if not admin_email:
        return

    signer_name = signing_request.signer_name or signing_request.signer_email
    subject = f"Signing progress update: {document.title}"
    body = (
        "A signer completed their portion of a Signacore document.\n\n"
        f"Document: {document.title}\n"
        f"Signer: {signer_name}\n"
        f"Status: {document.status}\n"
    )
    send_email(subject, body, [admin_email])


def send_admin_account_created_email(
    email: str,
    username: str,
    temporary_password: str,
    login_url: str,
) -> None:
    subject = "Your SignaCore admin account is ready"
    body = (
        "Hello,\n\n"
        "An admin account has been created for you in SignaCore.\n\n"
        f"Username: {username}\n"
        f"Temporary password: {temporary_password}\n"
        f"Admin login: {login_url}\n\n"
        "Sign in and request a password change if this password was not shared through an approved channel.\n"
    )
    send_email(subject, body, [email])


def send_admin_password_changed_email(
    email: str,
    username: str,
    temporary_password: str,
    login_url: str,
) -> None:
    subject = "Your SignaCore admin password was changed"
    body = (
        "Hello,\n\n"
        "Your SignaCore admin password has been changed.\n\n"
        f"Username: {username}\n"
        f"Temporary password: {temporary_password}\n"
        f"Admin login: {login_url}\n\n"
        "If you did not request this change, contact the SignaCore owner immediately.\n"
    )
    send_email(subject, body, [email])


def send_account_verification_email(user) -> None:
    profile = user.signacore_profile
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    verification_url = (
        f"{settings.SIGNACORE_APP_URL.rstrip('/')}/api/auth/email/verify"
        f"?uid={uid}&token={token}"
    )
    safe_name = escape(profile.display_name or "there")
    safe_url = escape(verification_url)
    subject = "Verify your SignaCore account"
    body = (
        f"Hello {profile.display_name or 'there'},\n\n"
        "Verify your email address to activate your SignaCore account.\n\n"
        f"Verification link: {verification_url}\n\n"
        "If you did not create this account, ignore this email.\n"
    )
    html_body = f"""\
<!doctype html>
<html lang="en">
  <body style="margin:0;background:#03070c;color:#f4f8fb;font-family:Avenir Next,Segoe UI,Arial,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#03070c;padding:36px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;overflow:hidden;border:1px solid #24384b;border-radius:28px;background:#09131d;">
            <tr>
              <td style="padding:36px;background:linear-gradient(135deg,#0b3048,#09131d 66%,#321b12);">
                <div style="font-size:12px;font-weight:900;letter-spacing:0.16em;text-transform:uppercase;color:#52c9ff;">SignaCore</div>
                <h1 style="margin:18px 0 0;font-family:Georgia,serif;font-size:38px;line-height:1.05;color:#f4f8fb;">Verify your email</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:36px;">
                <p style="margin:0 0 16px;font-size:16px;line-height:1.65;color:#d5e0e9;">Hello {safe_name},</p>
                <p style="margin:0 0 26px;font-size:16px;line-height:1.65;color:#9cadbd;">Confirm this email address to activate your SignaCore account.</p>
                <a href="{safe_url}" style="display:inline-block;border-radius:12px;background:#f1f7fb;color:#07111d;font-size:15px;font-weight:900;text-decoration:none;padding:15px 22px;">Verify email</a>
                <p style="margin:28px 0 0;font-size:12px;line-height:1.6;color:#718394;">If you did not create this account, you can ignore this email.</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""
    send_email(subject, body, [profile.email], html_body=html_body)


def build_account_notice_html(
    *,
    name: str,
    title: str,
    message: str,
    action_label: str,
    action_url: str,
) -> str:
    safe_name = escape(name or "there")
    safe_title = escape(title)
    safe_message = escape(message)
    safe_action_label = escape(action_label)
    safe_action_url = escape(action_url)
    return f"""\
<!doctype html>
<html lang="en">
  <body style="margin:0;background:#03070c;color:#f4f8fb;font-family:Avenir Next,Segoe UI,Arial,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#03070c;padding:36px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;overflow:hidden;border:1px solid #24384b;border-radius:28px;background:#09131d;">
            <tr>
              <td style="padding:36px;background:linear-gradient(135deg,#0b3048,#09131d 66%,#321b12);">
                <div style="font-size:12px;font-weight:900;letter-spacing:0.16em;text-transform:uppercase;color:#52c9ff;">SignaCore</div>
                <h1 style="margin:18px 0 0;font-family:Georgia,serif;font-size:38px;line-height:1.05;color:#f4f8fb;">{safe_title}</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:36px;">
                <p style="margin:0 0 16px;font-size:16px;line-height:1.65;color:#d5e0e9;">Hello {safe_name},</p>
                <p style="margin:0 0 26px;font-size:16px;line-height:1.65;color:#9cadbd;">{safe_message}</p>
                <a href="{safe_action_url}" style="display:inline-block;border-radius:12px;background:#f1f7fb;color:#07111d;font-size:15px;font-weight:900;text-decoration:none;padding:15px 22px;">{safe_action_label}</a>
                <p style="margin:28px 0 0;font-size:12px;line-height:1.6;color:#718394;">If you did not perform this action, contact SignaCore support immediately.</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def send_account_welcome_email(user) -> None:
    profile = user.signacore_profile
    destination = "/account" if profile.account_type == profile.AccountTypeEnum.SIGNER else "/admin"
    account_url = f"{settings.SIGNACORE_APP_URL.rstrip('/')}{destination}"
    subject = "Welcome to SignaCore"
    body = (
        f"Hello {profile.display_name or 'there'},\n\n"
        "Your SignaCore account is active and ready to use.\n\n"
        f"Open SignaCore: {account_url}\n\n"
        "If you did not create this account, contact SignaCore support immediately.\n"
    )
    send_email(
        subject,
        body,
        [profile.email],
        html_body=build_account_notice_html(
            name=profile.display_name,
            title="Your account is ready",
            message="Your SignaCore account is active. You can now manage agreements or review documents assigned to you.",
            action_label="Open SignaCore",
            action_url=account_url,
        ),
    )


def send_account_login_email(user, method: str) -> None:
    profile = user.signacore_profile
    account_url = f"{settings.SIGNACORE_APP_URL.rstrip('/')}/login"
    safe_method = method.strip() or "your account credentials"
    subject = "New sign-in to your SignaCore account"
    body = (
        f"Hello {profile.display_name or 'there'},\n\n"
        f"A new sign-in to your SignaCore account was completed using {safe_method}.\n\n"
        f"SignaCore: {account_url}\n\n"
        "If this was not you, contact SignaCore support immediately.\n"
    )
    send_email(
        subject,
        body,
        [profile.email],
        html_body=build_account_notice_html(
            name=profile.display_name,
            title="New account sign-in",
            message=f"A new sign-in to your SignaCore account was completed using {safe_method}.",
            action_label="Open SignaCore",
            action_url=account_url,
        ),
    )
