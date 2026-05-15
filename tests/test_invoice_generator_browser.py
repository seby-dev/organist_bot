"""Test that generate_invoice reuses the browser singleton."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import the module at collection time so patch() can resolve its dotted path.
import organist_bot.integrations.invoice_generator as ig


@pytest.mark.asyncio
async def test_browser_launched_once_across_two_calls():
    """The Chromium browser should be launched only once, not once per invoice."""
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.pdf = AsyncMock()
    mock_browser = MagicMock()
    mock_browser.is_connected.return_value = True
    mock_browser.new_page = AsyncMock(return_value=mock_page)
    mock_browser.close = AsyncMock()
    mock_chromium = AsyncMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)
    mock_pw = MagicMock()
    mock_pw.chromium = mock_chromium
    mock_pw.stop = AsyncMock()

    # Reset the singleton before test
    ig._browser = None
    ig._pw_instance = None

    mock_template = MagicMock()
    mock_template.render.return_value = "<html></html>"
    mock_env = MagicMock()
    mock_env.get_template.return_value = mock_template

    with (
        patch("organist_bot.integrations.invoice_generator.async_playwright") as mock_ap,
        patch(
            "organist_bot.integrations.invoice_generator.load_clients",
            return_value={
                "test-client": {"name": "Test", "address": "1 Road", "email": "t@t.com", "cc": []}
            },
        ),
        patch("organist_bot.integrations.invoice_generator.save_invoice"),
        patch(
            "organist_bot.integrations.invoice_generator.get_next_invoice_number",
            return_value="INV-2026-001",
        ),
        patch("organist_bot.integrations.invoice_generator.OUTPUT_DIR") as mock_dir,
        patch("organist_bot.integrations.invoice_generator.Environment", return_value=mock_env),
    ):
        mock_dir.mkdir = MagicMock()
        mock_html_path = MagicMock()
        mock_html_path.resolve.return_value = "/tmp/test.html"
        mock_html_path.write_text = MagicMock()
        mock_html_path.unlink = MagicMock()
        mock_dir.__truediv__ = MagicMock(return_value=mock_html_path)

        mock_ap_instance = AsyncMock()
        mock_ap_instance.start = AsyncMock(return_value=mock_pw)
        mock_ap.return_value = mock_ap_instance

        items = [{"description": "Service", "quantity": 1, "unit_price": 100}]
        await ig.generate_invoice("test-client", items)
        await ig.generate_invoice("test-client", items)

    assert mock_chromium.launch.call_count == 1, "Browser should be launched only once"
