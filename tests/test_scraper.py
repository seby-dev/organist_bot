from unittest.mock import Mock, patch

import pytest
from bs4 import BeautifulSoup
from requests.exceptions import HTTPError, RequestException, Timeout
from tenacity import RetryError

from organist_bot.scraper import Scraper

# Sample HTML content for testing
SAMPLE_GIG_HTML = """
<html>
<body>
    <div class="booking">
        <h2 class="type">Wedding Service</h2>
        <h3 class="organisation">St. Mary's Church</h3>
        <h4 class="locality">London</h4>
        <p class="date">15 March 2026</p>
        <p class="time">14:00</p>
        <p class="fee">GBP 150</p>
        <a class="noselect" href="/gig/123">View Details</a>
    </div>
    <div class="booking">
        <h2 class="type">Sunday Service</h2>
        <h3 class="organisation">St. John's Cathedral</h3>
        <h4 class="locality">Manchester</h4>
        <p class="date">22 March 2026</p>
        <p class="time">10:00</p>
        <p class="fee">GBP 75</p>
        <a class="noselect" href="/gig/124">View Details</a>
    </div>
</body>
</html>
"""

SAMPLE_DETAIL_HTML = """
<html>
<body>
    <div class="bookingDetails">
        <h3>Contact:</h3>
        <p>John Smith</p>
        <h3>Email:</h3>
        <p>john@example.com</p>
        <h3>Phone:</h3>
        <p>07700 900000</p>
        <h3>Address:</h3>
        <p>123 Church Street</p>
        <h3>Locality:</h3>
        <p>London</p>
        <h3>Postcode/Zip:</h3>
        <p>SW1A 1AA</p>
        <h3>Musical Requirements:</h3>
        <p>Traditional hymns and organ voluntaries</p>
    </div>
</body>
</html>
"""

INCOMPLETE_GIG_HTML = """
<html>
<body>
    <div class="booking">
        <h2 class="type">Funeral Service</h2>
        <h3 class="organisation">Community Church</h3>
    </div>
</body>
</html>
"""

EMPTY_DETAIL_HTML = """
<html>
<body>
    <div class="bookingDetails">
    </div>
</body>
</html>
"""


class TestScraperInitialization:
    """Test Scraper initialization and configuration."""

    def test_default_initialization(self):
        """Test Scraper initializes with default values."""
        scraper = Scraper()
        assert scraper.session.headers["User-Agent"] == "OrganistBot/1.0"
        assert scraper.timeout == 10

    def test_custom_initialization(self):
        """Test Scraper initializes with custom values."""
        scraper = Scraper(user_agent="CustomBot/2.0", timeout=20)
        assert scraper.session.headers["User-Agent"] == "CustomBot/2.0"
        assert scraper.timeout == 20

    def test_session_exists(self):
        """Test that a requests session is created."""
        scraper = Scraper()
        assert scraper.session is not None


class TestScraperFetch:
    """Test the fetch method with various scenarios."""

    @patch("organist_bot.scraper.requests.Session.get")
    def test_fetch_success(self, mock_get):
        """Test successful fetch of HTML content."""
        mock_response = Mock()
        mock_response.text = "<html><body>Test</body></html>"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        scraper = Scraper()
        result = scraper.fetch("https://example.com")

        assert result == "<html><body>Test</body></html>"
        mock_get.assert_called_once_with("https://example.com", timeout=10)

    @patch("organist_bot.scraper.requests.Session.get")
    def test_fetch_with_custom_timeout(self, mock_get):
        """Test fetch uses custom timeout."""
        mock_response = Mock()
        mock_response.text = "content"
        mock_response.raise_for_status = Mock()
        mock_get.return_value = mock_response

        scraper = Scraper(timeout=30)
        scraper.fetch("https://example.com")

        mock_get.assert_called_once_with("https://example.com", timeout=30)

    @patch("organist_bot.scraper.requests.Session.get")
    def test_fetch_http_error(self, mock_get):
        """Test fetch handles HTTP errors with retry."""
        mock_get.side_effect = HTTPError("404 Not Found")

        scraper = Scraper()
        with pytest.raises(RetryError):
            scraper.fetch("https://example.com/notfound")

    @patch("organist_bot.scraper.requests.Session.get")
    def test_fetch_timeout_error(self, mock_get):
        """Test fetch handles timeout errors with retry."""
        mock_get.side_effect = Timeout("Connection timeout")

        scraper = Scraper()
        with pytest.raises(RetryError):
            scraper.fetch("https://example.com")

    @patch("organist_bot.scraper.requests.Session.get")
    def test_fetch_retry_then_success(self, mock_get):
        """Test fetch retries and eventually succeeds."""
        mock_response = Mock()
        mock_response.text = "success"
        mock_response.raise_for_status = Mock()

        # Fail twice, then succeed
        mock_get.side_effect = [Timeout("timeout"), RequestException("error"), mock_response]

        scraper = Scraper()
        result = scraper.fetch("https://example.com")

        assert result == "success"
        assert mock_get.call_count == 3


