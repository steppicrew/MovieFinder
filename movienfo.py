#!/usr/bin/env python3
"""movienfo — find media files without a matching .nfo, search TMDB, and write Kodi/TMM-style NFOs.

Scans a movie library for media files that have no sibling ``.nfo`` (using the
shared-basename rule so multi-part ``Movie.Part 1.mkv`` / ``Movie.Part 2.mkv``
share a single ``Movie.nfo``, matching the existing library convention).

For each movie without an NFO it parses the title + year from the filename,
searches TMDB, presents the candidates in the terminal for selection, then
fetches full details and writes an NFO in the same style as the existing files.

Config (via a ``.env`` file in the project dir, or real environment variables):
    TMDB_API_KEY   TMDB v3 API key. Also overridable with ``--api-key``.
    MOVIE_DIRS     One or more library roots to scan, separated by ``os.pathsep``
                   (``:`` on Linux/macOS). Overridable with ``--root`` (repeatable).

Usage:
    ./movienfo.py                       # scan dirs from .env / MOVIE_DIRS, interactive
    ./movienfo.py --root /path/to/movies [--root /another]
    ./movienfo.py --dry-run             # show what would be written, write nothing
    ./movienfo.py --only "Collateral"   # only movies whose path matches substring
    ./movienfo.py --list                # just list movies missing an NFO and exit
"""
from __future__ import annotations

import argparse
import configparser
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("The 'requests' package is required: pip install requests")

ENV_PATH = Path(__file__).resolve().parent / ".env"
CONFIG_PATH = Path.home() / ".config" / "movienfo" / "config.ini"

MEDIA_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".mpg", ".mpeg"}
PART_RE = re.compile(r"\.[Pp]art ?\d+$")
# Trailing "(YYYY)" (optionally followed by edition tags we ignore for the year).
YEAR_RE = re.compile(r"\((\d{4})\)")

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_ORIG = "https://image.tmdb.org/t/p/original"
IMG_W500 = "https://image.tmdb.org/t/p/w500"

# Map a top-level library subfolder to a TMDB language for metadata.
LANG_BY_DIR = {"de": "de-DE", "en": "en-US"}
DEFAULT_LANG = "en-US"


