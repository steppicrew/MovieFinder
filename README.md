# movienfo

Finds media files that have **no matching `.nfo`**, searches TMDB, lets you pick
the right match interactively in the terminal, and writes Kodi/TinyMediaManager-
style NFO files — in the same format as the NFOs already in your library.

Handles both **movies** and **TV series**.

## How it works

**Movies**

- Scans recursively for media files (`.mkv .mp4 .avi .m4v .mov .wmv .mpg .mpeg`).
- Groups multi-part files (`Movie.Part 1.mkv`, `Movie.Part 2.mkv`) into one movie
  that shares a single `Movie.nfo`.
- Parses title + year from the filename (year from a trailing `(YYYY)`); common
  edition tags (Director's Cut, Extended, …) are stripped before searching.
- Queries TMDB, shows up to 10 candidates, and on your selection writes a full
  NFO (cast, crew, genres, images, certification, trailer, collection, IMDb id).

**Series** (`--series`, or a `|tv` tag on the directory)

- Treats each show folder (the directory containing `SxxExx` episode files, with
  optional `Season NN` subfolders) as one series.
- You pick the show once; it then writes a `tvshow.nfo` **and** an
  `episodedetails` NFO for every episode still missing one, mapped by season and
  episode number. Episodes TMDB doesn't have are reported and skipped.

## Configuration

Copy `.env.example` to `.env` and fill it in:

```ini
# TMDB v3 API key — get one at https://www.themoviedb.org/settings/api
TMDB_API_KEY=YOUR_V3_KEY_HERE

# One or more directories to scan, separated by ":" (os.pathsep on Linux).
# Each entry may carry a default language (=lang) and/or a content type (|tv|movie):
#   /path             auto language, movies
#   /path=de-DE       German metadata (short codes de/en also work)
#   /path=de|tv       German series library
#   /path|tv          series, auto language
MOVIE_DIRS=/movies/de=de-DE:/movies/en=en-US:/series/de=de-DE|tv:/series/en=en-US|tv
```

The language travels with each path. When no `=lang` is given, it falls back to
auto-detecting a `de/` or `en/` first subfolder, else English. The type tag uses
`|` (not `:`) so entries stay separable by `:`.

`.env` is git-ignored, so your key and paths stay local. The key can also be
supplied via `--api-key` or the `TMDB_API_KEY` environment variable; a legacy
`~/.config/movienfo/config.ini` (`[tmdb] api_key = ...`) is still read as a
fallback.

## Usage

The `movienfo.sh` wrapper is the simplest way to start — it locates the script
and `.env` regardless of your current directory and forwards any arguments:

```bash
./movienfo.sh              # interactive run
./movienfo.sh --dry-run    # preview only
```

Or call the Python entry point directly:

```bash
# List movies missing an NFO (no key needed):
./movienfo.py --list

# Interactive run over the directories from .env:
./movienfo.py

# Preview without writing files:
./movienfo.py --dry-run

# Only a subset (path substring match):
./movienfo.py --only "Edgar Wallace"

# Override the directories to scan (repeatable; supports =lang and |tv):
./movienfo.py --root /path/to/movies=de-DE --root /another/path=en

# Series mode (forces all scanned dirs to be treated as TV libraries):
./movienfo.py --series --root /path/to/series=de-DE
```

At each prompt: type a number to select, `s`/Enter to skip, `m` to re-search with
a corrected title/year, or `q` to quit.
