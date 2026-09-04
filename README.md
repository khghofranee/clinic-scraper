# Clinic Marketplace Ratings — Ireland

Automated weekly collection of star ratings and review counts for every private clinic in Ireland. Powered by Claude + Perplexity + Supabase.

---

## What it does

Across 15 Irish cities, this system:

1. Discovers all clinics (dental, cosmetic, hair, eye, fertility, physio, GP, laser, skin, weight loss)
2. Scrapes ratings from 6 platforms per clinic: Google, WhatClinic, Facebook, Trustpilot, LocalityBiz, Yelp
3. Computes a single weighted `marketplace_rating` using: `Σ(rating × reviews) / Σ(reviews)`
4. Saves a weekly snapshot to Postgres — one row per platform, one aggregate row per clinic

---

## Stack

| Layer | Tool |
|---|---|
| AI orchestrator | Claude `claude-sonnet-4-6` |
| MCP server | Python FastMCP — hosted on Railway |
| Web search | Perplexity `sonar` API |
| Database | Supabase Postgres (`eu-west-1` — Ireland) |
| Connector | Claude custom MCP connector (SSE) |
| Code repo | GitHub `khghofranee/clinic-scraper` |

---

## Project structure

```
clinic-scraper/
├── server.py          # FastMCP server — 4 tools
├── schema.sql         # Postgres table definitions
├── requirements.txt   # Python dependencies
├── Dockerfile         # Railway deployment
├── railway.toml       # Railway config
└── README.md
```

---

## MCP tools

| Tool | What it does |
|---|---|
| `discover_clinics(city, country)` | Finds all clinics in a city via Perplexity. Upserts into `clinics` table. |
| `scrape_platform(clinic_name, city, country, platform)` | Gets rating + review count on one platform via Perplexity. |
| `compute_rating(sources)` | Applies the weighted formula. Pure function — no side effects. |
| `save_result(clinic_id, country, platform_results, marketplace_rating, marketplace_reviews)` | Writes to `platform_snapshots` and `marketplace_snapshots`. |

---

## Database schema

### `clinics` — master list
```sql
id          TEXT PRIMARY KEY   -- e.g. clinic_ie_dub_0001
name        TEXT NOT NULL
city        TEXT NOT NULL
clinic_type TEXT               -- dental, cosmetic, hair, etc.
country     TEXT NOT NULL      -- IE
address     TEXT
created_at  TIMESTAMPTZ
```

### `platform_snapshots` — per-platform weekly data
```sql
id               SERIAL PRIMARY KEY
clinic_id        TEXT REFERENCES clinics(id)
country          TEXT
week_stamp       TEXT    -- e.g. 2026-W36
platform         TEXT    -- google, whatclinic, facebook, trustpilot, localitybiz, yelp
platform_rating  NUMERIC(3,2)
platform_reviews INTEGER
source_url       TEXT
scraped_at       TIMESTAMPTZ
```

### `marketplace_snapshots` — computed weekly rating
```sql
id                   SERIAL PRIMARY KEY
clinic_id            TEXT REFERENCES clinics(id)
country              TEXT
week_stamp           TEXT
marketplace_rating   NUMERIC(4,2)
marketplace_reviews  INTEGER
platforms_scraped    TEXT[]
computed_at          TIMESTAMPTZ
UNIQUE (clinic_id, week_stamp)
```

---

## Weighted rating formula

```
marketplace_rating = Σ(platform_rating × reviews) / Σ(reviews)
```

- Platforms with no listing or 0 reviews are excluded automatically
- If no platform has data → `marketplace_rating = null`, `marketplace_reviews = 0`
- Re-running the same week upserts the aggregate row (`ON CONFLICT DO UPDATE`)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/khghofranee/clinic-scraper.git
cd clinic-scraper
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set environment variables

```bash
cp .env.example .env
# fill in your values
```

| Variable | Description |
|---|---|
| `PERPLEXITY_API_KEY` | Get from perplexity.ai/settings/api |
| `DATABASE_URL` | Supabase Postgres URI — URL-encode special chars in password (`!` → `%21`, `#` → `%23`) |
| `HTTP_TIMEOUT` | Request timeout in seconds (default: `60`) |

### 4. Run the schema

```bash
psql $DATABASE_URL -f schema.sql
```

### 5. Run the server locally

```bash
python server.py
# → SSE server running on http://localhost:8080/sse
```

---

## Deployment (Railway)

Railway auto-deploys on every push to `main`.

```bash
# First time
railway login
railway init
railway up

# After that — just push
git push
```

Set env vars in Railway dashboard → Variables tab.

**Note:** Railway has a known bug where variable names sometimes get a trailing space. If env vars are not being read, check with:

```bash
cat /proc/1/environ | tr '\0' '\n' | grep PERPLEXITY
```

The URL is:
```
https://clinic-scraper-production-7ea9.up.railway.app
```

---

## Claude connector setup

1. Go to **Settings → Connectors → Add custom connector**
2. Name: `Clinic Scraper`
3. URL: `https://clinic-scraper-production-7ea9.up.railway.app/sse`
4. Authentication: **None**
5. Click **Add**

---

## Running a weekly update

Open a new Claude chat with the Clinic Scraper connector active and paste:

```
Use the Clinic Scraper tools to run the Ireland weekly reviews update.

For each of these 15 cities one by one:
Dublin, Cork, Galway, Limerick, Waterford, Drogheda, Dundalk,
Swords, Bray, Navan, Ennis, Tralee, Kilkenny, Sligo, Clonmel

For each city:
1. Call discover_clinics(city, "IE")
2. For each clinic, call scrape_platform 6 times:
   google, whatclinic, facebook, trustpilot, localitybiz, yelp
3. Exclude platforms with null rating or 0 reviews
4. Call compute_rating with valid results only
5. Call save_result with clinic_id, all results, and computed rating
6. Move to next clinic, then next city

When done report:
- Total clinics processed
- Total snapshots saved
- Clinics with at least one valid rating
- Clinics with no data on any platform (list them)
- Any errors encountered
```

---

## Ireland — platforms

| # | Platform | Notes |
|---|---|---|
| 1 | Google | Highest review volume |
| 2 | WhatClinic | Ireland-specific clinic directory |
| 3 | Facebook | Page ratings |
| 4 | Trustpilot | Business reviews |
| 5 | LocalityBiz | Irish local directory |
| 6 | Yelp | Lower IE presence but included |

---

## Infrastructure

| Service | Detail |
|---|---|
| Railway project | `5611d8b1-33b3-46f8-8ed9-d84b2dda84e3` |
| Supabase project | `cdhiphkpjlnrpqggynkg` · eu-west-1 |
| GitHub | `github.com/khghofranee/clinic-scraper` |
| MCP endpoint | `https://clinic-scraper-production-7ea9.up.railway.app/sse` |
