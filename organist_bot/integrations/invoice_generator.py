from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover — playwright not installed in base env
    async_playwright = None  # noqa: F841

from organist_bot.config import settings

_PKG_ROOT = Path(__file__).parent.parent  # organist_bot/
_PROJ_ROOT = _PKG_ROOT.parent  # project root

TEMPLATES_DIR = _PKG_ROOT / "templates"
OUTPUT_DIR = _PROJ_ROOT / "output"
CLIENTS_FILE = _PROJ_ROOT / "clients.json"
INVOICES_FILE = _PROJ_ROOT / "invoices.json"

logger = logging.getLogger(__name__)

_pw_instance = None
_browser = None


async def _get_browser():
    global _pw_instance, _browser
    if _browser is None or not _browser.is_connected():
        _pw_instance = await async_playwright().start()
        _browser = await _pw_instance.chromium.launch()
    return _browser


# ── Clients ───────────────────────────────────────────────────────────────────


def load_clients() -> dict:
    if CLIENTS_FILE.exists():
        with open(CLIENTS_FILE) as f:
            return json.load(f)
    return {}


def save_clients(clients: dict) -> None:
    with open(CLIENTS_FILE, "w") as f:
        json.dump(clients, f, indent=2)


def add_client(
    key: str, name: str, address: str, email: str = "", cc: list[str] | None = None
) -> None:
    clients = load_clients()
    clients[key] = {"name": name, "address": address, "email": email, "cc": cc or []}
    save_clients(clients)


def edit_client(
    key: str,
    name: str | None = None,
    address: str | None = None,
    email: str | None = None,
    cc: list[str] | None = None,
) -> None:
    clients = load_clients()
    if key not in clients:
        raise ValueError(f"Client '{key}' not found.")
    if name is not None:
        clients[key]["name"] = name
    if address is not None:
        clients[key]["address"] = address
    if email is not None:
        clients[key]["email"] = email
    if cc is not None:
        clients[key]["cc"] = cc
    save_clients(clients)


def delete_client(key: str) -> None:
    clients = load_clients()
    if key not in clients:
        raise ValueError(f"Client '{key}' not found.")
    del clients[key]
    save_clients(clients)


# ── Invoice history ───────────────────────────────────────────────────────────


def load_invoices() -> dict:
    if INVOICES_FILE.exists():
        with open(INVOICES_FILE) as f:
            return json.load(f)
    return {}


def save_invoice(invoice_data: dict) -> None:
    invoices = load_invoices()
    record = {**invoice_data, "pdf_path": str(invoice_data["pdf_path"])}
    invoices[invoice_data["invoice_number"]] = record
    with open(INVOICES_FILE, "w") as f:
        json.dump(invoices, f, indent=2)


def mark_invoice_emailed(invoice_number: str) -> None:
    invoices = load_invoices()
    if invoice_number in invoices:
        invoices[invoice_number]["emailed"] = True
        with open(INVOICES_FILE, "w") as f:
            json.dump(invoices, f, indent=2)


# ── Invoice numbering ─────────────────────────────────────────────────────────


def get_next_invoice_number() -> str:
    year = datetime.now().year
    invoices = load_invoices()
    year_count = sum(1 for inv in invoices.values() if inv.get("year") == year)
    return f"INV-{year}-{year_count + 1:03d}"


# ── PDF generation ────────────────────────────────────────────────────────────


async def generate_invoice(client_key: str, items: list[dict]) -> dict:
    """Generate a PDF invoice. Each item: {description, quantity, unit_price}.

    Returns a metadata dict including pdf_path, invoice_number, total, etc.
    """
    clients = load_clients()
    if client_key not in clients:
        raise ValueError(f"Client '{client_key}' not found. Available: {', '.join(clients.keys())}")

    client = clients[client_key]

    processed_items = [
        {
            "description": item["description"],
            "quantity": item["quantity"],
            "unit_price": item["unit_price"],
            "total": item["quantity"] * item["unit_price"],
        }
        for item in items
    ]
    subtotal = sum(i["total"] for i in processed_items)

    invoice_number = get_next_invoice_number()
    date_str = datetime.now().strftime("%-d %B %Y")
    currency = settings.currency

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("invoice.html")

    html = template.render(
        from_name=settings.from_name,
        from_address=settings.from_address,
        bill_to_name=client["name"],
        bill_to_address=client["address"],
        date=date_str,
        invoice_number=invoice_number,
        items=processed_items,
        subtotal=subtotal,
        currency=currency,
        payment_account_name=settings.payment_account_name,
        payment_account_number=settings.payment_account_number,
        payment_sort_code=settings.payment_sort_code,
        payment_note=settings.payment_note,
    )

    OUTPUT_DIR.mkdir(exist_ok=True)
    safe_key = client_key.replace(" ", "-")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    html_path = OUTPUT_DIR / f"invoice-{safe_key}-{timestamp}.html"
    html_path.write_text(html)

    pdf_path = OUTPUT_DIR / f"invoice-{safe_key}-{timestamp}.pdf"

    browser = await _get_browser()
    page = await browser.new_page()
    await page.goto(f"file://{html_path.resolve()}")
    await page.pdf(path=str(pdf_path), format="A4", print_background=True)
    await page.close()

    html_path.unlink()

    result = {
        "pdf_path": pdf_path,
        "client_key": client_key,
        "client_name": client["name"],
        "client_email": client.get("email", ""),
        "client_cc": client.get("cc", []),
        "invoice_number": invoice_number,
        "year": datetime.now().year,
        "date": date_str,
        "items": processed_items,
        "total": subtotal,
        "currency": currency,
        "emailed": False,
        "created_at": datetime.now().isoformat(),
    }
    save_invoice(result)
    return result
