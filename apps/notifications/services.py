from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.utils.encoding import force_bytes
from django.utils.html import escape
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


def build_branded_email_html(
    *,
    name: str = "there",
    title: str,
    message: str,
    action_label: str | None = None,
    action_url: str | None = None,
    details: list[tuple[str, str]] | None = None,
    code: str | None = None,
    footer: str = "If this message was unexpected, contact SignaCore support or ignore it if no action is required.",
) -> str:
    safe_name = escape(name or "there")
    safe_title = escape(title)
    safe_message = escape(message)
    safe_action_label = escape(action_label or "")
    safe_action_url = escape(action_url or "")
    safe_code = escape(code or "")
    safe_footer = escape(footer)
    logo_url = escape(f"{settings.SIGNACORE_APP_URL.rstrip('/')}/signa-core.png")
    detail_rows = ""
    for label, value in details or []:
        detail_rows += f"""\
                      <tr>
                        <td style="padding:10px 0;border-bottom:1px solid rgba(148,163,184,0.18);font-size:12px;font-weight:900;letter-spacing:0.12em;text-transform:uppercase;color:#6ee7f9;">{escape(label)}</td>
                        <td style="padding:10px 0;border-bottom:1px solid rgba(148,163,184,0.18);font-size:14px;font-weight:700;color:#e5edf5;text-align:right;">{escape(value)}</td>
                      </tr>
"""
    details_html = (
        f"""\
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin:0 0 26px;border:1px solid rgba(148,163,184,0.22);border-radius:18px;background:#0c1722;padding:8px 18px;">
                  {detail_rows}
                </table>
"""
        if detail_rows
        else ""
    )
    code_html = (
        f"""\
                <div style="margin:0 0 26px;border:1px solid rgba(110,231,249,0.35);border-radius:18px;background:#08121c;padding:22px;text-align:center;">
                  <div style="font-size:12px;font-weight:900;letter-spacing:0.14em;text-transform:uppercase;color:#6ee7f9;">Verification code</div>
                  <div style="margin-top:10px;font-family: ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:34px;font-weight:900;letter-spacing:0.22em;color:#f8fafc;">{safe_code}</div>
                </div>
"""
        if safe_code
        else ""
    )
    action_html = (
        f"""\
                <a href="{safe_action_url}" style="display:inline-block;border-radius:14px;background:#f8fafc;color:#07111d;font-size:15px;font-weight:900;text-decoration:none;padding:15px 22px;">{safe_action_label}</a>
                <p style="margin:24px 0 0;font-size:12px;line-height:1.6;color:#8191a3;">If the button does not work, copy this link into your browser:</p>
                <p style="margin:8px 0 0;font-size:12px;line-height:1.6;word-break:break-all;color:#6ee7f9;">{safe_action_url}</p>
"""
        if safe_action_label and safe_action_url
        else ""
    )
    return f"""\
<!doctype html>
<html lang="en">
  <body style="margin:0;background:#03070c;color:#f4f8fb;font-family:Avenir Next,Segoe UI,Arial,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#03070c;padding:36px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;overflow:hidden;border:1px solid #24384b;border-radius:28px;background:#09131d;box-shadow:0 24px 80px rgba(0,0,0,0.38);">
            <tr>
              <td style="padding:34px 36px;background:linear-gradient(135deg,#061520,#08283f 52%,#321a10);">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
                  <tr>
                    <td style="vertical-align:middle;">
                      <img src="{logo_url}" width="48" height="48" alt="SignaCore" style="display:block;border-radius:12px;">
                    </td>
                    <td align="right" style="vertical-align:middle;font-size:12px;font-weight:900;letter-spacing:0.16em;text-transform:uppercase;color:#6ee7f9;">SignaCore</td>
                  </tr>
                </table>
                <h1 style="margin:24px 0 0;font-family:Georgia,serif;font-size:36px;line-height:1.05;color:#f8fafc;">{safe_title}</h1>
              </td>
            </tr>
            <tr>
              <td style="padding:36px;">
                <p style="margin:0 0 16px;font-size:16px;line-height:1.65;color:#d5e0e9;">Hello {safe_name},</p>
                <p style="margin:0 0 26px;font-size:16px;line-height:1.65;color:#9cadbd;">{safe_message}</p>
{details_html}{code_html}{action_html}
              </td>
            </tr>
            <tr>
              <td style="padding:20px 36px 30px;border-top:1px solid rgba(148,163,184,0.14);color:#718394;font-size:12px;line-height:1.6;">
                <strong style="color:#d5e0e9;">SignaCore - by Se7en</strong><br>
                {safe_footer}
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def build_invitation_html(signing_request: SigningRequest, signing_link: str) -> str:
    return build_branded_email_html(
        name=signing_request.signer_name or "there",
        title="Signature requested",
        message="Please review and complete this document. You will verify your email before signing.",
        action_label="Review and sign",
        action_url=signing_link,
        details=[
            ("Document", signing_request.document.title),
            ("Expires", f"{signing_request.expires_at:%B %d, %Y at %I:%M %p %Z}"),
        ],
        footer="If you were not expecting this request, ignore this email.",
    )


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


def send_organization_invitation(
    email: str,
    organization_name: str,
    inviter_name: str,
    role: str,
    token: str,
) -> None:
    invitation_url = f"{settings.SIGNACORE_APP_URL.rstrip('/')}/register" f"?role=company&invite={token}"
    subject = f"You have been invited to {organization_name} on SignaCore"
    body = (
        f"Hello,\n\n{inviter_name} invited you to join {organization_name} on SignaCore.\n\n"
        f"Create your company account: {invitation_url}\n\n"
        "This invitation expires in 7 days. If you were not expecting it, ignore this email.\n"
    )
    send_email(
        subject,
        body,
        [email],
        html_body=build_branded_email_html(
            title="You are invited to a workspace",
            message=f"{inviter_name} invited you to join {organization_name} as a {role.lower()}.",
            action_label="Join workspace",
            action_url=invitation_url,
            details=[("Workspace", organization_name), ("Role", role.title())],
            footer="This invitation expires in 7 days. If you were not expecting it, ignore this email.",
        ),
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
    send_email(
        subject,
        body,
        [signing_request.signer_email],
        html_body=build_branded_email_html(
            name=signer_name,
            title="Your verification code",
            message=f"Use this code to continue signing {signing_request.document.title}.",
            code=otp_code,
            details=[
                ("Document", signing_request.document.title),
                ("Expires in", f"{settings.OTP_EXPIRY_MINUTES} minutes"),
            ],
            footer="If you did not request this code, ignore this email.",
        ),
    )


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
        html_body=build_branded_email_html(
            title="Document completed",
            message="The document below has been fully signed in SignaCore. The completed PDF is attached.",
            details=[("Document", document.title)],
            footer="This completed PDF is being delivered to the document owner and assigned signers.",
        ),
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
    send_email(
        subject,
        body,
        [admin_email],
        html_body=build_branded_email_html(
            title="Signing progress update",
            message="A signer completed their portion of a SignaCore document.",
            details=[
                ("Document", document.title),
                ("Signer", signer_name),
                ("Status", document.status),
            ],
            footer="You are receiving this because you own or manage this signing request.",
        ),
    )


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
    send_email(
        subject,
        body,
        [email],
        html_body=build_branded_email_html(
            title="Admin account created",
            message="An admin account has been created for you in SignaCore.",
            action_label="Open admin console",
            action_url=login_url,
            details=[
                ("Username", username),
                ("Temporary password", temporary_password),
            ],
            footer="Sign in and request a password change if this password was not shared through an approved channel.",
        ),
    )


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
    send_email(
        subject,
        body,
        [email],
        html_body=build_branded_email_html(
            title="Admin password changed",
            message="Your SignaCore admin password has been changed.",
            action_label="Open admin console",
            action_url=login_url,
            details=[
                ("Username", username),
                ("Temporary password", temporary_password),
            ],
            footer="If you did not request this change, contact the SignaCore owner immediately.",
        ),
    )


def send_account_verification_email(user) -> None:
    profile = user.signacore_profile
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    verification_url = f"{settings.SIGNACORE_APP_URL.rstrip('/')}/api/auth/email/verify" f"?uid={uid}&token={token}"
    subject = "Verify your SignaCore account"
    body = (
        f"Hello {profile.display_name or 'there'},\n\n"
        "Verify your email address to activate your SignaCore account.\n\n"
        f"Verification link: {verification_url}\n\n"
        "If you did not create this account, ignore this email.\n"
    )
    html_body = build_branded_email_html(
        name=profile.display_name or "there",
        title="Verify your email",
        message="Confirm this email address to activate your SignaCore account.",
        action_label="Verify email",
        action_url=verification_url,
        footer="If you did not create this account, you can ignore this email.",
    )
    send_email(subject, body, [profile.email], html_body=html_body)


def build_account_notice_html(
    *,
    name: str,
    title: str,
    message: str,
    action_label: str,
    action_url: str,
) -> str:
    return build_branded_email_html(
        name=name,
        title=title,
        message=message,
        action_label=action_label,
        action_url=action_url,
        footer="If you did not perform this action, contact SignaCore support immediately.",
    )


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
