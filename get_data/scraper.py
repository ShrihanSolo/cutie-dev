import argparse
import datetime
import json
import os
import re
import sys
import time
from typing import Dict, Tuple, List, Optional
import base64

# `requests` and `bs4` are imported lazily inside fetch_crossword() so that
# solve_crossword() (and its tests) can run without those dependencies.


# ============================================================
#  Crossword Grid Solver (general backtracking algorithm)
# ============================================================

def _parse_numbered(d: Dict[str, str]) -> Dict[int, str]:
    """Convert {"1A": "WATER", ...} -> {1: "WATER", ...} (uppercased)."""
    out: Dict[int, str] = {}
    for k, v in d.items():
        digits = ''.join(ch for ch in k if ch.isdigit())
        if not digits:
            continue
        out[int(digits)] = v.upper()
    return out


def _compute_numbering(grid: List[List[str]]) -> Tuple[Dict[int, int], Dict[int, int]]:
    """
    Apply standard crossword numbering to a solved grid.

    A cell is numbered if it starts an across run (length >= 2) and/or a down
    run (length >= 2). Numbers are assigned in reading order.

    Returns (across_lengths, down_lengths), each mapping clue number -> run length.
    """
    H, W = len(grid), len(grid[0])
    across: Dict[int, int] = {}
    down: Dict[int, int] = {}
    n = 0
    for r in range(H):
        for c in range(W):
            if grid[r][c] == '.':
                continue
            starts_across = (
                (c == 0 or grid[r][c - 1] == '.')
                and c + 1 < W and grid[r][c + 1] != '.'
            )
            starts_down = (
                (r == 0 or grid[r - 1][c] == '.')
                and r + 1 < H and grid[r + 1][c] != '.'
            )
            if not (starts_across or starts_down):
                continue
            n += 1
            if starts_across:
                length = 0
                while c + length < W and grid[r][c + length] != '.':
                    length += 1
                across[n] = length
            if starts_down:
                length = 0
                while r + length < H and grid[r + length][c] != '.':
                    length += 1
                down[n] = length
    return across, down


class _SolveTimeout(Exception):
    """Raised internally to abort solve_crossword when the time budget runs out."""


