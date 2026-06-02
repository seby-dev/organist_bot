"""Render the production invoice template with mock data to verify the PDF pipeline.

Does NOT touch invoices.json or bump the invoice counter. Outputs the HTML and PDF
to output/invoice_samples/ so the live template change can be eyeballed end-to-end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "organist_bot" / "templates"
OUT_DIR = ROOT / "output" / "invoice_samples"

MOCK = {
    "from_name": "Sebastian Daku",
    "from_address": "12 Wren Avenue<br>London, NW3 4QZ",
    "bill_to_name": "St. Augustine's Church",
    "bill_to_address": "Highbury Park<br>London, N5 1RR",
    "date": "2 June 2026",
    "invoice_number": "SMOKE-2026-000",
    "items": [
        {
            "description": "Organ Recital — Evensong Service",
            "quantity": 1,
            "unit_price": 180.0,
            "total": 180.0,
        },
        {"description": "Rehearsal Attendance", "quantity": 1, "unit_price": 45.0, "total": 45.0},
    ],
    "subtotal": 225.0,
    "currency": "£",
    "payment_account_name": "S. Daku",
    "payment_account_number": "12345678",
    "payment_sort_code": "01-02-03",
    "payment_note": "",
}


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    html = env.get_template("invoice.html").render(**MOCK)

    html_path = OUT_DIR / "_smoke_render.html"
    pdf_path = OUT_DIR / "_smoke_render.pdf"
    png_path = OUT_DIR / "_smoke_render.png"
    html_path.write_text(html)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport={"width": 820, "height": 1160})
        await page.goto(f"file://{html_path.resolve()}")
        await page.wait_for_load_state("networkidle")
        await page.pdf(path=str(pdf_path), format="A4", print_background=True)
        await page.screenshot(path=str(png_path), full_page=True)
        await browser.close()

    print(f"HTML: {html_path}")
    print(f"PDF:  {pdf_path}")
    print(f"PNG:  {png_path}")


if __name__ == "__main__":
    asyncio.run(main())
