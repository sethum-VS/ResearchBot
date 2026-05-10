# Local Infrastructure Setup

This guide explains how to run the backend's external dependencies locally.

---

## Firecrawl (Local via Docker Compose)

The `WebScraper.py` service expects a local Firecrawl instance on **port 3002**.

### Step 1 — Clone & Configure

```bash
git clone https://github.com/mendableai/firecrawl.git
cd firecrawl
cp apps/api/.env.example apps/api/.env
```

Open `apps/api/.env` and set:

```
USE_DB_AUTHENTICATION=false
```

This disables API-key validation so no cloud account is required.

### Step 2 — Start the Services

```bash
docker compose up -d
```

Firecrawl will be available at `http://localhost:3002`.

### Step 3 — Verify

```bash
curl -s http://localhost:3002/v1/scrape \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-dummy-key' \
  -d '{"url": "https://example.com", "formats": ["markdown"]}' | head -c 200
```

You should see a JSON response containing the scraped Markdown. If Docker
reports port conflicts, check that nothing else is bound to 3002.
