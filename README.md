# pokemon-sniper

A Pokémon card **paper-trading** bot. It trades a virtual $1,000 bankroll on
real daily TCGplayer prices (via [TCGCSV](https://tcgcsv.com)), never buys
anything real, and publishes everything to one dashboard page.
Paper trading. Returns before fees.

See [`SNIPER_BUILD_SPEC.md`](SNIPER_BUILD_SPEC.md) for the full spec.

## Setup

```sh
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
brew install p7zip   # or apt-get install p7zip-full; needed for the backfill
```

## Data

- `python -m scripts.backfill_archive` loads the full price history from
  TCGCSV's daily archives (2024-02-08 to yesterday). It is resumable: stop it
  and re-run any time. Every archive is cached in `data/archive_cache/` and
  never downloaded twice.
- `python -m scripts.fetch_prices` fetches today's prices from the live API.
- `python -m scripts.restore_snapshot` / `publish_snapshot` move
  `data/prices.db` to and from GitHub Release assets.
- All tunable numbers live in `sniper/config.py`.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -t .
```
