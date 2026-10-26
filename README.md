# UPCA Real Estate Marketing & Media — Realtor.ca Scraper, Cron Scheduler & Drone Lead Intelligence

A modular Python web scraping and lead intelligence system built with [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) (`v0.5`), [FastAPI](https://fastapi.tiangolo.com/), and [APScheduler](https://apscheduler.readthedocs.io/) for UPCA Real Estate Marketing & Media.

---

## Key Features

1. **Interactive Web Dashboard**:
   - Modern Tailwind-powered interface running locally at `http://127.0.0.1:8501`.
   - **On-Demand Scrape**: Choose any Canadian city, neighborhood, or postal code, select listing limits (5, 10, 20, 50), and target what you want to scrape (`all`, `no_drone`, `has_drone`, `uncertain`).
   - **Real-Time Streaming Console**: Watch live Scrapling fetching and parsing activity directly in the browser.

2. **Automated Cron & Interval Job Scheduler**:
   - Built-in background job scheduler powered by APScheduler.
   - Configure recurring tasks with presets:
     - ☀️ *Every day at 9:00 AM* (`0 9 * * *`)
     - 🌆 *Every day at 6:00 PM* (`0 18 * * *`)
     - ⏱️ *Every 1 hour, 6 hours, or 12 hours*
     - ⚙️ *Custom standard 5-part cron expressions* (e.g. `0 9 * * 1-5` for weekdays)
   - Manage schedules in real time: trigger on-demand ("Run Now"), Pause, Resume, or Delete with persistent storage in `output/scheduled_jobs.json`.

3. **Agent Contact Discovery**:
   - Extracts complete Realtor contact info:
     - **Agent Name** (e.g., `EDITH CASTILLO, RAJUL SHAH`, `Karen Blackley`)
     - **Direct Agent Phone Number** (e.g., `905-401-0633`)
     - **Agent Email / Relay Route** (identifies direct email addresses or official Realtor.ca contact form routing)
     - **Brokerage Name & Brokerage Phone Number**
   - Clickable `tel:` and email/portal routes directly in the web UI.

4. **Drone & Aerial Photography Heuristic Classifier**:
   - Downloads and inspects the **entire accessible photo gallery** (up to 50 high-resolution photos per listing).
   - Uses image dimensions, sky/horizon layout, roof/foliage/lot texture heuristics, and listing descriptions to label listings:
     - ⚡ `No drone photos detected` (High Priority Marketing Leads for UPCA)
     - 🚁 `Drone photos detected`
     - ❓ `Uncertain, manual review needed`

5. **Persistent History Store & Exports**:
   - SQLite database (`output/listing_history.db`) and JSON mirror (`output/listing_history.json`) track `date_first_seen` across runs.
   - One-click downloads for:
     - Excel Workbook (`output/upca_drone_leads.xlsx`)
     - UPCA Drone Leads CSV (`output/upca_drone_leads.csv`)
     - Interactive HTML Visual Review Report (`output/drone_review_report.html`)

---

## Compliance & Terms Notice

- **`robots.txt`**: Realtor.ca's `robots.txt` (`https://www.realtor.ca/robots.txt`) specifies `Allow: /` for `User-agent: *` with `Content-Signal: search=yes,ai-train=no,use=reference`.
- **Terms of Use**: Under `https://www.realtor.ca/terms-of-use`, content on Realtor.ca is provided for **private, non-commercial use by individuals**. Automated commercial scraping, redistribution, or AI model training is prohibited by their Terms of Use.
- **Authorized Official Data Source**: For commercial production pipelines or brokerage integrations, use the licensed **CREA DDF® (Data Distribution Facility) Web API** (RESO / OData v4 standard): <https://www.crea.ca/technology/data-distribution-facility/>.
- **Polite Access Only**: This system strictly enforces `robots.txt` verification via `protego`, polite request delays, and halts immediately on HTTP `403`/`429` or challenge pages. It **never** bypasses anti-bot systems and **never** performs automated messaging.

---

## Quick Start (Dashboard UI)

### 1. Launch the Dashboard
Run this PowerShell command inside the project root:
```powershell
.\.venv\Scripts\python.exe dashboard.py
```

### 2. Open Your Browser
Navigate to:
```text
http://127.0.0.1:8501
```

From the dashboard you can:
- Run a scrape immediately on Thorold, St. Catharines, Niagara Falls, or any Canadian location.
- Set up automated cron jobs to scrape new listings every morning or at your preferred interval.
- Filter and search collected listings by agent name, phone number, address, or brokerage.
- Download updated Excel spreadsheets and CSV exports with a single click.

---

## CLI Usage (PowerShell)

You can also run the scraper directly from the command line:

```powershell
# Scrape Thorold, ON for all listings
.\.venv\Scripts\python.exe .\main.py --location "Thorold, Ontario" --limit 10

# Filter specifically for listings WITHOUT drone photos (UPCA Leads)
.\.venv\Scripts\python.exe .\main.py --location "Thorold, Ontario" --limit 10 --filter no_drone

# Filter for listings WITH drone photos
.\.venv\Scripts\python.exe .\main.py --location "Thorold, Ontario" --limit 10 --filter has_drone
```

### Command-Line Arguments:
- `--location`, `-l`: Target Canadian location (e.g. `"Thorold, Ontario"`).
- `--limit`, `-n`: Number of listings to collect (default: `10`, max: `50`).
- `--filter`, `-f`: Drone filter (`all`, `no_drone`, `has_drone`, `uncertain`).
- `--max-images`: Maximum gallery photos to inspect per property (default: `50`).
- `--delay`: Polite pause between HTTP requests in seconds (default: `1.0`).

---

## Project Structure

```text
upca-scrapling/
  dashboard.py             # FastAPI Web Application & APScheduler Dashboard
  main.py                  # Scraper engine & CLI orchestration (7 modular stages)
  history_store.py         # SQLite + JSON persistent listing history store
  drone_analyzer.py        # Full-gallery computer vision classifier & HTML report generator
  requirements.txt         # Dependencies (scrapling, fastapi, apscheduler, openpyxl, etc.)
  README.md                # Documentation and operational manual
  output/
    upca_drone_leads.xlsx      # Primary Excel report with Agent Name, Phone, Email & Drone Status
    realtor_listings.xlsx      # Full property specifications Excel sheet
    upca_drone_leads.csv       # Lead spreadsheet with Agent contact details
    realtor_listings.csv       # Complete CSV export
    drone_review_report.html   # Visual HTML gallery review report
    listing_history.db         # Persistent SQLite database
    listing_history.json       # JSON mirror of database
    scheduled_jobs.json        # Persistent cron & interval schedule configurations
    scrape_log.txt             # Operational log file
    images/                    # Downloaded property galleries for visual verification
```
