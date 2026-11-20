from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse

from apps.events.auth import personal_join_link


class EmailService:
    @staticmethod
    def _render_styled_email(
        *,
        title: str,
        greeting: str,
        paragraphs: list[str],
        cta_text: str | None = None,
        cta_url: str | None = None,
        footer_note: str | None = None,
    ) -> tuple[str, str]:
        """Generate consistent plain-text and responsive HTML email variants with Meetakk branding."""
        plain_lines = []
        if greeting:
            plain_lines.append(greeting)
            plain_lines.append("")
        for p in paragraphs:
            plain_lines.append(p)
            plain_lines.append("")
        if cta_text and cta_url:
            plain_lines.append(f"{cta_text}: {cta_url}")
            plain_lines.append("")
        if footer_note:
            plain_lines.append(footer_note)
            plain_lines.append("")
        plain_lines.append("Meetakk Team")
        plain_text = "\n".join(plain_lines)

        paragraphs_html = "".join(
            f'<p style="margin: 0 0 16px 0; font-size: 15px; line-height: 1.6; color: #334155;">{p}</p>'
            for p in paragraphs
        )
        cta_html = ""
        if cta_text and cta_url:
            cta_html = f"""
            <div style="margin: 28px 0; text-align: center;">
              <a href="{cta_url}" style="background-color: #0f172a; color: #ffffff; padding: 14px 30px; text-decoration: none; border-radius: 8px; font-weight: 700; font-size: 16px; display: inline-block; letter-spacing: 0.02em; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
                {cta_text}
              </a>
              <div style="margin-top: 12px; font-size: 13px; color: #64748b; word-break: break-all;">
                Direct link: <a href="{cta_url}" style="color: #2563eb; font-weight: 600;">{cta_url}</a>
              </div>
            </div>
            """
        footer_html = ""
        if footer_note:
            footer_html = f'<p style="margin: 24px 0 0 0; font-size: 13px; line-height: 1.5; color: #64748b; border-top: 1px solid #e2e8f0; padding-top: 16px;">{footer_note}</p>'

        html_text = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
</head>
<body style="margin: 0; padding: 24px 12px; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
  <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 10px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.06); border: 1px solid #e2e8f0;">
    <div style="background-color: #0f172a; padding: 22px 24px; text-align: center;">
      <span style="display: inline-block; font-size: 22px; font-weight: 800; color: #ffffff; letter-spacing: 0.14em; text-transform: uppercase;">
        MEETAKK
      </span>
    </div>
    <div style="padding: 32px 28px;">
      <h1 style="margin: 0 0 20px 0; font-size: 21px; font-weight: 800; color: #0f172a; line-height: 1.35;">
        {title}
      </h1>
      <p style="margin: 0 0 16px 0; font-size: 15px; font-weight: 600; color: #1e293b;">
        {greeting}
      </p>
      {paragraphs_html}
      {cta_html}
      {footer_html}
    </div>
    <div style="background-color: #f8fafc; padding: 16px 24px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0;">
      &copy; Meetakk &bull; Speed Dating &amp; Social Platform
    </div>
  </div>
