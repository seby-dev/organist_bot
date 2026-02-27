# tests/test_storage.py

import csv
import pytest
from pathlib import Path

from organist_bot.storage import load_seen_gigs, save_seen_gigs


# ─────────────────────────────────────────────────────────
# load_seen_gigs
# ─────────────────────────────────────────────────────────

class TestLoadSeenGigs:

    def test_returns_empty_set_when_file_missing(self, tmp_path):
        """No file → empty set, no exception raised."""
        result = load_seen_gigs(str(tmp_path / "nonexistent.csv"))
        assert result == set()

    def test_returns_set_type(self, tmp_path):
        """Always returns a set, even for an empty file."""
        empty_file = tmp_path / "seen.csv"
        empty_file.write_text("")
        assert isinstance(load_seen_gigs(str(empty_file)), set)

    def test_empty_file_returns_empty_set(self, tmp_path):
        """An existing but empty file yields an empty set."""
        p = tmp_path / "seen.csv"
        p.write_text("")
        assert load_seen_gigs(str(p)) == set()

    def test_single_link_loaded(self, tmp_path):
        """A file with one entry returns a set with that one link."""
        p = tmp_path / "seen.csv"
        p.write_text("https://example.com/gig/1\n")
        assert load_seen_gigs(str(p)) == {"https://example.com/gig/1"}

    def test_multiple_links_loaded(self, tmp_path):
        """Multiple rows are all loaded into the set."""
        p = tmp_path / "seen.csv"
        p.write_text(
            "https://example.com/gig/1\n"
            "https://example.com/gig/2\n"
            "https://example.com/gig/3\n"
        )
        assert load_seen_gigs(str(p)) == {
            "https://example.com/gig/1",
            "https://example.com/gig/2",
            "https://example.com/gig/3",
        }

    def test_blank_rows_are_skipped(self, tmp_path):
        """Empty rows in the CSV do not produce empty-string entries."""
        p = tmp_path / "seen.csv"
        p.write_text(
            "https://example.com/gig/1\n"
            "\n"
            "https://example.com/gig/2\n"
            "\n"
        )
        result = load_seen_gigs(str(p))
        assert "" not in result
        assert result == {"https://example.com/gig/1", "https://example.com/gig/2"}

    def test_duplicate_entries_deduplicated_by_set(self, tmp_path):
        """Duplicate rows collapse into a single set member."""
        p = tmp_path / "seen.csv"
        p.write_text(
            "https://example.com/gig/1\n"
            "https://example.com/gig/1\n"
        )
        result = load_seen_gigs(str(p))
        assert result == {"https://example.com/gig/1"}
        assert len(result) == 1

    def test_only_first_column_used(self, tmp_path):
        """Extra CSV columns beyond the first are ignored."""
        p = tmp_path / "seen.csv"
        writer_rows = [
            ["https://example.com/gig/1", "extra_col", "another"],
            ["https://example.com/gig/2", "metadata"],
        ]
        with p.open("w", newline="") as fh:
            csv.writer(fh).writerows(writer_rows)
        result = load_seen_gigs(str(p))
        assert result == {"https://example.com/gig/1", "https://example.com/gig/2"}

    def test_default_filepath_does_not_raise_when_missing(self, monkeypatch, tmp_path):
        """Calling with no arguments falls back to default path without crashing."""
        # Redirect cwd so the default 'seen_gigs.csv' is definitely absent
        monkeypatch.chdir(tmp_path)
        result = load_seen_gigs()
        assert result == set()

    def test_links_with_special_characters(self, tmp_path):
        """Links containing query strings and fragments load correctly."""
        link = "https://example.com/gig/1?ref=test&id=42#section"
        p = tmp_path / "seen.csv"
        p.write_text(link + "\n")
        assert load_seen_gigs(str(p)) == {link}


# ─────────────────────────────────────────────────────────
# save_seen_gigs
# ─────────────────────────────────────────────────────────