class TestScraperParseGigListings:
    """Test parsing of gig listings from HTML."""

    def test_parse_multiple_listings(self):
        """Test parsing multiple gig listings."""
        scraper = Scraper()
        results = scraper.parse_gig_listings(SAMPLE_GIG_HTML, "booking")

        assert len(results) == 2
        assert results[0].find("h2", class_="type").text == "Wedding Service"
        assert results[1].find("h2", class_="type").text == "Sunday Service"

    def test_parse_no_listings(self):
        """Test parsing HTML with no matching listings."""
        scraper = Scraper()
        results = scraper.parse_gig_listings("<html><body></body></html>", "booking")

        assert len(results) == 0

    def test_parse_single_listing(self):
        """Test parsing HTML with single listing."""
        scraper = Scraper()
        results = scraper.parse_gig_listings(INCOMPLETE_GIG_HTML, "booking")

        assert len(results) == 1
        assert results[0].find("h2", class_="type").text == "Funeral Service"

    def test_parse_detail_page(self):
        """Test parsing detail page HTML."""
        scraper = Scraper()
        results = scraper.parse_gig_listings(SAMPLE_DETAIL_HTML, "bookingDetails")

        assert len(results) == 1


class TestScraperExtractBasicDetails:
    """Test extraction of basic gig details."""

    def test_extract_complete_details(self):
        """Test extraction of all basic details from complete listing."""
        scraper = Scraper()
        soup = BeautifulSoup(SAMPLE_GIG_HTML, "html.parser")
        booking = soup.find("div", class_="booking")

        details = scraper.extract_basic_details(booking)

        assert details["header"] == "Wedding Service"
        assert details["organisation"] == "St. Mary's Church"
        assert details["locality"] == "London"
        assert details["date"] == "15 March 2026"
        assert details["time"] == "14:00"
        assert details["fee"] == "GBP 150"
        assert details["link"] == "https://organistsonline.org/gig/123"

    def test_extract_incomplete_details(self):
        """Test extraction handles missing fields gracefully."""
        scraper = Scraper()
        soup = BeautifulSoup(INCOMPLETE_GIG_HTML, "html.parser")
        booking = soup.find("div", class_="booking")

        details = scraper.extract_basic_details(booking)

        assert details["header"] == "Funeral Service"
        assert details["organisation"] == "Community Church"
        assert details["locality"] is None
        assert details["date"] is None
        assert details["time"] is None
        assert details["fee"] is None
        assert details["link"] is None

    def test_extract_without_link(self):
        """Test extraction when no link is present."""
        html = '<div class="booking"><h2 class="type">Test</h2></div>'
        scraper = Scraper()
        soup = BeautifulSoup(html, "html.parser")
        booking = soup.find("div", class_="booking")

        details = scraper.extract_basic_details(booking)

        assert details["link"] is None


class TestScraperExtractFullDetails:
    """Test extraction of full gig details from detail page."""

    def test_extract_complete_full_details(self):
        """Test extraction of all detail fields."""
        scraper = Scraper()
        details = scraper.extract_full_details(SAMPLE_DETAIL_HTML)

        assert details["contact"] == "John Smith"
        assert details["email"] == "john@example.com"
        assert details["phone"] == "07700 900000"
        assert details["address"] == "123 Church Street"
        assert details["locality"] == "London"
        assert details["postcode"] == "SW1A 1AA"
        assert details["musical_requirements"] == "Traditional hymns and organ voluntaries"

    def test_extract_empty_full_details(self):
        """Test extraction from page with no detail fields."""
        scraper = Scraper()
        details = scraper.extract_full_details(EMPTY_DETAIL_HTML)

        assert details["contact"] is None
        assert details["email"] is None
        assert details["phone"] is None
        assert details["address"] is None
        assert details["locality"] is None
        assert details["postcode"] is None
        assert details["musical_requirements"] is None

    def test_extract_no_booking_details_element(self):
        """Test extraction when no bookingDetails div exists."""
        scraper = Scraper()
        details = scraper.extract_full_details("<html><body></body></html>")

        assert details == {}

    def test_extract_partial_full_details(self):
        """Test extraction with some missing fields."""
        partial_html = """
        <html>
        <body>
            <div class="bookingDetails">
                <h3>Contact:</h3>
                <p>Jane Doe</p>
                <h3>Email:</h3>
                <p>jane@example.com</p>
            </div>
        </body>
        </html>
        """
        scraper = Scraper()
        details = scraper.extract_full_details(partial_html)

        assert details["contact"] == "Jane Doe"
        assert details["email"] == "jane@example.com"
        assert details["phone"] is None
        assert details["musical_requirements"] is None


