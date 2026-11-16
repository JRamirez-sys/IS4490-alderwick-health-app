from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("login", "Signed in"),
                    ("logout", "Signed out"),
                    ("login_failed", "Failed sign-in"),
                    ("access_denied", "Access denied"),
                    ("view_chart", "Viewed patient chart"),
                    ("view_patient_profile", "Viewed patient profile"),
                    ("view_own_record", "Patient viewed own record"),
                    ("view_visit", "Viewed visit note"),
                    ("create_appointment_request", "Submitted appointment request"),
                    ("update_appointment_request", "Updated appointment request"),
                    ("decline_appointment_request", "Declined appointment request"),
                    ("register_patient", "Registered new patient"),
                    ("schedule_appointment", "Scheduled appointment"),
                    ("cancel_appointment", "Cancelled appointment"),
                    ("check_in", "Checked in patient"),
                    ("mark_no_show", "Marked no-show"),
                    ("send_message", "Sent message"),
                    ("translate_message", "Translated patient message"),
                    ("close_thread", "Closed message thread"),
                    ("save_visit_note", "Saved visit note"),
                    ("sign_visit_note", "Signed visit note"),
                ],
                db_index=True,
                max_length=40,
            ),
        ),
    ]