def solve_crossword(across_dict: Dict[str, str], down_dict: Dict[str, str],
                    max_dim: int = 12,
                    time_limit: float = 5.0) -> Optional[List[List[str]]]:
    """
    Given across and down answers (with keys like "1A", "2D"),
    reconstruct the crossword grid.

    Works for any rectangular grid size (5x5, 7x7, 4x6, 8x8, ...) and any
    black-square layout. The grid dimensions are discovered automatically.

    Bad input (e.g. a clue mislabeled by the scraper) can have no valid grid.
    The solver is guaranteed to terminate: it fails fast on structurally
    invalid input and otherwise aborts after `time_limit` seconds.

    Returns:
        The grid as a list of lists of str, with "." for black squares,
        or None if no valid grid is found (or the time budget is exceeded).
    """
    across = _parse_numbered(across_dict)   # {num: word}
    down = _parse_numbered(down_dict)

    if not across and not down:
        return None

    numbers = sorted(set(across) | set(down))
    # Standard crossword numbering is contiguous (1..N): every numbered cell
    # gets the next integer. Non-contiguous clue numbers mean the input is
    # inconsistent -- e.g. CNET mislabeling "6 Down" as "5 Down" -- so no valid
    # grid exists. Reject it here instead of searching the space fruitlessly.
    if numbers != list(range(1, len(numbers) + 1)):
        return None

    max_a = max((len(w) for w in across.values()), default=2)
    max_d = max((len(w) for w in down.values()), default=2)

    deadline = time.monotonic() + time_limit
    nodes = [0]

    def try_dims(H: int, W: int) -> Optional[List[List[str]]]:
        letters: List[List[Optional[str]]] = [[None] * W for _ in range(H)]

        def place(cells: List[Tuple[int, int, str]]) -> Optional[List[Tuple[int, int]]]:
            """Place letters; return newly-set cells, or None on conflict."""
            newly: List[Tuple[int, int]] = []
            for r, c, ch in cells:
                cur = letters[r][c]
                if cur is None:
                    letters[r][c] = ch
                    newly.append((r, c))
                elif cur != ch:
                    for rr, cc in newly:
                        letters[rr][cc] = None
                    return None
            return newly

        def validate() -> Optional[List[List[str]]]:
            grid = [['.' if letters[r][c] is None else letters[r][c]
                     for c in range(W)] for r in range(H)]
            # Reject grids with an all-black border (dimensions not tight).
            if not any(grid[0][c] != '.' for c in range(W)):
                return None
            if not any(grid[H - 1][c] != '.' for c in range(W)):
                return None
            if not any(grid[r][0] != '.' for r in range(H)):
                return None
            if not any(grid[r][W - 1] != '.' for r in range(H)):
                return None
            # Induced numbering must exactly match the given clues.
            af, df = _compute_numbering(grid)
            if set(af) != set(across) or set(df) != set(down):
                return None
            if any(af[k] != len(across[k]) for k in across):
                return None
            if any(df[k] != len(down[k]) for k in down):
                return None
            return grid

        # Assign each clue number a start cell, monotonically increasing in
        # reading order (standard crossword numbering is row-major).
        def assign(i: int, prev_idx: int) -> Optional[List[List[str]]]:
            nodes[0] += 1
            if nodes[0] % 4096 == 0 and time.monotonic() > deadline:
                raise _SolveTimeout
            if i == len(numbers):
                return validate()
            num = numbers[i]
            aw = across.get(num)
            dw = down.get(num)
            for r in range(H):
                for c in range(W):
                    idx = r * W + c
                    if idx <= prev_idx:
                        continue
                    if aw is not None and c + len(aw) > W:
                        continue
                    if dw is not None and r + len(dw) > H:
                        continue
                    # Boundary pruning: a numbered cell starts a word, so the
                    # cell just before it (and just after the word) cannot
                    # already hold a letter -- that would extend the run.
                    if aw is not None:
                        if c > 0 and letters[r][c - 1] is not None:
                            continue
                        if c + len(aw) < W and letters[r][c + len(aw)] is not None:
                            continue
                    if dw is not None:
                        if r > 0 and letters[r - 1][c] is not None:
                            continue
                        if r + len(dw) < H and letters[r + len(dw)][c] is not None:
                            continue
                    cells: List[Tuple[int, int, str]] = []
                    if aw is not None:
                        cells += [(r, c + k, aw[k]) for k in range(len(aw))]
                    if dw is not None:
                        cells += [(r + k, c, dw[k]) for k in range(len(dw))]
                    newly = place(cells)
                    if newly is None:
                        continue
                    result = assign(i + 1, idx)
                    if result is not None:
                        return result
                    for rr, cc in newly:
                        letters[rr][cc] = None
            return None

        return assign(0, -1)

    # Search dimensions, smallest area first so the tightest grid wins.
    dims = sorted(
        ((H, W) for H in range(max_d, max_dim + 1)
         for W in range(max_a, max_dim + 1)),
        key=lambda hw: (hw[0] * hw[1], hw[0], hw[1]),
    )
    try:
        for H, W in dims:
            grid = try_dims(H, W)
            if grid is not None:
                return grid
    except _SolveTimeout:
        return None
    return None


# ============================================================
#  Article date verification
# ============================================================

# CNET's URL carries no year ("...-for-friday-aug-21/"), and old articles stay
# live forever. A date's weekday shifts by one each year, so when CNET has not
# published the year we want, the same slug can resolve to a PREVIOUS year's
# puzzle -- a 200 with entirely wrong answers. Cross-check the page's own
# publication date before trusting it.

_DATE_META_PATTERNS = (
    re.compile(r'"date(?:Published|Modified)"\s*:\s*"(\d{4}-\d{2}-\d{2})', re.I),
    re.compile(r'article:(?:published|modified)_time"[^>]*content="(\d{4}-\d{2}-\d{2})', re.I),
    re.compile(r'content="(\d{4}-\d{2}-\d{2})[^"]*"[^>]*property="article:(?:published|modified)_time', re.I),
    re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})', re.I),
)

# CNET publishes the next day's answers the evening before, so the article date
# legitimately trails the puzzle date. A wrong-year article is off by ~365 days,
# leaving enormous margin.
_DATE_TOLERANCE_DAYS = 2


def article_dates(html: str) -> List[str]:
    """Every publication-ish date (YYYY-MM-DD) the page advertises, in order."""
    found: List[str] = []
    for pattern in _DATE_META_PATTERNS:
        for match in pattern.findall(html):
            if match not in found:
                found.append(match)
    return found


def check_article_date(html: str, date: str) -> Optional[str]:
    """
    Verify the page belongs to `date`.

    Returns None when the page carries no usable date (nothing is claimed, so
    nothing is checked), otherwise the matching date. Raises RuntimeError when
    the page advertises dates and none of them is close to the one requested.
    """
    wanted = datetime.datetime.strptime(date, "%Y-%m-%d")
    candidates = article_dates(html)
    if not candidates:
        return None

    for candidate in candidates:
        try:
            delta = (datetime.datetime.strptime(candidate, "%Y-%m-%d") - wanted).days
        except ValueError:
            continue
        if abs(delta) <= _DATE_TOLERANCE_DAYS:
            return candidate

    raise RuntimeError(
        f"Page for {date} advertises {', '.join(candidates[:3])} -- CNET's slug "
        f"carries no year, so this is almost certainly a different year's puzzle. "
        f"Refusing it."
    )


