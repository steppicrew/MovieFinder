# movienfo

Finds media files in your movie library that have **no matching `.nfo`**, searches
TMDB for the movie, lets you pick the right match interactively in the terminal,
and writes a Kodi/TinyMediaManager-style `.nfo` next to the media file — in the
same format as the NFOs already in your library.

## How it works

- Scans the library recursively for media files (`.mkv .mp4 .avi .m4v .mov .wmv .mpg .mpeg`).
- Groups multi-part files (`Movie.Part 1.mkv`, `Movie.Part 2.mkv`) into one movie
  that shares a single `Movie.nfo` (matching the existing `Britz.nfo` convention).
- Parses the title and the year from the filename — the year is taken from a
  trailing `(YYYY)`. Common edition tags (Director's Cut, Extended, Uncut, …) are
  stripped before searching.
- Uses the top-level folder to choose the metadata language: `de/` → German
  (`de-DE`), `en/` → English (`en-US`), anything else → English.
- For each movie it queries TMDB, shows up to 10 candidates with year / rating /
  overview, and waits for your selection. It then fetches full details (cast,
  crew, genres, images, certification, trailer, collection, IMDb id) and writes
  the NFO.

## Configuration

Copy `.env.example` to `.env` and fill it in:

```ini
# TMDB v3 API key — get one at https://www.themoviedb.org/settings/api
TMDB_API_KEY=YOUR_V3_KEY_HERE

# One or more directories to scan, separated by ":" (os.pathsep on Linux):
MOVIE_DIRS=/mnt/movies:/mnt/more-movies
```

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

# Override the directories to scan (repeatable):
./movienfo.py --root /path/to/movies --root /another/path
```

At each prompt: type a number to select, `s`/Enter to skip, `m` to re-search with
a corrected title/year, or `q` to quit.
