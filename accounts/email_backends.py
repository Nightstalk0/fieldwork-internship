import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class BaseHttpEmailBackend(BaseEmailBackend):
    """Send Django EmailMessage objects over HTTPS using stdlib urllib."""

    endpoint = ""

    def _build_request(self, message):
        raise NotImplementedError

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        sent_count = 0
        timeout = getattr(settings, "EMAIL_TIMEOUT", 10)
        for message in email_messages:
            if not message.to:
                continue
            request = self._build_request(message)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    if 200 <= response.status < 300:
                        sent_count += 1
                    else:
                        raise OSError(f"Email API returned HTTP {response.status}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if not self.fail_silently:
                    raise OSError(f"Email API error HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if not self.fail_silently:
                    raise OSError(f"Email API connection failed: {exc.reason}") from exc
        return sent_count


class ResendEmailBackend(BaseHttpEmailBackend):
    """Send email via the Resend HTTPS API (works when SMTP ports are blocked)."""

    endpoint = "https://api.resend.com/emails"

    def _build_request(self, message):
        api_key = getattr(settings, "RESEND_API_KEY", "")
        if not api_key:
            raise OSError("RESEND_API_KEY is not configured.")
        payload = {
            "from": message.from_email or settings.DEFAULT_FROM_EMAIL,
            "to": list(message.to),
            "subject": message.subject,
            "text": message.body,
        }
        if message.cc:
            payload["cc"] = list(message.cc)
        if message.bcc:
            payload["bcc"] = list(message.bcc)
        if message.reply_to:
            payload["reply_to"] = list(message.reply_to)
        data = json.dumps(payload).encode("utf-8")
        return urllib.request.Request(
            self.endpoint,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "FieldworkPortal/1.0",
            },
            method="POST",
        )


class BrevoEmailBackend(BaseHttpEmailBackend):
    """Send email via the Brevo HTTPS API (works when SMTP ports are blocked)."""

    endpoint = "https://api.brevo.com/v3/smtp/email"

    def _build_request(self, message):
        api_key = getattr(settings, "BREVO_API_KEY", "")
        if not api_key:
            raise OSError("BREVO_API_KEY is not configured.")
        sender_email = (message.from_email or settings.DEFAULT_FROM_EMAIL).strip()
        payload = {
            "sender": {"email": sender_email},
            "to": [{"email": address} for address in message.to],
            "subject": message.subject,
            "textContent": message.body,
        }
        if message.cc:
            payload["cc"] = [{"email": address} for address in message.cc]
        if message.bcc:
            payload["bcc"] = [{"email": address} for address in message.bcc]
        if message.reply_to:
            payload["replyTo"] = {"email": message.reply_to[0]}
        data = json.dumps(payload).encode("utf-8")
        return urllib.request.Request(
            self.endpoint,
            data=data,
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "FieldworkPortal/1.0",
            },
            method="POST",
        )