# ============================================================
#  Clue paragraph parsing
# ============================================================

# CNET renders each clue as a single <p>. It has emitted the answer both
# inside the bold label and outside it:
#     <strong>1A Clue:</strong> Spider ___ <strong>Answer: WEB</strong>
#     <strong>1A Clue:</strong> Spider ___ <strong>Answer:</strong> WEB   <- since 2026-07-23
# Reading the answer out of the <strong> tag yielded "" for every clue under
# the second form, so solve_crossword() had nothing to work with and every
# puzzle was saved with "grid": null for ~2 months. Parse the paragraph's
# plain text instead, so the answer is found wherever the tag boundaries fall.

_ANSWER_RE = re.compile(r"answer\s*:", re.I)
_CLUE_RE = re.compile(r"(\d+)\s*([AD])\b\s*(?:clue\s*:)?\s*(.*)", re.I | re.S)


def parse_clue_paragraph(full_text: str) -> Optional[Tuple[str, str, str, str]]:
    """
    Parse one clue paragraph's text into (number, direction, clue, answer).

    Returns None when the text is not a numbered clue carrying an answer, so
    callers can skip the page's ordinary prose paragraphs.

    >>> parse_clue_paragraph("1A Clue: Spider ___ Answer: WEB")
    ('1', 'across', 'Spider ___', 'WEB')
    >>> parse_clue_paragraph("Here are today's answers.") is None
    True
    """
    parts = _ANSWER_RE.split(full_text, maxsplit=1)
    if len(parts) != 2:
        return None

    # Answers may be multiple words ("ALL IN" -> "ALLIN"); an empty or
    # non-alphabetic result means the parse went wrong, so drop the clue.
    answer = "".join(parts[1].split()).upper()
    if not answer.isalpha():
        return None

    m = _CLUE_RE.match(parts[0].strip())
    if not m:
        return None

    number, letter, clue = m.group(1), m.group(2).upper(), m.group(3).strip()
    if not clue:
        return None

    return number, "across" if letter == "A" else "down", clue, answer


# ============================================================
#  Scraper for one crossword
# ============================================================

def fetch_crossword(date: str) -> Dict:
    """
    Fetch crossword data for a given date, reconstruct the grid,
    and return a JSON-serializable dictionary.
    """

    import requests
    from bs4 import BeautifulSoup

    # --- Build URL for the date ---
    date_obj = datetime.datetime.strptime(date, "%Y-%m-%d")
    day = str(int(date_obj.strftime("%d"))).lower()
    day_of_week = date_obj.strftime("%A").lower()
    month = date_obj.strftime("%B").lower()
    if len(month) > 5:
        month = date_obj.strftime("%b").lower()
        if month == "sep":
            month = "sept"

    url = f"https://www.cnet.com/tech/gaming/todays-nyt-mini-crossword-answers-for-{day_of_week}-{month}-{day}/"

    # --- Fetch page ---
    response = requests.get(url)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch {url} (status {response.status_code})")

    # Guard against landing on a previous year's article (see above).
    if check_article_date(response.text, date) is None:
        print(f"ℹ️  {date}: page carries no publication date; year check skipped")

    soup = BeautifulSoup(response.text, "html.parser")

    # --- Parse clues + answers ---
    clues_across, clues_down, answers = {}, {}, {}
    for p in soup.find_all("p"):
        parsed = parse_clue_paragraph(p.get_text(separator=" ", strip=True))
        if parsed is None:
            continue
        number, direction, clue_text, answer_text = parsed
        if direction == "across":
            clues_across[number] = clue_text
            answers[f"{number}A"] = answer_text
        else:
            clues_down[number] = clue_text
            answers[f"{number}D"] = answer_text

    # Fail loudly instead of returning a puzzle with no answers. Silently
    # parsing zero answers is exactly how the 2026-07-23 markup change turned
    # into two months of gridless puzzle files.
    if not answers:
        raise RuntimeError(
            f"No clues parsed from {url} -- the page layout has probably changed."
        )

    # --- Split answers into across/down dicts ---
    a_dict = {k: v for k, v in answers.items() if k.endswith("A")}
    d_dict = {k: v for k, v in answers.items() if k.endswith("D")}

    # --- Solve for the grid ---
    grid = solve_crossword(a_dict, d_dict)

    # print(url)
    # print(clues_across)
    # print(clues_down)
    # print(answers)

    return {
        "date": date,
        "grid": grid,
        "clues": {
            "across": clues_across,
            "down": clues_down
        }
    }


# ============================================================
#  Batch fetcher for multiple days
# ============================================================

