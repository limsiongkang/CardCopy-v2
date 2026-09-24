"""
The alert email.

Sent over SMTP using the credentials in .env. Nothing is hardcoded, and the
password is never written to the log file - only whether sending worked.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from html import escape

import config


class EmailError(Exception):
    """Sending failed, explained in plain English."""


def _money(value) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def build_message(alerts: list[dict], run_date: str, stats: dict, sheet_url: str = "") -> EmailMessage:
    subject = (
        f"{config.CLIENT_NAME}: {len(alerts)} competitor "
        f"{'change' if len(alerts) == 1 else 'changes'} - {run_date}"
    )

    lines = [
        f"Competitor check for {config.CLIENT_NAME}, {run_date}.",
        "",
        f"Products checked : {stats.get('checked', 0)}",
        f"Read successfully: {stats.get('ok', 0)}",
        f"Could not be read: {stats.get('failed', 0)}",
        f"Changes found    : {len(alerts)}",
        "",
    ]

    if alerts:
        lines.append("WHAT CHANGED")
        lines.append("-" * 60)
        for alert in alerts:
            lines.append(f"{alert['alert']}: {alert['competitor']} - {alert['product']}")
            lines.append(f"    was {alert['was']}   ->   now {alert['now']}")
            lines.append(f"    {alert['url']}")
            lines.append("")
    else:
        lines.append("No price drops or stock changes since the previous run.")
        lines.append("")

    if sheet_url:
        lines.append(f"Full results: {sheet_url}")

    body_text = "\n".join(lines)

    rows_html = "".join(
        f"<tr>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>"
        f"<strong>{escape(a['alert'])}</strong></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{escape(a['competitor'])}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>"
        f"<a href='{escape(a['url'])}'>{escape(a['product'][:70])}</a></td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#777'>{escape(str(a['was']))}</td>"
        f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-weight:600'>{escape(str(a['now']))}</td>"
        f"</tr>"
        for a in alerts
    )

    if alerts:
        table_html = (
            "<table style='border-collapse:collapse;font-size:14px;margin-top:12px'>"
            "<tr style='text-align:left;background:#f5f5f5'>"
            "<th style='padding:6px 10px'>Change</th>"
            "<th style='padding:6px 10px'>Competitor</th>"
            "<th style='padding:6px 10px'>Product</th>"
            "<th style='padding:6px 10px'>Was</th>"
            "<th style='padding:6px 10px'>Now</th>"
            "</tr>" + rows_html + "</table>"
        )
    else:
        table_html = "<p>No price drops or stock changes since the previous run.</p>"

    body_html = f"""<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#222">
<h2 style="margin-bottom:4px">{escape(config.CLIENT_NAME)} - competitor check</h2>
<p style="color:#777;margin-top:0">{escape(run_date)}</p>
<p style="font-size:14px">
Checked <strong>{stats.get('checked', 0)}</strong> products &middot;
read <strong>{stats.get('ok', 0)}</strong> &middot;
failed <strong>{stats.get('failed', 0)}</strong> &middot;
changes <strong>{len(alerts)}</strong>
</p>
{table_html}
{f'<p style="margin-top:18px"><a href="{escape(sheet_url)}">Open the full sheet</a></p>' if sheet_url else ''}
</body></html>"""

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_FROM or config.SMTP_USERNAME
    message["To"] = config.ALERT_EMAIL_TO
    message.set_content(body_text)
    message.add_alternative(body_html, subtype="html")
    return message


def send(message: EmailMessage, log=print) -> None:
    if not config.SMTP_USERNAME or not config.SMTP_PASSWORD:
        raise EmailError(
            "SMTP_USERNAME or SMTP_PASSWORD is empty in .env, so no email was sent. "
            "See step 4 of README.md."
        )
    if not config.ALERT_EMAIL_TO:
        raise EmailError("ALERT_EMAIL_TO is empty in .env, so there is nowhere to send.")

    context = ssl.create_default_context()
    try:
        if config.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context, timeout=60) as server:
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
                server.send_message(message)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        raise EmailError(
            "Gmail rejected the username or password.\n"
            "  SMTP_PASSWORD must be a 16-character Google app password, not your\n"
            "  normal Gmail password. See step 4 of README.md.\n"
            f"  Google said: {exc.smtp_error.decode(errors='replace') if exc.smtp_error else exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise EmailError(f"Could not send the email: {exc}") from exc

    log(f"  Emailed {config.ALERT_EMAIL_TO}.")