class TestSaveSeenGigs:

    def test_creates_file_when_absent(self, tmp_path):
        """Saving creates the CSV file if it does not exist yet."""
        p = tmp_path / "seen.csv"
        assert not p.exists()
        save_seen_gigs({"https://example.com/gig/1"}, str(p))
        assert p.exists()

    def test_empty_set_writes_empty_file(self, tmp_path):
        """Saving an empty set produces an empty file (no rows)."""
        p = tmp_path / "seen.csv"
        save_seen_gigs(set(), str(p))
        assert p.read_text() == ""

    def test_single_link_written(self, tmp_path):
        """A single link is written as one CSV row."""
        p = tmp_path / "seen.csv"
        save_seen_gigs({"https://example.com/gig/1"}, str(p))
        rows = p.read_text().strip().splitlines()
        assert rows == ["https://example.com/gig/1"]

    def test_multiple_links_written(self, tmp_path):
        """All links in the set are written, one per row."""
        p = tmp_path / "seen.csv"
        links = {
            "https://example.com/gig/1",
            "https://example.com/gig/2",
            "https://example.com/gig/3",
        }
        save_seen_gigs(links, str(p))
        written = {row[0] for row in csv.reader(p.open())}
        assert written == links

    def test_links_written_in_sorted_order(self, tmp_path):
        """Rows are written in lexicographic order for deterministic output."""
        p = tmp_path / "seen.csv"
        links = {
            "https://example.com/gig/3",
            "https://example.com/gig/1",
            "https://example.com/gig/2",
        }
        save_seen_gigs(links, str(p))
        rows = [r[0] for r in csv.reader(p.open())]
        assert rows == sorted(links)

    def test_overwrites_existing_file(self, tmp_path):
        """A second save replaces the first — no stale entries remain."""
        p = tmp_path / "seen.csv"
        save_seen_gigs({"https://example.com/old"}, str(p))
        save_seen_gigs({"https://example.com/new"}, str(p))
        result = load_seen_gigs(str(p))
        assert result == {"https://example.com/new"}
        assert "https://example.com/old" not in result

    def test_default_filepath_writes_to_data_dir(self, monkeypatch, tmp_path):
        """Omitting filepath writes to 'data/seen_gigs.csv' relative to cwd."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        save_seen_gigs({"https://example.com/gig/1"})
        assert (tmp_path / "data" / "seen_gigs.csv").exists()

    def test_links_with_special_characters_round_trip(self, tmp_path):
        """Links with query strings and fragments survive a save/load cycle."""
        p = tmp_path / "seen.csv"
        link = "https://example.com/gig/1?ref=test&id=42#section"
        save_seen_gigs({link}, str(p))
        assert load_seen_gigs(str(p)) == {link}


# ─────────────────────────────────────────────────────────
# Round-trip
# ─────────────────────────────────────────────────────────

class TestRoundTrip:

    def test_save_then_load_returns_same_set(self, tmp_path):
        """Whatever is saved can be loaded back identically."""
        p = tmp_path / "seen.csv"
        original = {
            "https://example.com/gig/1",
            "https://example.com/gig/2",
            "https://example.com/gig/3",
        }
        save_seen_gigs(original, str(p))
        assert load_seen_gigs(str(p)) == original

    def test_empty_set_round_trips(self, tmp_path):
        """Saving and loading an empty set is stable."""
        p = tmp_path / "seen.csv"
        save_seen_gigs(set(), str(p))
        assert load_seen_gigs(str(p)) == set()

    def test_incremental_saves_do_not_accumulate(self, tmp_path):
        """Each save is a full overwrite; earlier entries don't bleed through."""
        p = tmp_path / "seen.csv"
        save_seen_gigs({"https://example.com/gig/1"}, str(p))
        save_seen_gigs({"https://example.com/gig/2"}, str(p))
        save_seen_gigs({"https://example.com/gig/3"}, str(p))
        assert load_seen_gigs(str(p)) == {"https://example.com/gig/3"}

    def test_large_set_round_trips(self, tmp_path):
        """A large number of links all survive a save/load cycle."""
        p = tmp_path / "seen.csv"
        links = {f"https://example.com/gig/{i}" for i in range(500)}
        save_seen_gigs(links, str(p))
        assert load_seen_gigs(str(p)) == links
