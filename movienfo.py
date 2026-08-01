#!/usr/bin/env python3
"""movienfo — find media files without a matching .nfo, search TMDB, and write Kodi/TMM-style NFOs.

Scans a movie library for media files that have no sibling ``.nfo`` (using the
shared-basename rule so multi-part ``Movie.Part 1.mkv`` / ``Movie.Part 2.mkv``
share a single ``Movie.nfo``, matching the existing library convention).

For each movie without an NFO it parses the title + year from the filename,
searches TMDB, presents the candidates in the terminal for selection, then
fetches full details and writes an NFO in the same style as the existing files.

Movies vs. series:
    * A movie library holds media files; each gets a sibling ``Movie.nfo``.
    * A series library holds one folder per show, with episode files named
      ``... SxxExx ...``. Each show gets a ``tvshow.nfo`` and every episode gets
      its own ``episodedetails`` NFO. Use ``--series`` (or a ``:tv`` type tag on
      the directory, see below) to scan in series mode.

Config (via a ``.env`` file in the project dir, or real environment variables):
    TMDB_API_KEY   TMDB v3 API key. Also overridable with ``--api-key``.
    MOVIE_DIRS     One or more library roots to scan, separated by ``os.pathsep``
                   (``:`` on Linux/macOS). Overridable with ``--root`` (repeatable).

                   Each entry may carry a default language and/or a type. The
                   type tag uses ``|`` so entries stay ``:``-separable:
                       /path                      (language auto-detected, movie)
                       /path=de-DE                (German metadata)
                       /path=en                   (short code, expands to en-US)
                       /path=de-DE|tv             (German series library)
                       /path|tv                   (series, auto language)
                   Short codes ``de``/``en`` expand to ``de-DE``/``en-US``.

Usage:
    ./movienfo.py                       # scan dirs from .env / MOVIE_DIRS, interactive
    ./movienfo.py --root /path/to/movies=de-DE [--root /another=en]
    ./movienfo.py --series --root /path/to/series=de-DE
    ./movienfo.py --dry-run             # show what would be written, write nothing
    ./movienfo.py --only "Collateral"   # only titles whose path matches substring
    ./movienfo.py --list                # just list items missing an NFO and exit
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
# "S01E02", "s1e2", "1x02" — capture season + episode numbers.
SXXEXX_RE = re.compile(r"(?:[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})|(\d{1,2})x(\d{1,3}))")

TMDB_BASE = "https://api.themoviedb.org/3"
IMG_ORIG = "https://image.tmdb.org/t/p/original"
IMG_W500 = "https://image.tmdb.org/t/p/w500"

# Map a top-level library subfolder / short code to a full TMDB language tag.
LANG_BY_DIR = {"de": "de-DE", "en": "en-US"}
DEFAULT_LANG = "en-US"


def expand_lang(code: str) -> str:
    """Normalize a language token: 'de' -> 'de-DE', 'en' -> 'en-US', else as-is."""
    code = code.strip()
    if not code:
        return DEFAULT_LANG
    return LANG_BY_DIR.get(code.lower(), code)


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


@dataclass
class Root:
    """A configured scan directory with its default language and content type."""
    path: Path
    lang: str = DEFAULT_LANG        # empty-ish means "auto-detect from subfolder"
    kind: str = "movie"             # "movie" or "tv"
    lang_explicit: bool = False     # was the language given, or should we auto-detect?


def parse_root_spec(spec: str, force_kind: str | None) -> Root | None:
    """Parse a ``path[=lang][|type]`` entry into a Root.

    The type tag uses ``|`` (not ``:``) so entries stay separable by
    ``os.pathsep`` (``:``) inside MOVIE_DIRS on Linux/macOS.

    Examples:
        /movies/de            -> lang auto, movie
        /movies/de=de-DE      -> de-DE, movie
        /series/de=de|tv      -> de-DE, tv
        /series/en|tv         -> lang auto, tv
    """
    spec = spec.strip()
    if not spec:
        return None
    kind = force_kind or "movie"
    # A trailing "|tv" / "|movie" type tag.
    m = re.search(r"\|(tv|movie)$", spec)
    if m:
        kind = m.group(1)
        spec = spec[: m.start()]
    # An "=lang" suffix. Split on the last '=' so Windows drive letters survive.
    lang = DEFAULT_LANG
    lang_explicit = False
    if "=" in spec:
        spec, _, lang_raw = spec.rpartition("=")
        lang = expand_lang(lang_raw)
        lang_explicit = True
    path = Path(os.path.expanduser(spec.strip()))
    if force_kind:
        kind = force_kind
    return Root(path=path, lang=lang, kind=kind, lang_explicit=lang_explicit)


def resolve_roots(cli_roots: list[str], force_kind: str | None) -> list[Root]:
    """Determine which library directories to scan, with language + type.

    Priority: --root args > MOVIE_DIRS (os.pathsep-separated) from env/.env.
    ``force_kind`` (from --series/--movies) overrides any per-entry ``:type`` tag.
    """
    if cli_roots:
        specs = cli_roots
    else:
        raw = os.environ.get("MOVIE_DIRS", "").strip()
        if not raw:
            sys.exit(
                "No library directory configured.\n"
                "  Set MOVIE_DIRS in .env (os.pathsep-separated) or pass --root.\n"
            )
        specs = [s for s in raw.split(os.pathsep) if s.strip()]

    valid: list[Root] = []
    for spec in specs:
        root = parse_root_spec(spec, force_kind)
        if root is None:
            continue
        if root.path.is_dir():
            valid.append(root)
        else:
            print(f"  ! skipping missing directory: {root.path}", file=sys.stderr)
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
        # NB: do not use Path.with_suffix — titles like "R.I.P.D." or
        # "Mr. & Mrs. Smith" have trailing/embedded dots it would mangle.
        return self.stem.with_name(self.stem.name + ".nfo")


@dataclass
class Episode:
    """One episode file within a show folder."""
    media: Path
    season: int
    episode: int

    @property
    def nfo_path(self) -> Path:
        return self.media.with_name(self.media.stem + ".nfo")


@dataclass
class SeriesItem:
    """One TV show folder, plus the episode files found inside it."""
    folder: Path
    title: str = ""
    year: int | None = None
    lang: str = DEFAULT_LANG
    episodes: list[Episode] = field(default_factory=list)

    @property
    def tvshow_nfo(self) -> Path:
        return self.folder / "tvshow.nfo"

    def missing_episodes(self) -> list[Episode]:
        return [e for e in self.episodes if not e.nfo_path.exists()]

    def needs_work(self) -> bool:
        return not self.tvshow_nfo.exists() or bool(self.missing_episodes())


def strip_part(stem: str) -> str:
    return PART_RE.sub("", stem)


def parse_season_episode(name: str) -> tuple[int, int] | None:
    m = SXXEXX_RE.search(name)
    if not m:
        return None
    if m.group(1) is not None:
        return int(m.group(1)), int(m.group(2))
    return int(m.group(3)), int(m.group(4))


def display_path(path: Path, roots: list["Root"]) -> Path:
    """Show a path relative to whichever configured root contains it."""
    for root in roots:
        try:
            return path.relative_to(root.path)
        except ValueError:
            continue
    return path


def resolve_item_lang(path: Path, root: Root) -> str:
    """Language for one item: explicit root language wins; else auto-detect
    from the first path segment under the root (de/en), else DEFAULT_LANG."""
    if root.lang_explicit:
        return root.lang
    try:
        rel = path.relative_to(root.path)
    except ValueError:
        return DEFAULT_LANG
    if rel.parts:
        return LANG_BY_DIR.get(rel.parts[0].lower(), DEFAULT_LANG)
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


def find_missing(root: Root) -> list[MovieItem]:
    groups: dict[Path, MovieItem] = {}
    for path in sorted(root.path.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
            continue
        stem_str = strip_part(str(path.with_suffix("")))
        stem = Path(stem_str)
        item = groups.get(stem)
        if item is None:
            title, year = parse_title_year(stem.name)
            item = MovieItem(stem=stem, title=title, year=year,
                             lang=resolve_item_lang(path, root))
            groups[stem] = item
        item.parts.append(path)

    missing = [it for it in groups.values() if not it.nfo_path.exists()]
    missing.sort(key=lambda it: str(it.stem).lower())
    return missing


def find_missing_series(root: Root) -> list[SeriesItem]:
    """Group episode files by their containing show folder.

    A "show folder" is the directory that directly contains episode media files
    with an SxxExx marker (episodes may sit directly in it or in Season subdirs).
    """
    shows: dict[Path, SeriesItem] = {}
    for path in sorted(root.path.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTS:
            continue
        se = parse_season_episode(path.name)
        if se is None:
            continue
        season, episode = se
        # The show folder: parent, unless the parent looks like a Season dir.
        parent = path.parent
        if re.fullmatch(r"(?i)(season\s*\d+|staffel\s*\d+|s\d+)", parent.name):
            show_folder = parent.parent
        else:
            show_folder = parent
        item = shows.get(show_folder)
        if item is None:
            title, year = parse_title_year(show_folder.name)
            item = SeriesItem(folder=show_folder, title=title, year=year,
                              lang=resolve_item_lang(path, root))
            shows[show_folder] = item
        item.episodes.append(Episode(media=path, season=season, episode=episode))

    missing = [s for s in shows.values() if s.needs_work()]
    missing.sort(key=lambda s: str(s.folder).lower())
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

    # -- TV / series --------------------------------------------------------- #
    def search_tv(self, title: str, year: int | None,
                  lang: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": title, "language": lang,
                                  "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        results = self._get("/search/tv", **params).get("results", [])
        if not results and year:
            params.pop("first_air_date_year", None)
            results = self._get("/search/tv", **params).get("results", [])
        if not results and lang != "en-US":
            results = self._get("/search/tv", query=title, language="en-US",
                                include_adult="false").get("results", [])
        return results

    def tv_details(self, tv_id: int, lang: str) -> dict[str, Any]:
        return self._get(
            f"/tv/{tv_id}",
            language=lang,
            append_to_response="credits,images,external_ids,content_ratings",
            include_image_language=f"{lang.split('-')[0]},en,null",
        )

    def tv_season(self, tv_id: int, season: int, lang: str) -> dict[str, Any]:
        return self._get(f"/tv/{tv_id}/season/{season}",
                         language=lang, append_to_response="credits")


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


def actor_lines(cast: list[dict[str, Any]], indent: str = "    ") -> list[str]:
    out: list[str] = []
    for i, c in enumerate(cast):
        out.append(f"{indent}<actor>")
        out.append(f"{indent}    <name>{esc(c.get('name'))}</name>")
        out.append(f"{indent}    <role>{esc(c.get('character'))}</role>")
        out.append(f"{indent}    <order>{c.get('order', i)}</order>")
        pp = c.get("profile_path")
        out.append(f"{indent}    <thumb>{IMG_ORIG}{pp if pp else ''}</thumb>"
                   if pp else f"{indent}    <thumb></thumb>")
        out.append(f"{indent}</actor>")
    return out


def tv_certification(details: dict[str, Any], lang: str) -> str:
    country = "DE" if lang.startswith("de") else "US"
    ratings = details.get("content_ratings", {}).get("results", [])
    by_country = {r.get("iso_3166_1"): (r.get("rating") or "") for r in ratings}
    return by_country.get(country) or by_country.get("US") or ""


def build_tvshow_nfo(details: dict[str, Any], lang: str) -> str:
    ext = details.get("external_ids", {})
    imdb_id = ext.get("imdb_id") or ""
    tvdb_id = ext.get("tvdb_id")
    tmdb_id = details.get("id")
    premiered = details.get("first_air_date") or ""
    year = premiered[:4] if premiered else ""

    out: list[str] = []
    a = out.append
    a('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>')
    a("<tvshow>")
    a(f"    <title>{esc(details.get('name'))}</title>")
    a(f"    <originaltitle>{esc(details.get('original_name'))}</originaltitle>")
    a(f"    <showtitle>{esc(details.get('name'))}</showtitle>")
    a("    <ratings>")
    a('        <rating name="tmdb" max="10" default="true">')
    a(f"            <value>{float(details.get('vote_average') or 0):.6f}</value>")
    a(f"            <votes>{details.get('vote_count') or 0}</votes>")
    a("        </rating>")
    a("    </ratings>")
    a("    <userrating>0</userrating>")
    a("    <top250>0</top250>")
    a(f"    <season>{details.get('number_of_seasons') or -1}</season>")
    a(f"    <episode>{details.get('number_of_episodes') or 0}</episode>")
    a("    <displayseason>-1</displayseason>")
    a("    <displayepisode>-1</displayepisode>")
    a("    <outline></outline>")
    a(f"    <plot>{esc(details.get('overview'))}</plot>")
    a("    <tagline></tagline>")
    a("    <runtime>0</runtime>")
    for line in collect_thumbs(details):
        a(line)
    a("    <fanart>")
    a("    </fanart>")
    a(f"    <mpaa>{esc(tv_certification(details, lang))}</mpaa>")
    a("    <playcount>0</playcount>")
    a("    <lastplayed></lastplayed>")
    if tmdb_id:
        a(f"    <episodeguide>{tmdb_id}</episodeguide>")
        a(f"    <id>{tmdb_id}</id>")
    if imdb_id:
        a(f'    <uniqueid type="imdb">{esc(imdb_id)}</uniqueid>')
    if tmdb_id:
        a(f'    <uniqueid type="tmdb" default="true">{tmdb_id}</uniqueid>')
    if tvdb_id:
        a(f'    <uniqueid type="tvdb">{tvdb_id}</uniqueid>')
    for g in details.get("genres", []):
        a(f"    <genre>{esc(g.get('name'))}</genre>")
    a(f"    <premiered>{esc(premiered)}</premiered>")
    a(f"    <year>{year}</year>")
    a(f"    <status>{esc(details.get('status'))}</status>")
    a("    <code></code>")
    a("    <aired></aired>")
    networks = details.get("networks", [])
    a(f"    <studio>{esc(networks[0].get('name')) if networks else ''}</studio>")
    a("    <trailer></trailer>")
    for line in actor_lines(details.get("credits", {}).get("cast", [])):
        a(line)
    a("</tvshow>")
    return "\n".join(out) + "\n"


def build_episode_nfo(show: dict[str, Any], ep: dict[str, Any]) -> str:
    """Build an episodedetails NFO from a TMDB season-episode object.

    The episode/season data is already fetched in the target language, so no
    separate language argument is needed here."""
    crew = ep.get("crew", [])
    directors = [c for c in crew if c.get("job") == "Director"]
    writers = [c for c in crew
               if c.get("department") == "Writing"
               or c.get("job") in {"Writer", "Screenplay", "Story", "Teleplay"}]
    guests = ep.get("guest_stars", [])
    show_cast = show.get("credits", {}).get("cast", [])

    air = ep.get("air_date") or ""
    year = air[:4] if air else ""

    out: list[str] = []
    a = out.append
    a('<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>')
    a("<episodedetails>")
    a(f"    <title>{esc(ep.get('name'))}</title>")
    a(f"    <showtitle>{esc(show.get('name'))}</showtitle>")
    a("    <ratings>")
    a('        <rating name="tmdb" max="10" default="true">')
    a(f"            <value>{float(ep.get('vote_average') or 0):.6f}</value>")
    a(f"            <votes>{ep.get('vote_count') or 0}</votes>")
    a("        </rating>")
    a("    </ratings>")
    a("    <userrating>0</userrating>")
    a("    <top250>0</top250>")
    a(f"    <season>{ep.get('season_number')}</season>")
    a(f"    <episode>{ep.get('episode_number')}</episode>")
    a("    <displayseason>-1</displayseason>")
    a("    <displayepisode>-1</displayepisode>")
    a("    <outline></outline>")
    a(f"    <plot>{esc(ep.get('overview'))}</plot>")
    a("    <tagline></tagline>")
    runtime = ep.get("runtime") or 0
    a(f"    <runtime>{runtime}</runtime>")
    still = ep.get("still_path")
    if still:
        a(f'    <thumb aspect="thumb" preview="{IMG_W500}{still}">'
          f'{IMG_ORIG}{still}</thumb>')
    a("    <mpaa></mpaa>")
    a("    <playcount>0</playcount>")
    a("    <lastplayed></lastplayed>")
    ep_id = ep.get("id")
    if ep_id:
        a(f"    <id>{ep_id}</id>")
        a(f'    <uniqueid type="tmdb" default="true">{ep_id}</uniqueid>')
    for g in show.get("genres", []):
        a(f"    <genre>{esc(g.get('name'))}</genre>")
    for w in dedup_by_name(writers):
        a(f"    <credits>{esc(w.get('name'))}</credits>")
    for d in directors:
        a(f"    <director>{esc(d.get('name'))}</director>")
    a(f"    <premiered>{esc(air)}</premiered>")
    a(f"    <year>{year}</year>")
    a("    <status></status>")
    a("    <code></code>")
    a(f"    <aired>{esc(air)}</aired>")
    networks = show.get("networks", [])
    a(f"    <studio>{esc(networks[0].get('name')) if networks else ''}</studio>")
    a("    <trailer></trailer>")
    # Prefer the show's regular cast, then episode guest stars.
    for line in actor_lines(show_cast + guests):
        a(line)
    a("</episodedetails>")
    return "\n".join(out) + "\n"


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
    return _read_selection(shown)


def _read_selection(shown: list[dict[str, Any]]) -> dict[str, Any] | None:
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


def prompt_choice_tv(item: SeriesItem,
                     results: list[dict[str, Any]]) -> dict[str, Any] | None:
    print("\n" + "=" * 78)
    n_missing = len(item.missing_episodes())
    print(f"SHOW:   {item.folder.name}  "
          f"({len(item.episodes)} episode file(s), {n_missing} missing NFO)")
    print(f"SEARCH: title='{item.title}' year={item.year} lang={item.lang}")
    print("-" * 78)
    if not results:
        print("  No TMDB results.")
    shown = results[:10]
    for idx, r in enumerate(shown, 1):
        date = r.get("first_air_date") or "????-??-??"
        ryear = date[:4]
        overview = (r.get("overview") or "").replace("\n", " ")
        if len(overview) > 140:
            overview = overview[:137] + "..."
        orig = r.get("original_name")
        title = r.get("name") or orig or "?"
        extra = f" [{orig}]" if orig and orig != title else ""
        print(f"  [{idx:>2}] {title}{extra} ({ryear})  tmdb:{r.get('id')} "
              f"★{r.get('vote_average', 0):.1f}")
        if overview:
            print(f"       {overview}")
    print("-" * 78)
    if shown:
        print("  Enter a number to select the show, [m] new search term, "
              "[s]kip, [q]uit.")
    else:
        print("  No matches. [m] enter a new search term, [s]kip this show, [q]uit.")
    return _read_selection(shown)


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


def process_series(item: SeriesItem, tmdb: Tmdb, dry_run: bool) -> str:
    """Match a show, then write tvshow.nfo + an NFO for each missing episode."""
    title, year = item.title, item.year
    while True:
        results = tmdb.search_tv(title, year, item.lang)
        choice = prompt_choice_tv(item, results)
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

        show = tmdb.tv_details(choice["id"], item.lang)

        # tvshow.nfo
        if not item.tvshow_nfo.exists():
            if dry_run:
                print(f"  [dry-run] would write {item.tvshow_nfo}")
            else:
                item.tvshow_nfo.write_text(build_tvshow_nfo(show, item.lang),
                                           encoding="utf-8")
                print(f"  ✔ wrote {item.tvshow_nfo}")

        # Episodes — fetch each needed season once and cache it.
        season_cache: dict[int, dict[str, Any]] = {}
        wrote = 0
        for ep in item.missing_episodes():
            season_data = season_cache.get(ep.season)
            if season_data is None:
                season_data = tmdb.tv_season(choice["id"], ep.season, item.lang)
                season_cache[ep.season] = season_data
            ep_obj = next(
                (e for e in season_data.get("episodes", [])
                 if e.get("episode_number") == ep.episode), None)
            if ep_obj is None:
                print(f"    ! S{ep.season:02d}E{ep.episode:02d}: not found on TMDB "
                      f"({ep.media.name})")
                continue
            nfo = build_episode_nfo(show, ep_obj)
            if dry_run:
                print(f"    [dry-run] S{ep.season:02d}E{ep.episode:02d} -> "
                      f"{ep.nfo_path.name}")
            else:
                ep.nfo_path.write_text(nfo, encoding="utf-8")
                print(f"    ✔ S{ep.season:02d}E{ep.episode:02d} -> {ep.nfo_path.name}")
            wrote += 1
        return "dry-run" if dry_run else "written"


def run_movies(roots: list[Root], tmdb: Tmdb | None, args: argparse.Namespace,
               counts: dict[str, int]) -> None:
    missing: list[MovieItem] = []
    for root in roots:
        missing.extend(find_missing(root))
    missing.sort(key=lambda it: str(it.stem).lower())
    if args.only:
        needle = args.only.lower()
        missing = [m for m in missing if needle in str(m.stem).lower()]

    if not missing:
        print("No movies missing an NFO. ✨")
        return
    print(f"\nFound {len(missing)} movie(s) without an NFO:")
    for m in missing:
        print(f"  - {display_path(m.stem, roots)}  "
              f"(year={m.year}, {len(m.parts)} part(s))")
    if args.list or tmdb is None:
        return
    for item in missing:
        result = process(item, tmdb, args.dry_run)
        counts[result] = counts.get(result, 0) + 1


def run_series(roots: list[Root], tmdb: Tmdb | None, args: argparse.Namespace,
               counts: dict[str, int]) -> None:
    missing: list[SeriesItem] = []
    for root in roots:
        missing.extend(find_missing_series(root))
    missing.sort(key=lambda s: str(s.folder).lower())
    if args.only:
        needle = args.only.lower()
        missing = [s for s in missing if needle in str(s.folder).lower()]

    if not missing:
        print("No series missing an NFO. ✨")
        return
    print(f"\nFound {len(missing)} series needing NFO work:")
    for s in missing:
        tvshow = "tvshow.nfo missing" if not s.tvshow_nfo.exists() else "tvshow.nfo ok"
        print(f"  - {display_path(s.folder, roots)}  "
              f"({tvshow}, {len(s.missing_episodes())}/{len(s.episodes)} episodes missing)")
    if args.list or tmdb is None:
        return
    for item in missing:
        result = process_series(item, tmdb, args.dry_run)
        counts[result] = counts.get(result, 0) + 1


def main() -> None:
    load_dotenv()

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", default=[], metavar="DIR[=lang][:type]",
                    help="Library root to scan (repeatable; overrides MOVIE_DIRS). "
                         "May include an =lang and/or :tv|:movie tag.")
    ap.add_argument("--api-key", help="TMDB v3 API key (overrides env/config)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write NFO files, just show what would happen")
    ap.add_argument("--only", help="Only process items whose path contains this substring")
    ap.add_argument("--list", action="store_true",
                    help="List items missing an NFO and exit")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--series", "--tv", action="store_true",
                      help="Treat all scanned directories as TV series libraries")
    mode.add_argument("--movies", action="store_true",
                      help="Treat all scanned directories as movie libraries")
    args = ap.parse_args()

    force_kind = "tv" if args.series else "movie" if args.movies else None
    roots = resolve_roots(args.root, force_kind)

    movie_roots = [r for r in roots if r.kind == "movie"]
    tv_roots = [r for r in roots if r.kind == "tv"]

    tmdb: Tmdb | None = None
    if not args.list:
        tmdb = Tmdb(load_api_key(args.api_key))

    counts = {"written": 0, "skipped": 0, "dry-run": 0}
    try:
        if movie_roots:
            run_movies(movie_roots, tmdb, args, counts)
        if tv_roots:
            run_series(tv_roots, tmdb, args, counts)
    except KeyboardInterrupt:
        print("\nAborted by user.")

    if not args.list:
        print("\nDone. "
              f"written={counts['written']} "
              f"skipped={counts['skipped']} "
              f"dry-run={counts['dry-run']}")


if __name__ == "__main__":
    main()
