from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0014_alter_adminauditlog_action_document_import"),
    ]

    operations = [
        migrations.AlterField(
            model_name="adminauditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("LOGIN", "Login"),
                    ("LOGOUT", "Logout"),
                    ("DOCUMENT_LIST", "Document List"),
                    ("DOCUMENT_VIEW", "Document View"),
                    ("DOCUMENT_UPLOAD", "Document Upload"),
                    ("DOCUMENT_IMPORT", "Document Import"),
                    ("DOCUMENT_AUTHOR", "Document Author"),
                    ("DOCUMENT_UPDATE", "Document Update"),
                    ("DOCUMENT_DELETE", "Document Delete"),
                    ("DOCUMENT_VOID", "Document Void"),
                    ("DOCUMENT_DOWNLOAD", "Document Download"),
                    ("FIELD_CREATE", "Field Create"),
                    ("FIELD_UPDATE", "Field Update"),
                    ("FIELD_DELETE", "Field Delete"),
                    ("SIGNING_REQUEST_SEND", "Signing Request Send"),
                    ("SIGNING_REQUEST_RESEND", "Signing Request Resend"),
                    ("ADMIN_USER_LIST", "Admin User List"),
                    ("ADMIN_USER_CREATE", "Admin User Create"),
                    ("ORGANIZATION_MEMBER_LIST", "Organization Member List"),
                    ("ORGANIZATION_MEMBER_INVITE", "Organization Member Invite"),
                    ("ORGANIZATION_MEMBER_REMOVE", "Organization Member Remove"),
                    ("ADMIN_PASSWORD_CHANGE", "Admin Password Change"),
                    ("AUDIT_LOG_LIST", "Audit Log List"),
                    ("BILLING_VIEW", "Billing View"),
                    ("BILLING_CHECKOUT", "Billing Checkout"),
                    ("BILLING_PORTAL", "Billing Portal"),
                    ("OAUTH_LOGIN", "OAuth Login"),
                    ("EMAIL_REGISTER", "Email Registration"),
                    ("EMAIL_LOGIN", "Email Login"),
                ],
                max_length=64,
            ),
        ),
    ]