# --------------------------------------------------------------------------- #
# Config / API key
# --------------------------------------------------------------------------- #
def load_dotenv(path: Path = ENV_PATH) -> None:
    """Minimal .env loader — populates os.environ without external deps.

    Existing environment variables take precedence (are not overwritten).
    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments, and simple
    single/double quoting.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def load_api_key(cli_key: str | None) -> str:
    if cli_key:
        return cli_key.strip()
    env = os.environ.get("TMDB_API_KEY")
    if env:
        return env.strip()
    if CONFIG_PATH.is_file():
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_PATH)
        key = cfg.get("tmdb", "api_key", fallback="").strip()
        if key:
            return key
    sys.exit(
        "No TMDB API key found.\n"
        "  Provide one via --api-key, TMDB_API_KEY in .env, the environment,\n"
        f"  or {CONFIG_PATH} ([tmdb] api_key = ...).\n"
    )


def resolve_roots(cli_roots: list[Path]) -> list[Path]:
    """Determine which library directories to scan.

    Priority: --root args > MOVIE_DIRS (os.pathsep-separated) from env/.env.
    """
    if cli_roots:
        roots = cli_roots
    else:
        raw = os.environ.get("MOVIE_DIRS", "").strip()
        if not raw:
            sys.exit(
                "No library directory configured.\n"
                "  Set MOVIE_DIRS in .env (os.pathsep-separated) or pass --root.\n"
            )
        roots = [Path(os.path.expanduser(p.strip()))
                 for p in raw.split(os.pathsep) if p.strip()]
    valid: list[Path] = []
    for r in roots:
        if r.is_dir():
            valid.append(r)
        else:
            print(f"  ! skipping missing directory: {r}", file=sys.stderr)
    if not valid:
        sys.exit("None of the configured directories exist.")
    return valid


# --------------------------------------------------------------------------- #
# Library scanning
# --------------------------------------------------------------------------- #
@dataclass
class MovieItem:
    """One logical movie (may map to several media parts)."""
    stem: Path                 # basename path without extension and without .Part N
    parts: list[Path] = field(default_factory=list)
    title: str = ""
    year: int | None = None
    lang: str = DEFAULT_LANG

    @property
    def nfo_path(self) -> Path:
        return self.stem.with_suffix(".nfo")


def strip_part(stem: str) -> str:
    return PART_RE.sub("", stem)


def display_path(path: Path, roots: list[Path]) -> Path:
    """Show a path relative to whichever configured root contains it."""
    for root in roots:
        try:
            return path.relative_to(root)
        except ValueError:
            continue
    return path


def detect_lang(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return DEFAULT_LANG
    if rel.parts:
        return LANG_BY_DIR.get(rel.parts[0], DEFAULT_LANG)
    return DEFAULT_LANG


def parse_title_year(stem_name: str) -> tuple[str, int | None]:
    """From a filename stem, extract a search title and year.

    "16 Blocks (2006)" -> ("16 Blocks", 2006)
    "Collateral"       -> ("Collateral", None)
    """
    year: int | None = None
    m = YEAR_RE.search(stem_name)
    name = stem_name
    if m:
        year = int(m.group(1))
        name = stem_name[: m.start()] + stem_name[m.end():]
    # Drop common edition/quality tags that hurt search.
    name = re.sub(
        r"\b(Directors? Cut|Director's Cut|Extended|Uncut|Remastered|UCE|"
        r"Special Edition|Theatrical|IMAX|4K|UHD|1080p|720p|BluRay)\b",
        "", name, flags=re.IGNORECASE,
    )
    name = name.replace("_", " ")
    name = re.sub(r"\s+-\s+$", "", name)      # trailing " - "
    name = re.sub(r"\s{2,}", " ", name).strip(" -")
    return name, year


def find_missing(root: Path) -> list[MovieItem]:
    groups: dict[Path, MovieItem] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
            continue
        stem_str = strip_part(str(path.with_suffix("")))
        stem = Path(stem_str)
        item = groups.get(stem)
        if item is None:
            title, year = parse_title_year(stem.name)
            item = MovieItem(stem=stem, title=title, year=year,
                             lang=detect_lang(path, root))
            groups[stem] = item
        item.parts.append(path)

    missing = [it for it in groups.values() if not it.nfo_path.exists()]
    missing.sort(key=lambda it: str(it.stem).lower())
    return missing


# --------------------------------------------------------------------------- #
# TMDB client
# --------------------------------------------------------------------------- #
class Tmdb:
    def __init__(self, api_key: str) -> None:
        self.key = api_key
        self.sess = requests.Session()

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params["api_key"] = self.key
        last: requests.Response | None = None
        for _ in range(4):
            last = self.sess.get(f"{TMDB_BASE}{path}", params=params, timeout=30)
            if last.status_code == 429:  # rate limited
                wait = int(last.headers.get("Retry-After", "2"))
                time.sleep(wait + 1)
                continue
            last.raise_for_status()
            return last.json()
        if last is not None:
            last.raise_for_status()
        return {}

    def search(self, title: str, year: int | None, lang: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": title, "language": lang,
                                  "include_adult": "false"}
        if year:
            params["year"] = year
        results = self._get("/search/movie", **params).get("results", [])
        if not results and year:
            # Retry without the year constraint.
            params.pop("year", None)
            results = self._get("/search/movie", **params).get("results", [])
        if not results and lang != "en-US":
            # Retry in English (helps for original-language titles).
            results = self._get("/search/movie", query=title,
                                language="en-US",
                                include_adult="false").get("results", [])
        return results

    def details(self, tmdb_id: int, lang: str) -> dict[str, Any]:
        return self._get(
            f"/movie/{tmdb_id}",
            language=lang,
            append_to_response="credits,release_dates,images,videos,external_ids",
            include_image_language=f"{lang.split('-')[0]},en,null",
        )


# --------------------------------------------------------------------------- #
# NFO building
# --------------------------------------------------------------------------- #
def esc(text: str | None) -> str:
    return escape(text or "", {'"': "&quot;", "'": "&apos;"})


def certification(details: dict[str, Any], lang: str) -> str:
    """Pick an MPAA/FSK-style certification from release_dates."""
    country = "DE" if lang.startswith("de") else "US"
    entries = details.get("release_dates", {}).get("results", [])
    by_country = {e.get("iso_3166_1"): e for e in entries}
    for cc in (country, "US"):
        e = by_country.get(cc)
        if not e:
            continue
        for rd in e.get("release_dates", []):
            cert = (rd.get("certification") or "").strip()
            if cert:
                return f"Rated {cert}" if cc == "US" else cert
    return ""


def collect_thumbs(details: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    images = details.get("images", {})
    for poster in images.get("posters", []):
        fp = poster.get("file_path")
        if fp:
            lines.append(
                f'    <thumb aspect="poster" preview="{IMG_W500}{fp}">'
                f'{IMG_ORIG}{fp}</thumb>'
            )
    for backdrop in images.get("backdrops", []):
        fp = backdrop.get("file_path")
        if fp:
            lines.append(
                f'    <thumb aspect="fanart" preview="{IMG_W500}{fp}">'
                f'{IMG_ORIG}{fp}</thumb>'
            )
    return lines


def build_nfo(details: dict[str, Any], lang: str) -> str:
    credits = details.get("credits", {})
    cast = credits.get("cast", [])
    crew = credits.get("crew", [])
    directors = [c for c in crew if c.get("job") == "Director"]
    writers = [c for c in crew
               if c.get("department") == "Writing"
               or c.get("job") in {"Writer", "Screenplay", "Story"}]

    imdb_id = (details.get("external_ids", {}).get("imdb_id")
               or details.get("imdb_id") or "")
    tmdb_id = details.get("id")
    year = ""
    premiered = details.get("release_date") or ""
    if premiered:
        year = premiered[:4]

    rating = details.get("vote_average") or 0.0
    votes = details.get("vote_count") or 0

    # Trailer: prefer a YouTube trailer.
    trailer = ""
    for v in details.get("videos", {}).get("results", []):
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            trailer = ("plugin://plugin.video.youtube/?action=play_video&amp;"
                       f"videoid={v.get('key')}")
            break

    out: list[str] = []
    a = out.append
    a('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>')
    a("<movie>")
    a(f"    <title>{esc(details.get('title'))}</title>")
    a(f"    <originaltitle>{esc(details.get('original_title'))}</originaltitle>")
    a("    <ratings>")
    a('        <rating name="themoviedb" max="10" default="true">')
    a(f"            <value>{float(rating):.6f}</value>")
    a(f"            <votes>{votes}</votes>")
    a("        </rating>")
    a("    </ratings>")
    a("    <userrating>0</userrating>")
    a("    <top250>0</top250>")
    a("    <outline></outline>")
    a(f"    <plot>{esc(details.get('overview'))}</plot>")
    a(f"    <tagline>{esc(details.get('tagline'))}</tagline>")
    runtime = details.get("runtime") or 0
    a(f"    <runtime>{runtime}</runtime>")

    for line in collect_thumbs(details):
        a(line)

    a("    <fanart>")
    a("    </fanart>")
    a(f"    <mpaa>{esc(certification(details, lang))}</mpaa>")
    a("    <playcount>0</playcount>")
    a("    <lastplayed></lastplayed>")
    if imdb_id:
        a(f"    <id>{esc(imdb_id)}</id>")
        a(f'    <uniqueid type="imdb" default="true">{esc(imdb_id)}</uniqueid>')
    if tmdb_id:
        a(f'    <uniqueid type="tmdb">{tmdb_id}</uniqueid>')
    for g in details.get("genres", []):
        a(f"    <genre>{esc(g.get('name'))}</genre>")
    for pc in details.get("production_countries", []):
        a(f"    <country>{esc(pc.get('name'))}</country>")
    coll = details.get("belongs_to_collection")
    if coll:
        a("    <set>")
        a(f"        <name>{esc(coll.get('name'))}</name>")
        a("        <overview></overview>")
        a("    </set>")
    for w in dedup_by_name(writers):
        a(f"    <credits>{esc(w.get('name'))}</credits>")
    for d in directors:
        a(f"    <director>{esc(d.get('name'))}</director>")
    a(f"    <premiered>{esc(premiered)}</premiered>")
    a(f"    <year>{year}</year>")
    a("    <status></status>")
    a("    <code></code>")
    a("    <aired></aired>")
    studios = details.get("production_companies", [])
    if studios:
        a(f"    <studio>{esc(studios[0].get('name'))}</studio>")
    else:
        a("    <studio></studio>")
    a(f"    <trailer>{trailer}</trailer>")

    for i, c in enumerate(cast):
        a("    <actor>")
        a(f"        <name>{esc(c.get('name'))}</name>")
        a(f"        <role>{esc(c.get('character'))}</role>")
        a(f"        <order>{c.get('order', i)}</order>")
        pp = c.get("profile_path")
        thumb = f"{IMG_ORIG}{pp}" if pp else ""
        a(f"        <thumb>{thumb}</thumb>")
        a("    </actor>")

    a("    <resume>")
    a("        <position>0.000000</position>")
    a("        <total>0.000000</total>")
    a("    </resume>")
    a(f"    <dateadded>{time.strftime('%Y-%m-%d %H:%M:%S')}</dateadded>")
    a("</movie>")
    return "\n".join(out) + "\n"


def dedup_by_name(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in people:
        name = p.get("name") or ""
        if name and name not in seen:
            seen.add(name)
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Interactive selection
# --------------------------------------------------------------------------- #
def prompt_choice(item: MovieItem, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    print("\n" + "=" * 78)
    rel_parts = ", ".join(p.name for p in item.parts)
    print(f"FILE:   {item.stem.name}  ({len(item.parts)} part(s): {rel_parts})")
    print(f"SEARCH: title='{item.title}' year={item.year} lang={item.lang}")
    print("-" * 78)
    if not results:
        print("  No TMDB results.")
    shown = results[:10]
    for idx, r in enumerate(shown, 1):
        date = r.get("release_date") or "????-??-??"
        ryear = date[:4]
        overview = (r.get("overview") or "").replace("\n", " ")
        if len(overview) > 140:
            overview = overview[:137] + "..."
        orig = r.get("original_title")
        title = r.get("title") or orig or "?"
        extra = f" [{orig}]" if orig and orig != title else ""
        print(f"  [{idx:>2}] {title}{extra} ({ryear})  tmdb:{r.get('id')} "
              f"★{r.get('vote_average', 0):.1f}")
        if overview:
            print(f"       {overview}")
    print("-" * 78)
    if shown:
        print("  Enter a number to select, [m] new search term, [s]kip, [q]uit.")
    else:
        print("  No matches. [m] enter a new search term, [s]kip this file, [q]uit.")
    while True:
        choice = input("  > ").strip().lower()
        if choice in {"s", ""}:
            return None
        if choice == "q":
            raise KeyboardInterrupt
        if choice == "m":
            return {"__manual__": True}
        if choice.isdigit() and shown:
            n = int(choice)
            if 1 <= n <= len(shown):
                return shown[n - 1]
        print("  Invalid choice. [m] search again, [s]kip, [q]uit"
              + (", or a number." if shown else "."))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def process(item: MovieItem, tmdb: Tmdb, dry_run: bool) -> str:
    title, year = item.title, item.year
    while True:
        results = tmdb.search(title, year, item.lang)
        choice = prompt_choice(item, results)
        if choice is None:
            return "skipped"
        if choice.get("__manual__"):
            q = input(f"  Search title [{title}]: ").strip()
            y = input(f"  Year [{year if year else 'any'}]: ").strip()
            if q:
                title = q
            if y:
                year = int(y) if y.isdigit() else None
            continue
        details = tmdb.details(choice["id"], item.lang)
        nfo = build_nfo(details, item.lang)
        if dry_run:
            print(f"  [dry-run] would write {item.nfo_path}")
            return "dry-run"
        item.nfo_path.write_text(nfo, encoding="utf-8")
        print(f"  ✔ wrote {item.nfo_path}")
        return "written"


def main() -> None:
    load_dotenv()

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, action="append", default=[],
                    metavar="DIR",
                    help="Library root to scan (repeatable; "
                         "overrides MOVIE_DIRS from .env)")
    ap.add_argument("--api-key", help="TMDB v3 API key (overrides env/config)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write NFO files, just show what would happen")
    ap.add_argument("--only", help="Only process movies whose path contains this substring")
    ap.add_argument("--list", action="store_true",
                    help="List movies missing an NFO and exit")
    args = ap.parse_args()

    roots = resolve_roots(args.root)

    missing: list[MovieItem] = []
    for root in roots:
        missing.extend(find_missing(root))
    missing.sort(key=lambda it: str(it.stem).lower())

    if args.only:
        needle = args.only.lower()
        missing = [m for m in missing if needle in str(m.stem).lower()]

    if not missing:
        print("No media files missing an NFO. ✨")
        return

    print(f"Found {len(missing)} movie(s) without an NFO:")
    for m in missing:
        print(f"  - {display_path(m.stem, roots)}  "
              f"(year={m.year}, {len(m.parts)} part(s))")

    if args.list:
        return

    api_key = load_api_key(args.api_key)
    tmdb = Tmdb(api_key)

    counts = {"written": 0, "skipped": 0, "dry-run": 0}
    try:
        for item in missing:
            result = process(item, tmdb, args.dry_run)
            counts[result] = counts.get(result, 0) + 1
    except KeyboardInterrupt:
        print("\nAborted by user.")

    print("\nDone. "
          f"written={counts['written']} "
          f"skipped={counts['skipped']} "
          f"dry-run={counts['dry-run']}")


if __name__ == "__main__":
    main()