</body>
</html>"""
        return plain_text, html_text

    @staticmethod
    def send_registration_receipt_email(
        recipient_email, name, event_title, registration_status, join_url=None
    ):
        waitlisted = registration_status == "waitlisted"
        subject = (
            f"Registration received for: {event_title}"
            if waitlisted
            else f"Your Registration & Access Link for: {event_title}"
        )
        if waitlisted:
            paragraphs = [
                f"Thank you for registering for {event_title}.",
                "You are currently on the waitlist; your place is not yet confirmed. We will email you immediately if a place becomes available.",
            ]
            cta_text = None
            cta_url = None
            footer_note = "If you have any questions, feel free to reply to this email."
        else:
            paragraphs = [
                f"Thank you for registering for {event_title}!",
                "Your registration is confirmed. You can join the event directly using your personal link below when the event begins.",
                "Please keep this email safe. Your link is personal to you and does not require an additional password.",
            ]
            cta_text = "Enter Event Room"
            cta_url = join_url
            footer_note = "This link is personal and will remain active until the event ends. Please do not share it."

        plain, html = EmailService._render_styled_email(
            title=f"Registration Confirmed: {event_title}" if not waitlisted else f"Waitlist Entry: {event_title}",
            greeting=f"Hello {name},",
            paragraphs=paragraphs,
            cta_text=cta_text,
            cta_url=cta_url,
            footer_note=footer_note,
        )
        delivered = send_mail(
            subject=subject,
            message=plain,
            html_message=html,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        if delivered != 1:
            raise RuntimeError("Registration receipt was not accepted for delivery")

    @staticmethod
    def send_event_reminder_email(
        recipient_email, name, event_title, join_url, time_until_start="6 hours"
    ):
        subject = f"Reminder: {event_title} starts in {time_until_start}!"
        paragraphs = [
            f"This is a friendly reminder that {event_title} starts in {time_until_start}.",
            "Get ready to meet your dates! Make sure your camera and microphone are tested and ready.",
            "Click the button below to open your personal event room when it's time to begin:",
        ]
        plain, html = EmailService._render_styled_email(
            title=f"Reminder: {event_title} starts in {time_until_start}",
            greeting=f"Hello {name},",
            paragraphs=paragraphs,
            cta_text="Enter Event Room",
            cta_url=join_url,
            footer_note="Your personal access link is unique to you. We look forward to seeing you tonight!",
        )
        delivered = send_mail(
            subject=subject,
            message=plain,
            html_message=html,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            fail_silently=False,
        )
        if delivered != 1:
            raise RuntimeError("Event reminder was not accepted for delivery")

    @staticmethod
    def send_registration_invitation_email(
        recipient_email, event_title, registration_url
    ):
        subject = f"You're invited to register for: {event_title}"
        paragraphs = [
            f"You have been invited to register for {event_title}.",
            "Click the link below to complete your registration and secure your spot:",
        ]
        plain, html = EmailService._render_styled_email(
            title=f"Invitation to Register: {event_title}",
            greeting="Hello,",
            paragraphs=paragraphs,
            cta_text="Complete Registration",
            cta_url=registration_url,
            footer_note="We look forward to seeing you at the event.",
        )
        try:
            send_mail(
                subject=subject,
                message=plain,
                html_message=html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"Error sending registration invitation: {str(e)}")

    @staticmethod
    def send_verification_email(
        recipient_email,
        name,
        token,
        event_title="Event",
        registration_status="pending",
    ):
        verify_url = f"{settings.FRONTEND_URL}{reverse('registration:verify-email', args=[token])}"
        subject = f"Registration Verification for: {event_title}"
        if registration_status == "waitlisted":
            registration_note = (
                "Your email verification is needed to keep your waitlist entry. "
                "A place is not confirmed unless you later accept an invitation."
            )
        else:
            registration_note = (
                "Your place is reserved for 24 hours. After you verify your email, "
                "your registration is complete and your room access link is ready."
            )

        paragraphs = [
            f"Thank you for registering for {event_title}.",
            "Please click the button below to verify your email address and activate your registration.",
            registration_note,
        ]
        plain, html = EmailService._render_styled_email(
            title=f"Verify Your Email: {event_title}",
            greeting=f"Hello {name},",
            paragraphs=paragraphs,
            cta_text="Verify Email",
            cta_url=verify_url,
            footer_note="This verification link is valid for 24 hours.",
        )
        try:
            send_mail(
                subject=subject,
                message=plain,
                html_message=html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"Error sending verification email: {str(e)}")

    @staticmethod
    def build_event_access_url(participant, raw_token):
        join_url = personal_join_link(participant, raw_token)
        if join_url.startswith(("http://", "https://")):
            return join_url
        return f"{settings.FRONTEND_URL.rstrip('/')}{join_url}"

    @staticmethod
    def send_event_access_email(recipient_email, name, event_title, join_url):
        subject = f"Your access link for: {event_title}"
        paragraphs = [
            f"Your registration for {event_title} is confirmed.",
            "Use your personal link below to enter the event room:",
        ]
        plain, html = EmailService._render_styled_email(
            title=f"Your Event Access Link: {event_title}",
            greeting=f"Hello {name},",
            paragraphs=paragraphs,
            cta_text="Enter Event Room",
            cta_url=join_url,
            footer_note="This link is personal and expires when the event ends. Please do not forward it.",
        )
        try:
            send_mail(
                subject=subject,
                message=plain,
                html_message=html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"Error sending event access email: {str(e)}")

    @staticmethod
    def send_waitlist_invitation_email(recipient_email, name, event_title, message):
        subject = f"A place is available for: {event_title}"
        paragraphs = [message]
        plain, html = EmailService._render_styled_email(
            title=f"Place Available: {event_title}",
            greeting=f"Hello {name},",
            paragraphs=paragraphs,
        )
        try:
            send_mail(
                subject=subject,
                message=plain,
                html_message=html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
        except Exception as e:
            raise RuntimeError(f"Error sending waitlist invitation: {str(e)}")

    @staticmethod
    def send_matches_summary_email(
        recipient_email,
        name,
        event_title,
        matches,
    ):
        """Deliver a consolidated list of all mutual matches with WhatsApp numbers after the event ends."""
        subject = f"Your Matches from {event_title} — Meetakk"
        if not matches:
            paragraphs = [
                f"Thank you for participating in {event_title}!",
                "You did not have any mutual matches this time, but we hope you had a great experience meeting new people.",
                "We look forward to seeing you at our upcoming events!",
            ]
        else:
            match_items = []
            for m in matches:
                p_name = m.get("name") or "Your Match"
                p_phone = m.get("whatsapp") or "No WhatsApp provided"
                match_items.append(f"&bull; <strong>{p_name}</strong> &mdash; WhatsApp: <code>{p_phone}</code>")
            matches_html = "<br>".join(match_items)
            paragraphs = [
                f"Thank you for participating in {event_title}!",
                "Here are your mutual matches from the event:",
                matches_html,
                "Please reach out respectfully and enjoy getting to know each other!",
            ]

        plain, html = EmailService._render_styled_email(
            title=f"Your Matches: {event_title}",
            greeting=f"Hello {name},",
            paragraphs=paragraphs,
            footer_note="Thank you for using Meetakk.",
        )
        try:
            sent = send_mail(
                subject=subject,
                message=plain,
                html_message=html,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient_email],
                fail_silently=False,
            )
            if sent != 1:
                raise RuntimeError("The email backend did not accept the message")
        except Exception as e:
            raise RuntimeError(f"Error sending matches summary email: {str(e)}")
