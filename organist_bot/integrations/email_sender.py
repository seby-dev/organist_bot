from __future__ import annotations

import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from organist_bot.config import settings

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def send_invoice_email(invoice_data: dict) -> dict:
    """Send the invoice PDF to the client via email.

    Returns {"success": True} or {"success": False, "error": "..."}.
    Never raises — all failures are captured and returned.
    """
    try:
        smtp_host = settings.smtp_host
        smtp_port = settings.smtp_port
        smtp_user = settings.smtp_user
        smtp_password = settings.smtp_password
        from_email = settings.from_email or smtp_user
        from_name = settings.from_name

        to_email = invoice_data.get("client_email", "")
        if not to_email:
            return {"success": False, "error": "Client has no email address on file."}

        env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
        template = env.get_template("email.html")

        html_body = template.render(
            from_name=from_name,
            from_email=from_email,
            client_name=invoice_data["client_name"],
            invoice_number=invoice_data["invoice_number"],
            date=invoice_data["date"],
            items=invoice_data.get("items", []),
            total=invoice_data["total"],
            currency=invoice_data["currency"],
            payment_note=settings.payment_note,
        )

        cc_list = invoice_data.get("client_cc", [])

        msg = MIMEMultipart()
        msg["From"] = f"{from_name} <{from_email}>"
        msg["To"] = to_email
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        msg["Subject"] = f"Invoice {invoice_data['invoice_number']} — {from_name}"

        msg.attach(MIMEText(html_body, "html"))

        pdf_path = invoice_data["pdf_path"]
        with open(pdf_path, "rb") as f:
            attachment = MIMEApplication(f.read(), _subtype="pdf")
            attachment.add_header("Content-Disposition", "attachment", filename=Path(pdf_path).name)
            msg.attach(attachment)

        all_recipients = [to_email] + cc_list

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(from_email, all_recipients, msg.as_string())

        logger.info("Invoice %s emailed to %s", invoice_data["invoice_number"], to_email)
        return {"success": True}

    except FileNotFoundError as e:
        logger.error("PDF not found for emailing: %s", e)
        return {"success": False, "error": f"Invoice PDF file not found: {e}"}
    except smtplib.SMTPAuthenticationError:
        logger.error("SMTP authentication failed")
        return {
            "success": False,
            "error": "Email authentication failed. Check SMTP credentials in .env.",
        }
    except smtplib.SMTPException as e:
        logger.error("SMTP error: %s", e)
        return {"success": False, "error": f"Email delivery failed: {e}"}
    except Exception as e:
        logger.error("Unexpected error sending email: %s", e)
        return {"success": False, "error": str(e)}
