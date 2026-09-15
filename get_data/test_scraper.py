"""
Tests for the CNET clue parser. Run with:  python get_data/test_scraper.py

These need no network and no third-party packages: the fragile part of the
scraper is parse_clue_paragraph(), which works on the paragraph's plain text,
so it can be exercised directly.
"""

import base64
import doctest
import json
import os
import sys
import tempfile
import unittest
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scraper

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CNET has shipped the answer both inside the bold label and outside it. Both
# render to identical paragraph text, which is why parsing text rather than
# tags survives the switch.
OLD_MARKUP = '<p><strong>{key} Clue:</strong> {clue} <strong>Answer: {ans}</strong></p>'
NEW_MARKUP = '<p><strong>{key} Clue:</strong> {clue} <strong>Answer:</strong> {ans}</p>'


class _TextExtractor(HTMLParser):
    """Stand-in for BeautifulSoup's get_text(separator=" ", strip=True)."""

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def paragraph_text(html):
    parser = _TextExtractor()
    parser.feed(html)
    return " ".join(p.strip() for p in parser.parts if p.strip())


class ParseClueParagraphTests(unittest.TestCase):
    def test_both_markups_parse_identically(self):
        expected = ("1", "across", "Spider ___", "WEB")
        for name, tmpl in (("old", OLD_MARKUP), ("new", NEW_MARKUP)):
            with self.subTest(markup=name):
                text = paragraph_text(tmpl.format(key="1A", clue="Spider ___", ans="WEB"))
                self.assertEqual(scraper.parse_clue_paragraph(text), expected)

    def test_answer_does_not_leak_into_clue(self):
        """The 2026-07-23 regression stored clues as 'Spider ___  WEB'."""
        text = paragraph_text(NEW_MARKUP.format(key="1A", clue="Spider ___", ans="WEB"))
        _, _, clue, answer = scraper.parse_clue_paragraph(text)
        self.assertNotIn("WEB", clue)
        self.assertEqual(answer, "WEB")

    def test_multiple_bold_tags_in_clue(self):
        """'First letter of each word' clues bold every initial."""
        html = ('<p><strong>5D Clue:</strong> <strong>T</strong>unisia, '
                '<strong>H</strong>aiti, <strong>E</strong>gypt '
                '<strong>Answer: THE</strong></p>')
        parsed = scraper.parse_clue_paragraph(paragraph_text(html))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[3], "THE")

    def test_down_clues_and_multi_word_answers(self):
        text = paragraph_text(NEW_MARKUP.format(key="6D", clue="Maximum poker bet", ans="ALL IN"))
        self.assertEqual(scraper.parse_clue_paragraph(text), ("6", "down", "Maximum poker bet", "ALLIN"))

    def test_non_clue_paragraphs_are_skipped(self):
        for text in ("Here are today's answers.",
                     "Read more about the NYT Mini.",
                     "Hints and answers: scroll down",
                     "1A Clue: no answer here",
                     ""):
            with self.subTest(text=text):
                self.assertIsNone(scraper.parse_clue_paragraph(text))


class GridReconstructionTests(unittest.TestCase):
    """End-to-end check against a real puzzle from the archive."""

    PUZZLE = "2026-06-30.json"

    def setUp(self):
        path = os.path.join(REPO_ROOT, "puzzles", self.PUZZLE)
        if not os.path.exists(path):
            self.skipTest(f"{self.PUZZLE} not in archive")
        with open(path) as f:
            self.puzzle = json.loads(base64.b64decode(f.read()))
        if not self.puzzle.get("grid"):
            self.skipTest(f"{self.PUZZLE} has no grid")

    def _answers_from_grid(self, grid):
        height, width = len(grid), len(grid[0])
        numbers, answers, next_num = {}, {}, 1
        for r in range(height):
            for c in range(width):
                if grid[r][c] == ".":
                    continue
                starts_across = (c == 0 or grid[r][c - 1] == ".") and c + 1 < width and grid[r][c + 1] != "."
                starts_down = (r == 0 or grid[r - 1][c] == ".") and r + 1 < height and grid[r + 1][c] != "."
                if starts_across or starts_down:
                    numbers[(r, c)] = next_num
                    next_num += 1
                if starts_across:
                    word, cc = "", c
                    while cc < width and grid[r][cc] != ".":
                        word, cc = word + grid[r][cc], cc + 1
                    answers[f"{numbers[(r, c)]}A"] = word
                if starts_down:
                    word, rr = "", r
                    while rr < height and grid[rr][c] != ".":
                        word, rr = word + grid[rr][c], rr + 1
                    answers[f"{numbers[(r, c)]}D"] = word
        return answers

    def test_round_trip_through_both_markups(self):
        truth = self._answers_from_grid(self.puzzle["grid"])
        for name, tmpl in (("old", OLD_MARKUP), ("new", NEW_MARKUP)):
            with self.subTest(markup=name):
                parsed = {}
                for key, answer in truth.items():
                    direction = "across" if key.endswith("A") else "down"
                    clue = self.puzzle["clues"][direction].get(key[:-1], "clue")
                    text = paragraph_text(tmpl.format(key=key, clue=clue, ans=answer))
                    number, got_dir, _, got_answer = scraper.parse_clue_paragraph(text)
                    parsed[f"{number}{'A' if got_dir == 'across' else 'D'}"] = got_answer
                self.assertEqual(parsed, truth)
                grid = scraper.solve_crossword(
                    {k: v for k, v in parsed.items() if k.endswith("A")},
                    {k: v for k, v in parsed.items() if k.endswith("D")},
                )
                self.assertEqual(grid, self.puzzle["grid"])


