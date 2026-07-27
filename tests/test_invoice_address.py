"""Tests for invoice address line rendering — no browser required.

Regression cover for addresses stored as HTML fragments (``<br>``) leaking
literal "<br>" text into the From / Bill To blocks of the rendered invoice,
because the Jinja env autoescapes.
"""

from jinja2 import Environment, FileSystemLoader, select_autoescape

from organist_bot.integrations.invoice_generator import TEMPLATES_DIR, address_lines


class TestAddressLines:
    def test_splits_on_br_tag(self):
        assert address_lines("St John the Evangelist <br>Grove Lane<br>KT1 2SU") == [
            "St John the Evangelist",
            "Grove Lane",
            "KT1 2SU",
        ]

    def test_splits_on_self_closing_and_spaced_br_variants(self):
        assert address_lines("A<br/>B<br />C<BR>D") == ["A", "B", "C", "D"]

    def test_splits_on_newlines(self):
        assert address_lines("Holy Cross\nParsons Green\nAshington Road, SW6 3QA") == [
            "Holy Cross",
            "Parsons Green",
            "Ashington Road, SW6 3QA",
        ]

    def test_splits_on_crlf(self):
        assert address_lines("A\r\nB") == ["A", "B"]

    def test_single_line_address_stays_one_line(self):
        assert address_lines("1 Clareville Rd, Caterham CR3 6LA, United Kingdom") == [
            "1 Clareville Rd, Caterham CR3 6LA, United Kingdom"
        ]

    def test_drops_blank_segments_and_strips_whitespace(self):
        assert address_lines("  A  <br><br>  B  \n\n") == ["A", "B"]

    def test_empty_input_returns_empty_list(self):
        assert address_lines("") == []
        assert address_lines(None) == []


class TestInvoiceTemplateRendersAddressLines:
    """The template must emit real <br> markup, never escaped text."""

    def _render(self, **overrides) -> str:
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        ctx = {
            "from_name": "A Organist",
            "from_address_lines": address_lines("12 Foo St<br>London<br>N1 1AA"),
            "bill_to_name": "St John the Evangelist",
            "bill_to_address_lines": address_lines("Grove Lane\nKingston\nKT1 2SU"),
            "date": "27 July 2026",
            "invoice_number": "INV-2026-001",
            "items": [{"description": "Organ", "quantity": 1, "unit_price": 150, "total": 150}],
            "subtotal": 150,
            "currency": "£",
            "payment_account_name": "A Organist",
            "payment_account_number": "12345678",
            "payment_sort_code": "00-00-00",
            "payment_note": "",
        }
        ctx.update(overrides)
        return env.get_template("invoice.html").render(**ctx)

    def test_no_escaped_br_text_in_output(self):
        assert "&lt;br&gt;" not in self._render()

    def test_from_address_lines_separated_by_br_markup(self):
        html = self._render()
        assert "12 Foo St<br>London<br>N1 1AA" in html

    def test_bill_to_address_lines_separated_by_br_markup(self):
        html = self._render()
        assert "Grove Lane<br>Kingston<br>KT1 2SU" in html

    def test_address_text_is_still_escaped(self):
        """Autoescaping must stay on for the address content itself."""
        html = self._render(bill_to_address_lines=address_lines("St <script>x</script> Church"))
        assert "<script>x</script>" not in html
        assert "&lt;script&gt;" in html
