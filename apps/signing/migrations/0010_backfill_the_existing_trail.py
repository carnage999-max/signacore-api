"""Give documents signed before this existed a history too.

Not everything, because not everything was recorded: there was no notion of a document being
opened, so no opened event can honestly be invented for a past signature. What the old columns
do support is when a request went out, when its code was confirmed, and when it was signed.

The address and device are attached only to the last of those. SigningRequest held one of each,
overwritten as the signer moved through, so the value that survived belongs to whatever they did
last. Spreading it across earlier events would be making up where somebody was.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    SigningRequest = apps.get_model("signing", "SigningRequest")
    SigningEvent = apps.get_model("signing", "SigningEvent")

    events = []
    for request in SigningRequest.objects.all().iterator(chunk_size=500):
        trail = [("SENT", request.created_at)]
        if request.otp_verified_at:
            trail.append(("CODE_VERIFIED", request.otp_verified_at))
        if request.signed_at:
            trail.append(("SIGNED", request.signed_at))

        last = len(trail) - 1
        for index, (event, at) in enumerate(trail):
            carries_address = index == last and index > 0
            events.append(
                SigningEvent(
                    signing_request=request,
                    event=event,
                    at=at,
                    ip_address=request.ip_address if carries_address else "",
                    user_agent=request.user_agent if carries_address else "",
                    detail="Recorded before SignaCore kept a full trail",
                )
            )

    SigningEvent.objects.bulk_create(events, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [("signing", "0009_signing_event_trail")]
    # Deliberately not reversible by deleting rows. detail is encrypted with Fernet, which is
    # non-deterministic, so a query for the marker written above matches nothing - a reverse
    # written that way would report success and remove not one row. Backfilled events are
    # indistinguishable from recorded ones once this has run, and the table itself goes when
    # 0009 is reversed.
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
