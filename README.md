# FlixPatrol → MDBList Sync

Scrape today's top 10 from [FlixPatrol](https://flixpatrol.com) and sync them to [MDBList](https://mdblist.com) static lists.
Runs as a Docker container with a **built-in scheduler** — no external cron needed.

Inspired by [flixpatrol-top10-on-trakt](https://github.com/Navino16/flixpatrol-top10-on-trakt), but targeting MDBList instead of Trakt.

---

## Why MDBList instead of Trakt?

| | Trakt | MDBList |
|---|---|---|
| Free list limit | 5 lists | Unlimited static lists |
| Auth | OAuth2 flow | Simple API key |
| Kometa support | ✅ | ✅ |
| Rate limits | Strict | 1 000 req/day (free) |

## Why a built-in scheduler?

The original Trakt tool is a "run once and exit" binary that you point a cron job at. Many users end up running it **every minute**, which:

- hammers FlixPatrol (risk of IP ban)
- burns through API quotas
- generates noisy logs

This tool **stays running** and executes on a configurable cron schedule (default: twice a day at 06:00 and 18:00). FlixPatrol only updates once per day, so that's plenty.

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/YOUR_USER/flixpatrol-to-mdblist.git
cd flixpatrol-to-mdblist
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and set your **MDBList API key** (get it at [mdblist.com/preferences](https://mdblist.com/preferences/)):

```env
MDBLIST_API_KEY=your_key_here
```

Optionally customise the lists in `config/default.json` (a sensible default is generated on first run).

### 3. Run

```bash
docker compose up -d
```

Check the logs:

```bash
docker compose logs -f
```

That's it. The container will sync on startup and then again at the scheduled times.

---

## Configuration

### Environment Variables

All variables are optional and override values in `config/default.json`.

| Variable | Description | Default |
|---|---|---|
| `MDBLIST_API_KEY` | Your MDBList API key | *(from config)* |
| `SCHEDULE` | Cron expression (5-field) | `0 6,18 * * *` |
| `TZ` | Timezone | `Europe/Zurich` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `DRY_RUN` | `true` = log only, don't write | `false` |
| `RUN_ONCE` | `true` = run once and exit | `false` |

### Schedule Examples

| Expression | Meaning |
|---|---|
| `0 6 * * *` | Daily at 06:00 |
| `0 6,18 * * *` | Twice daily (**recommended**) |
| `0 */8 * * *` | Every 8 hours |
| `0 3 * * 1` | Mondays at 03:00 |
| `@daily` | Midnight |
| `@hourly` | Every hour (not recommended) |

> **Do NOT use** `* * * * *` — this hammers FlixPatrol and burns your MDBList API quota.

### Config File (`config/default.json`)

```jsonc
{
  // Top 10 lists to sync (same options as the Trakt tool)
  "FlixPatrolTop10": [
    {
      "platform": "netflix",       // see supported platforms below
      "location": "world",         // or a specific country
      "fallback": false,           // fallback location if empty
      "limit": 10,                 // max items
      "type": "movies",            // "movies", "shows", or "both"
      "name": "Netflix Top 10 Movies",
      "normalizeName": true,       // kebab-case the MDBList slug
      "kids": false                // Netflix Kids (needs specific country)
    }
  ],

  // Popular lists (IMDB, Letterboxd, etc.)
  "FlixPatrolPopular": [
    {
      "platform": "imdb",
      "limit": 100,
      "type": "both",
      "name": "IMDB Popular"
    }
  ],

  "MDBList": {
    "apiKey": "YOUR_MDBLIST_API_KEY_HERE"
  },

  // Built-in scheduler
  "Schedule": {
    "cron": "0 6,18 * * *",
    "runOnStart": true             // sync immediately on container start
  },

  "Cache": {
    "enabled": true,
    "ttl": 86400                   // 24 h — matches FlixPatrol update frequency
  }
}
```

### Supported Platforms (72)

`9now` `abema` `amazon` `amazon-channels` `amazon-prime` `amc-plus` `antenna-tv` `apple-tv` `bbc` `canal` `catchplay` `cda` `chili` `claro-video` `coupang-play` `crunchyroll` `discovery-plus` `disney` `francetv` `friday` `globoplay` `go3` `google` `hami-video` `hayu` `hbo-max` `hrti` `hulu` `hulu-nippon` `itunes` `jiocinema` `jiohotstar` `joyn` `lemino` `m6plus` `mgm-plus` `myvideo` `neon-tv` `netflix` `now` `oneplay` `osn` `paramount-plus` `peacock` `player` `pluto-tv` `raiplay` `rakuten-tv` `rtl-plus` `sbs` `shahid` `skyshowtime` `stan` `starz` `streamz` `telasa` `tf1` `tod` `trueid` `tubi` `tv-2-norge` `u-next` `viaplay` `videoland` `vidio` `viki` `viu` `vix` `voyo` `vudu` `watchit` `wavve` `wow` `zee5`

### Popular Sources (12)

`facebook` `imdb` `instagram` `letterboxd` `movie-db` `reddit` `rotten-tomatoes` `tmdb` `trakt` `twitter` `wikipedia` `youtube`

---

## How It Works

```
┌─────────────┐      scrape      ┌─────────────┐    search     ┌─────────────┐
│  FlixPatrol │  ──────────────► │   Matcher   │ ────────────► │   MDBList   │
│  (HTML)     │    titles+year   │  (title →   │   IMDB/TMDB   │   (API)     │
└─────────────┘                  │   IMDB ID)  │      IDs      └──────┬──────┘
                                 └─────────────┘                      │
                                                                 create/sync
                                                                 static lists
```

1. **Scrape** FlixPatrol top-10 / popular pages
2. **Match** each title to an IMDB/TMDB ID via MDBList search
3. **Create** the list on MDBList if it doesn't exist
4. **Sync** items (remove old, add new)
5. **Sleep** until the next cron tick

---

## Advanced Usage

### Run Once (for external cron or testing)

```bash
docker compose run --rm -e RUN_ONCE=true flixpatrol-mdblist
```

### Dry Run

```bash
docker compose run --rm -e DRY_RUN=true flixpatrol-mdblist
```

### Without Docker

```bash
pip install -r requirements.txt
cd app
MDBLIST_API_KEY=xxx python flixpatrol_to_mdblist.py
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `MDBList API key not set` | Set `MDBLIST_API_KEY` in `.env` or `config/default.json` |
| `No items from FlixPatrol` | Platform/location combo may not exist on FlixPatrol |
| `title – not found` | Title couldn't be matched; FlixPatrol name ≠ MDBList/IMDB name |
| `API limit reached` | Free MDBList = 1 000 req/day; reduce lists or upgrade |
| Permission denied on config | `sudo chown -R 1000:1000 ./config` |

---

## License

GPL-3.0 — same as the original [flixpatrol-top10-on-trakt](https://github.com/Navino16/flixpatrol-top10-on-trakt).