def _existing_has_grid(path: str) -> bool:
    """True if `path` already holds a puzzle with a usable grid."""
    try:
        with open(path) as f:
            return bool(json.loads(base64.b64decode(f.read())).get("grid"))
    except Exception:
        return False


def fetch_crosswords_for_past_n_days(start_date: str, n_days: int = 7, save_folder: str = ".",
                                     delay: float = 1.0, skip_existing: bool = False) -> int:
    """
    Fetch and save crosswords for the past N days starting from `start_date`.

    Args:
        start_date: Starting date in YYYY-MM-DD format.
        n_days: How many days back to fetch (inclusive).
        save_folder: Directory to save JSON files.
        delay: Seconds to wait between requests, so a long backfill does not
            hammer CNET.
        skip_existing: Don't re-fetch dates that already have a solved puzzle.

    Returns:
        How many dates were fetched successfully.
    """

    os.makedirs(save_folder, exist_ok=True)
    succeeded = 0
    start_date_obj = datetime.datetime.strptime(start_date, "%Y-%m-%d")

    for delta in range(0, n_days + 1):
        current_date = start_date_obj - datetime.timedelta(days=delta)
        date_str = current_date.strftime("%Y-%m-%d")

        file_path = f"{save_folder}/{date_str}.json"
        if skip_existing and _existing_has_grid(file_path):
            print(f"⏭️  Skipping {date_str} (already solved)")
            continue

        if delay and delta:
            time.sleep(delay)

        try:
            crossword_data = fetch_crossword(date_str)
            # Convert dict → JSON string
            json_str = json.dumps(crossword_data)

            # Encode JSON string → Base64
            encoded = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")

            # Never replace a good puzzle with a gridless one: this fetcher
            # re-visits the last few days on every run.
            if crossword_data["grid"] is None and _existing_has_grid(file_path):
                print(f"⏭️  Kept existing {file_path} (solver failed this run)")
                continue

            # Save the encoded string directly (not as JSON object, just text)
            with open(file_path, "w") as f:
                f.write(encoded)

            if crossword_data["grid"] is None:
                print(f"⚠️  Saved {file_path} WITHOUT a grid "
                      f"(clues scraped, but solve_crossword could not "
                      f"reconstruct the grid)")
            else:
                print(f"✅ Saved {file_path}")
            succeeded += 1
        except Exception as e:
            print(f"❌ Failed for {date_str}: {e}")

    return succeeded


def main(argv=None) -> int:
    # The scraper runs from CI at the repo root, but resolve the output folder
    # from this file's location so it also works when run from get_data/.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    parser = argparse.ArgumentParser(
        description="Scrape NYT Mini crossword puzzles into puzzles/.",
        epilog="With no arguments: fetch tomorrow plus the previous 3 days "
               "(what the daily workflow runs).",
    )
    parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD",
                        help="Oldest date to fetch. Switches to backfill mode.")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD",
                        help="Newest date to fetch in backfill mode "
                             "(default: today).")
    parser.add_argument("--only-missing", action="store_true",
                        help="Skip dates that already have a solved puzzle.")
    parser.add_argument("--delay", type=float, default=1.0, metavar="SECONDS",
                        help="Pause between requests (default: 1.0).")
    parser.add_argument("--out", default=os.path.join(repo_root, "puzzles"),
                        metavar="DIR", help="Output folder (default: puzzles/).")
    args = parser.parse_args(argv)

    if args.to_date and not args.from_date:
        parser.error("--to requires --from")

    if args.from_date:
        # --- Backfill mode: an explicit date range, oldest bound given. ---
        end = args.to_date or datetime.datetime.now().strftime("%Y-%m-%d")
        start_obj = datetime.datetime.strptime(args.from_date, "%Y-%m-%d")
        end_obj = datetime.datetime.strptime(end, "%Y-%m-%d")
        span = (end_obj - start_obj).days
        if span < 0:
            parser.error(f"--from ({args.from_date}) is after --to ({end})")

        print(f"Backfilling {args.from_date} .. {end} ({span + 1} dates)")
        fetched = fetch_crosswords_for_past_n_days(
            end, span, args.out, delay=args.delay,
            skip_existing=args.only_missing,
        )
        print(f"\nFetched {fetched} of {span + 1} dates.")
        return 0

    # --- Daily mode: CNET publishes the next day's answers the evening
    # before, so start from tomorrow and walk back a few days to fill in
    # anything previously missed. ---
    date = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    fetched = fetch_crosswords_for_past_n_days(date, 3, args.out, delay=args.delay)

    # Exit non-zero when nothing at all was fetched, so a silent upstream
    # change shows up as a failed workflow run instead of months of quiet
    # no-ops.
    if fetched == 0:
        print("❌ No puzzles fetched for any date -- failing the run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