class TestScraperHelperMethods:
    """Test static helper methods."""

    def test_get_text_found(self):
        """Test _get_text when element exists."""
        html = '<div><p class="test">Hello World</p></div>'
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find("div")

        result = Scraper._get_text(element, "p", "test")
        assert result == "Hello World"

    def test_get_text_not_found(self):
        """Test _get_text when element doesn't exist."""
        html = '<div><p class="other">Hello</p></div>'
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find("div")

        result = Scraper._get_text(element, "p", "test")
        assert result is None

    def test_get_text_strips_whitespace(self):
        """Test _get_text strips leading/trailing whitespace."""
        html = '<div><p class="test">  Spaced Text  </p></div>'
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find("div")

        result = Scraper._get_text(element, "p", "test")
        assert result == "Spaced Text"

    def test_get_sibling_text_found(self):
        """Test _get_sibling_text when sibling exists."""
        html = "<div><h3>Label:</h3><p>Value</p></div>"
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find("div")

        result = Scraper._get_sibling_text(element, "Label:")
        assert result == "Value"

    def test_get_sibling_text_not_found(self):
        """Test _get_sibling_text when label doesn't exist."""
        html = "<div><h3>Other:</h3><p>Value</p></div>"
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find("div")

        result = Scraper._get_sibling_text(element, "Label:")
        assert result is None

    def test_get_sibling_text_no_sibling(self):
        """Test _get_sibling_text when label exists but no sibling."""
        html = "<div><h3>Label:</h3></div>"
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find("div")

        result = Scraper._get_sibling_text(element, "Label:")
        assert result is None

    def test_get_sibling_text_strips_whitespace(self):
        """Test _get_sibling_text strips whitespace."""
        html = "<div><h3>Label:</h3><p>  Spaced  </p></div>"
        soup = BeautifulSoup(html, "html.parser")
        element = soup.find("div")

        result = Scraper._get_sibling_text(element, "Label:")
        assert result == "Spaced"


class TestScraperContextManager:
    """Test Scraper as context manager."""

    def test_context_manager_enter(self):
        """Test __enter__ returns self."""
        scraper = Scraper()
        with scraper as s:
            assert s is scraper

    def test_context_manager_exit_closes_session(self):
        """Test __exit__ closes session."""
        scraper = Scraper()
        scraper.session.close = Mock()

        with scraper:
            pass

        scraper.session.close.assert_called_once()

    def test_context_manager_with_exception(self):
        """Test session closes even when exception occurs."""
        scraper = Scraper()
        scraper.session.close = Mock()

        try:
            with scraper:
                raise ValueError("Test error")
        except ValueError:
            pass

        scraper.session.close.assert_called_once()


class TestScraperIntegration:
    """Integration tests combining multiple methods."""

    def test_full_workflow_basic_to_full_details(self):
        """Test complete workflow from fetching to extracting details."""
        scraper = Scraper()

        # Parse basic listing
        listings = scraper.parse_gig_listings(SAMPLE_GIG_HTML, "booking")
        basic_details = scraper.extract_basic_details(listings[0])

        # Extract full details
        full_details = scraper.extract_full_details(SAMPLE_DETAIL_HTML)

        # Combine
        complete_gig = {**basic_details, **full_details}

        assert complete_gig["header"] == "Wedding Service"
        assert complete_gig["organisation"] == "St. Mary's Church"
        assert complete_gig["contact"] == "John Smith"
        assert complete_gig["email"] == "john@example.com"

    @patch("organist_bot.scraper.requests.Session.get")
    def test_workflow_with_mocked_fetch(self, mock_get):
        """Test workflow with mocked HTTP requests."""
        # Mock the fetch responses
        list_response = Mock()
        list_response.text = SAMPLE_GIG_HTML
        list_response.raise_for_status = Mock()

        detail_response = Mock()
        detail_response.text = SAMPLE_DETAIL_HTML
        detail_response.raise_for_status = Mock()

        mock_get.side_effect = [list_response, detail_response]

        scraper = Scraper()

        # Fetch and parse listing page
        list_html = scraper.fetch("https://organistsonline.org/required/")
        listings = scraper.parse_gig_listings(list_html, "booking")
        basic_details = scraper.extract_basic_details(listings[0])

        # Fetch and parse detail page
        detail_html = scraper.fetch(basic_details["link"])
        full_details = scraper.extract_full_details(detail_html)

        assert basic_details["organisation"] == "St. Mary's Church"
        assert full_details["contact"] == "John Smith"
        assert mock_get.call_count == 2