class WriteGuardTests(unittest.TestCase):
    def test_gridless_result_does_not_clobber_good_file(self):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "2026-09-14.json")
        good = {"date": "2026-09-14", "grid": [["A"]], "clues": {"across": {}, "down": {}}}
        with open(path, "w") as f:
            f.write(base64.b64encode(json.dumps(good).encode()).decode())

        original = scraper.fetch_crossword
        scraper.fetch_crossword = lambda date: {"date": date, "grid": None,
                                                "clues": {"across": {"1": "x"}, "down": {}}}
        try:
            scraper.fetch_crosswords_for_past_n_days("2026-09-14", 0, folder)
        finally:
            scraper.fetch_crossword = original

        with open(path) as f:
            self.assertEqual(json.loads(base64.b64decode(f.read()))["grid"], [["A"]])


class ArticleDateTests(unittest.TestCase):
    """CNET's slug has no year, so an old article can answer for a new date."""

    JSON_LD = '<script type="application/ld+json">{"datePublished": "%sT21:00:00Z"}</script>'
    OG_META = '<meta property="article:published_time" content="%sT21:00:00Z"/>'
    TIME_TAG = '<time datetime="%s" class="c-date">whenever</time>'

    def test_accepts_matching_date(self):
        for name, tmpl in (("json-ld", self.JSON_LD), ("og", self.OG_META), ("time", self.TIME_TAG)):
            with self.subTest(source=name):
                self.assertEqual(scraper.check_article_date(tmpl % "2026-08-20", "2026-08-20"),
                                 "2026-08-20")

    def test_accepts_evening_before_publication(self):
        """CNET posts the next day's answers the night before."""
        self.assertEqual(scraper.check_article_date(self.JSON_LD % "2026-08-19", "2026-08-20"),
                         "2026-08-19")

    def test_rejects_previous_year(self):
        """The failure this guard exists for: thursday-aug-21 is 2025's puzzle."""
        with self.assertRaises(RuntimeError) as ctx:
            scraper.check_article_date(self.JSON_LD % "2025-08-21", "2026-08-21")
        self.assertIn("different year", str(ctx.exception))

    def test_passes_when_any_advertised_date_matches(self):
        """Pages embed dates for related articles; one good match is enough."""
        html = (self.JSON_LD % "2024-01-01") + (self.OG_META % "2026-08-20")
        self.assertEqual(scraper.check_article_date(html, "2026-08-20"), "2026-08-20")

    def test_returns_none_when_page_has_no_date(self):
        self.assertIsNone(scraper.check_article_date("<html><p>nothing</p></html>", "2026-08-20"))


class ExitCodeTests(unittest.TestCase):
    """The workflow relies on these to go red instead of quietly no-opping."""

    def _run(self, fetch_impl, argv):
        original = scraper.fetch_crossword
        scraper.fetch_crossword = fetch_impl
        try:
            return scraper.main(argv + ["--delay", "0", "--out", tempfile.mkdtemp()])
        finally:
            scraper.fetch_crossword = original

    def test_daily_run_fails_when_nothing_fetched(self):
        def boom(date):
            raise RuntimeError("404")
        self.assertEqual(self._run(boom, []), 1)

    def test_daily_run_succeeds_when_something_lands(self):
        def ok(date):
            return {"date": date, "grid": [["A"]], "clues": {"across": {}, "down": {}}}
        self.assertEqual(self._run(ok, []), 0)

    def test_batch_fetcher_returns_success_count(self):
        def ok(date):
            return {"date": date, "grid": [["A"]], "clues": {"across": {}, "down": {}}}
        original = scraper.fetch_crossword
        scraper.fetch_crossword = ok
        try:
            count = scraper.fetch_crosswords_for_past_n_days(
                "2026-09-14", 2, tempfile.mkdtemp(), delay=0)
        finally:
            scraper.fetch_crossword = original
        self.assertEqual(count, 3)


def load_tests(loader, tests, ignore):
    tests.addTests(doctest.DocTestSuite(scraper))
    return tests


if __name__ == "__main__":
    unittest.main(verbosity=2)
