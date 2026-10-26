"""
UPCA Real Estate Marketing & Media - Realtor.ca Scraper & Drone Lead Intelligence Dashboard.
Built with FastAPI, APScheduler, and Scrapling v0.5.
Provides on-demand scraping, automated cron/interval scheduling, and real-time lead tracking
with Agent Name, Phone Number, Email contact route, and Drone Photo detection.
"""

from __future__ import annotations

import collections
import contextlib
import datetime
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Local application imports
from drone_analyzer import generate_html_review_report
from history_store import ListingHistoryStore
from main import (
    LABEL_DRONE_DETECTED,
    LABEL_NO_DRONE,
    LABEL_UNCERTAIN,
    run_scraper,
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
IMAGES_DIR = OUTPUT_DIR / "images"
DB_PATH = OUTPUT_DIR / "listing_history.db"
JSON_HISTORY_PATH = OUTPUT_DIR / "listing_history.json"
SCHEDULE_JOBS_FILE = OUTPUT_DIR / "scheduled_jobs.json"
EXCEL_PATH = OUTPUT_DIR / "upca_drone_leads.xlsx"
FALLBACK_EXCEL_PATH = OUTPUT_DIR / "realtor_listings.xlsx"
CSV_PATH = OUTPUT_DIR / "upca_drone_leads.csv"
HTML_REPORT_PATH = OUTPUT_DIR / "drone_review_report.html"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# -----------------------------------------------------------------------------
# In-memory Log Capture for Real-time Dashboard Streaming
# -----------------------------------------------------------------------------
log_buffer = collections.deque(maxlen=600)


def _now_hms() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


class MemoryLogHandler(logging.Handler):
    is_dashboard_handler = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            log_buffer.append(
                {
                    "timestamp": datetime.datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                    "level": record.levelname,
                    "message": msg,
                }
            )
        except Exception:
            pass


mem_handler = MemoryLogHandler()
mem_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger("realtor_scraper").addHandler(mem_handler)


# -----------------------------------------------------------------------------
# Scrape Execution Manager (Thread-safe state)
# -----------------------------------------------------------------------------
class ScrapeManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.is_running: bool = False
        self.state: str = "idle"  # "idle" | "running" | "completed" | "failed"
        self.current_task: Optional[str] = None
        self.current_step: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.last_summary: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    def start_scrape(
        self,
        location: str,
        limit: int = 10,
        drone_filter: str = "all",
        delay: float = 1.0,
        max_images: int = 50,
        task_label: str = "Manual On-Demand Scrape",
    ) -> bool:
        with self.lock:
            if self.is_running:
                return False
            self.is_running = True
            self.state = "running"
            self.current_task = f"{task_label} — {location} (filter: {drone_filter}, limit: {limit})"
            self.current_step = f"Initializing scraper for {location}..."
            self.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.finished_at = None
            self.last_error = None

        log_buffer.append(
            {
                "timestamp": _now_hms(),
                "level": "START",
                "message": f"🚀 STARTED: {task_label} for '{location}' (Limit: {limit}, Filter: '{drone_filter}')",
            }
        )

        thread = threading.Thread(
            target=self._run_worker,
            args=(location, limit, drone_filter, delay, max_images),
            daemon=True,
        )
        thread.start()
        return True

    def _run_worker(
        self,
        location: str,
        limit: int,
        drone_filter: str,
        delay: float,
        max_images: int,
    ) -> None:
        scraper_logger = logging.getLogger("realtor_scraper")
        if mem_handler not in scraper_logger.handlers:
            scraper_logger.addHandler(mem_handler)

        try:
            summary = run_scraper(
                location=location,
                max_listings=limit,
                max_images_per_listing=max_images,
                request_delay=delay,
                drone_filter=drone_filter,
                output_dir=OUTPUT_DIR,
            )
            inspected = summary.get("listings_inspected", summary.get("listings_collected", 0))
            collected = summary.get("listings_collected", 0)
            no_drone = summary.get("no_drone_count", 0)
            has_drone = summary.get("drone_detected_count", 0)
            photos = summary.get("photos_downloaded", 0)

            if inspected == 0 and summary.get("listings_skipped", 0) > 0:
                err_text = f"0 listings could be processed for '{location}' (skipped: {summary.get('listings_skipped')}). Check console logs above."
                log_buffer.append(
                    {
                        "timestamp": _now_hms(),
                        "level": "ERROR",
                        "message": f"❌ FAILED: {err_text}",
                    }
                )
                with self.lock:
                    self.last_summary = summary
                    self.last_error = err_text
                    self.state = "failed"
            else:
                done_msg = (
                    f"✅ COMPLETED: '{location}' — Inspected {inspected} listings "
                    f"(Matched Filter: {collected} | ⚡ Leads Without Drone: {no_drone} | "
                    f"🚁 With Drone: {has_drone} | 📷 Photos Analyzed: {photos})"
                )
                log_buffer.append(
                    {
                        "timestamp": _now_hms(),
                        "level": "SUCCESS",
                        "message": done_msg,
                    }
                )
                with self.lock:
                    self.last_summary = summary
                    self.last_error = None
                    self.state = "completed"
        except Exception as exc:
            err_msg = str(exc)
            scraper_logger.error("Scrape run failed: %s", exc)
            log_buffer.append(
                {
                    "timestamp": _now_hms(),
                    "level": "ERROR",
                    "message": f"❌ FAILED: Scrape aborted due to error: {err_msg}",
                }
            )
            with self.lock:
                self.last_error = err_msg
                self.state = "failed"
        finally:
            with self.lock:
                self.is_running = False
                self.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            latest_msg = log_buffer[-1]["message"] if log_buffer else (self.current_step or "Ready")
            return {
                "is_running": self.is_running,
                "state": self.state,
                "current_task": self.current_task,
                "current_step": latest_msg,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "last_summary": self.last_summary,
                "last_error": self.last_error,
                "logs": list(log_buffer),
            }


scrape_mgr = ScrapeManager()

# -----------------------------------------------------------------------------
# Persistent Scheduler (APScheduler + JSON Store)
# -----------------------------------------------------------------------------
scheduler = BackgroundScheduler(daemon=True)


def load_scheduled_jobs_config() -> List[Dict[str, Any]]:
    if not SCHEDULE_JOBS_FILE.exists():
        return []
    try:
        data = json.loads(SCHEDULE_JOBS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_scheduled_jobs_config(jobs: List[Dict[str, Any]]) -> None:
    SCHEDULE_JOBS_FILE.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def scheduled_job_runner(job_id: str) -> None:
    jobs = load_scheduled_jobs_config()
    job_cfg = next((j for j in jobs if j.get("id") == job_id), None)
    if not job_cfg:
        return

    location = job_cfg.get("location", "Thorold, Ontario")
    limit = int(job_cfg.get("limit", 10))
    drone_filter = job_cfg.get("drone_filter", "no_drone")
    label = f"Scheduled Cron Job [{job_cfg.get('name', job_id)}]"

    logging.getLogger("realtor_scraper").info("Triggering %s for %s", label, location)
    started = scrape_mgr.start_scrape(
        location=location,
        limit=limit,
        drone_filter=drone_filter,
        delay=1.0,
        max_images=50,
        task_label=label,
    )
    if not started:
        logging.getLogger("realtor_scraper").warning(
            "Scheduled job '%s' skipped because a scraper job is already running.",
            job_cfg.get("name"),
        )

    # Update last run timestamp in config
    for j in jobs:
        if j.get("id") == job_id:
            j["last_run"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            break
    save_scheduled_jobs_config(jobs)


def register_job_in_scheduler(job_cfg: Dict[str, Any]) -> None:
    job_id = job_cfg["id"]
    schedule_type = job_cfg.get("schedule_type", "cron")

    try:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

        if schedule_type == "cron":
            cron_expr = job_cfg.get("cron_expression", "0 9 * * *")
            trigger = CronTrigger.from_crontab(cron_expr)
        elif schedule_type == "interval":
            hours = int(job_cfg.get("interval_hours", 6))
            minutes = int(job_cfg.get("interval_minutes", 0))
            if hours == 0 and minutes == 0:
                hours = 6
            trigger = IntervalTrigger(hours=hours, minutes=minutes)
        else:
            trigger = CronTrigger.from_crontab("0 9 * * *")

        scheduler.add_job(
            scheduled_job_runner,
            trigger=trigger,
            id=job_id,
            name=job_cfg.get("name", job_id),
            args=[job_id],
            replace_existing=True,
        )

        if job_cfg.get("status") == "paused":
            scheduler.pause_job(job_id)

    except Exception as exc:
        logging.getLogger("realtor_scraper").error(
            "Failed to schedule job %s: %s", job_id, exc
        )


def sync_scheduler_from_config() -> None:
    jobs = load_scheduled_jobs_config()
    for job_cfg in jobs:
        register_job_in_scheduler(job_cfg)


# -----------------------------------------------------------------------------
# Database Backfill Helper (Ensures historical records have agent_name)
# -----------------------------------------------------------------------------
def backfill_history_db() -> None:
    if not DB_PATH.exists():
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(listing_history)")
            cols = {row[1] for row in cur.fetchall()}
            for col in [
                "agent_name",
                "agent_phone",
                "agent_email",
                "brokerage_name",
                "brokerage_phone",
            ]:
                if col not in cols:
                    conn.execute(f"ALTER TABLE listing_history ADD COLUMN {col} TEXT")

            cur.execute(
                "SELECT listing_id, agent_or_brokerage FROM listing_history WHERE agent_name IS NULL OR agent_name = ''"
            )
            rows = cur.fetchall()
            for lid, aob in rows:
                if not aob:
                    continue
                m = re.match(r"^(.*?)\s*\((.*?)\)$", aob)
                if m:
                    agent, brok = m.group(1).strip(), m.group(2).strip()
                    cur.execute(
                        "UPDATE listing_history SET agent_name = ?, brokerage_name = ? WHERE listing_id = ?",
                        (agent, brok, lid),
                    )
                else:
                    cur.execute(
                        "UPDATE listing_history SET agent_name = ? WHERE listing_id = ?",
                        (aob.strip(), lid),
                    )
            conn.commit()
    except Exception:
        pass


# -----------------------------------------------------------------------------
# FastAPI Application Setup
# -----------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    backfill_history_db()
    sync_scheduler_from_config()
    if not scheduler.running:
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown()


app = FastAPI(
    title="UPCA Real Estate Marketing & Media - Scraper & Scheduler",
    description="Realtor.ca Listing Scraper, Drone Photo Lead Generator, and Cron Scheduler",
    version="1.0.0",
    lifespan=lifespan,
)

# Mount local output images for in-browser visual inspection
if IMAGES_DIR.exists():
    app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")
    app.mount("/report/images", StaticFiles(directory=str(IMAGES_DIR)), name="report_images")


# -----------------------------------------------------------------------------
# Pydantic Request Models
# -----------------------------------------------------------------------------
class ScrapeRequest(BaseModel):
    location: str = Field(default="Thorold, Ontario")
    limit: int = Field(default=10, ge=1, le=50)
    filter: str = Field(default="all")  # all, no_drone, has_drone, uncertain
    delay: float = Field(default=1.0, ge=0.5, le=5.0)
    max_images: int = Field(default=50, ge=1, le=100)


class ScheduleJobRequest(BaseModel):
    name: str = Field(..., description="Job identifier label")
    location: str = Field(default="Thorold, Ontario")
    limit: int = Field(default=10, ge=1, le=50)
    filter: str = Field(default="no_drone")
    schedule_type: str = Field(default="cron")  # "cron" or "interval"
    cron_preset: Optional[str] = None
    cron_expression: Optional[str] = "0 9 * * *"
    interval_hours: Optional[int] = 6
    interval_minutes: Optional[int] = 0


# -----------------------------------------------------------------------------
# API Endpoints
# -----------------------------------------------------------------------------
@app.get("/api/stats")
def get_stats() -> Dict[str, Any]:
    history_store = ListingHistoryStore(DB_PATH, JSON_HISTORY_PATH)
    all_recs = history_store.get_all_records()

    no_drone_count = sum(1 for r in all_recs if r.get("drone_photo_status") == LABEL_NO_DRONE)
    has_drone_count = sum(1 for r in all_recs if r.get("drone_photo_status") == LABEL_DRONE_DETECTED)
    uncertain_count = sum(1 for r in all_recs if r.get("drone_photo_status") == LABEL_UNCERTAIN)

    today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    new_today_count = sum(
        1 for r in all_recs if str(r.get("date_first_seen", "")).startswith(today_str)
    )

    jobs = load_scheduled_jobs_config()
    active_jobs = sum(1 for j in jobs if j.get("status", "active") == "active")

    return {
        "total_listings": len(all_recs),
        "no_drone_leads": no_drone_count,
        "has_drone_count": has_drone_count,
        "uncertain_count": uncertain_count,
        "new_today_count": new_today_count,
        "active_jobs_count": active_jobs,
    }


@app.get("/api/listings")
def get_listings(
    filter: str = "all",
    search: str = "",
    limit: int = 100,
) -> Dict[str, Any]:
    history_store = ListingHistoryStore(DB_PATH, JSON_HISTORY_PATH)
    records = history_store.get_all_records(drone_filter=filter)

    # Clean up empty agent contacts with fallback to agent_or_brokerage
    for r in records:
        if not r.get("agent_name") and r.get("agent_or_brokerage"):
            aob = str(r["agent_or_brokerage"])
            m = re.match(r"^(.*?)\s*\((.*?)\)$", aob)
            if m:
                r["agent_name"] = m.group(1).strip()
                r["brokerage_name"] = r.get("brokerage_name") or m.group(2).strip()
            else:
                r["agent_name"] = aob.strip()

        # Generate a thumbnail relative link if local images exist
        listing_id = r.get("listing_id")
        # Check folder in images directory
        r["thumbnail_url"] = None
        for folder_candidate in IMAGES_DIR.glob(f"*{listing_id}*"):
            if folder_candidate.is_dir():
                first_img = next(folder_candidate.glob("photo_*.jpg"), None) or next(
                    folder_candidate.glob("photo_*.png"), None
                )
                if first_img:
                    r["thumbnail_url"] = f"/images/{folder_candidate.name}/{first_img.name}"
                    break

        if not r.get("thumbnail_url") and r.get("canonical_url"):
            # Try by numeric sequence folder or listing_id
            pass

    if search:
        s = search.lower()
        records = [
            r
            for r in records
            if s in (r.get("property_address") or "").lower()
            or s in (r.get("city") or "").lower()
            or s in (r.get("agent_name") or "").lower()
            or s in (r.get("agent_phone") or "").lower()
            or s in (r.get("agent_email") or "").lower()
            or s in (r.get("brokerage_name") or "").lower()
            or s in (r.get("mls_number") or "").lower()
        ]

    return {
        "count": len(records),
        "filter": filter,
        "records": records[:limit],
    }


@app.post("/api/scrape/start")
def start_scrape(req: ScrapeRequest) -> Dict[str, Any]:
    started = scrape_mgr.start_scrape(
        location=req.location,
        limit=req.limit,
        drone_filter=req.filter,
        delay=req.delay,
        max_images=req.max_images,
        task_label="Manual On-Demand Scrape",
    )
    if not started:
        raise HTTPException(
            status_code=409,
            detail="A scrape job is already currently running. Please wait for it to finish.",
        )
    return {"message": "Scrape started successfully", "location": req.location, "filter": req.filter}


@app.get("/api/scrape/status")
def get_scrape_status() -> Dict[str, Any]:
    return scrape_mgr.get_status()


@app.post("/api/scrape/clear_logs")
def clear_scrape_logs() -> Dict[str, str]:
    log_buffer.clear()
    if not scrape_mgr.is_running:
        scrape_mgr.state = "idle"
        scrape_mgr.last_error = None
    return {"message": "Logs cleared"}


# -----------------------------------------------------------------------------
# Cron & Scheduled Jobs Management API
# -----------------------------------------------------------------------------
@app.get("/api/jobs")
def get_jobs() -> List[Dict[str, Any]]:
    jobs = load_scheduled_jobs_config()
    res = []
    for j in jobs:
        job_id = j["id"]
        sched_job = scheduler.get_job(job_id)
        next_run = None
        next_run_dt = getattr(sched_job, "next_run_time", None) if sched_job else None
        if next_run_dt:
            next_run = next_run_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

        res.append(
            {
                **j,
                "next_run_time": next_run,
                "is_paused": j.get("status") == "paused",
            }
        )
    return res


@app.post("/api/jobs")
def create_scheduled_job(req: ScheduleJobRequest) -> Dict[str, Any]:
    clean_id = re.sub(r"[^a-zA-Z0-9_]+", "_", req.name.strip().lower())
    job_id = f"job_{clean_id}_{int(time.time())}"

    # Handle presets
    cron_expr = req.cron_expression or "0 9 * * *"
    sched_type = req.schedule_type
    hours = req.interval_hours or 6
    minutes = req.interval_minutes or 0

    if req.cron_preset == "daily_9am":
        sched_type = "cron"
        cron_expr = "0 9 * * *"
    elif req.cron_preset == "daily_6pm":
        sched_type = "cron"
        cron_expr = "0 18 * * *"
    elif req.cron_preset == "every_1h":
        sched_type = "interval"
        hours = 1
        minutes = 0
    elif req.cron_preset == "every_6h":
        sched_type = "interval"
        hours = 6
        minutes = 0
    elif req.cron_preset == "every_12h":
        sched_type = "interval"
        hours = 12
        minutes = 0
    elif req.cron_preset == "every_30m":
        sched_type = "interval"
        hours = 0
        minutes = 30

    # Validate cron syntax
    if sched_type == "cron":
        try:
            CronTrigger.from_crontab(cron_expr)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid cron expression '{cron_expr}': {exc}",
            )

    job_record = {
        "id": job_id,
        "name": req.name.strip(),
        "location": req.location.strip(),
        "limit": req.limit,
        "drone_filter": req.filter,
        "schedule_type": sched_type,
        "cron_expression": cron_expr,
        "interval_hours": hours,
        "interval_minutes": minutes,
        "status": "active",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "last_run": None,
    }

    jobs = load_scheduled_jobs_config()
    jobs.append(job_record)
    save_scheduled_jobs_config(jobs)

    register_job_in_scheduler(job_record)

    logging.getLogger("realtor_scraper").info(
        "Scheduled new job '%s' (%s, %s)", req.name, sched_type, cron_expr if sched_type == 'cron' else f'{hours}h{minutes}m'
    )
    return {"message": "Job scheduled successfully", "job": job_record}


@app.delete("/api/jobs/{job_id}")
def delete_scheduled_job(job_id: str) -> Dict[str, str]:
    jobs = load_scheduled_jobs_config()
    new_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(new_jobs) == len(jobs):
        raise HTTPException(status_code=404, detail="Job not found")

    save_scheduled_jobs_config(new_jobs)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    logging.getLogger("realtor_scraper").info("Deleted scheduled job: %s", job_id)
    return {"message": f"Job {job_id} deleted successfully"}


@app.post("/api/jobs/{job_id}/pause")
def pause_scheduled_job(job_id: str) -> Dict[str, str]:
    jobs = load_scheduled_jobs_config()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job["status"] = "paused"
    save_scheduled_jobs_config(jobs)
    if scheduler.get_job(job_id):
        scheduler.pause_job(job_id)

    logging.getLogger("realtor_scraper").info("Paused scheduled job: %s", job_id)
    return {"message": f"Job {job_id} paused"}


@app.post("/api/jobs/{job_id}/resume")
def resume_scheduled_job(job_id: str) -> Dict[str, str]:
    jobs = load_scheduled_jobs_config()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job["status"] = "active"
    save_scheduled_jobs_config(jobs)
    if scheduler.get_job(job_id):
        scheduler.resume_job(job_id)

    logging.getLogger("realtor_scraper").info("Resumed scheduled job: %s", job_id)
    return {"message": f"Job {job_id} resumed"}


@app.post("/api/jobs/{job_id}/run_now")
def run_scheduled_job_now(job_id: str, background_tasks: BackgroundTasks) -> Dict[str, str]:
    jobs = load_scheduled_jobs_config()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    background_tasks.add_task(scheduled_job_runner, job_id)
    return {"message": f"Triggered execution for {job.get('name', job_id)}"}


# -----------------------------------------------------------------------------
# File Downloads & Reports
# -----------------------------------------------------------------------------
@app.get("/download/excel")
def download_excel() -> FileResponse:
    target = EXCEL_PATH if EXCEL_PATH.exists() else FALLBACK_EXCEL_PATH
    if not target.exists():
        raise HTTPException(status_code=404, detail="Excel file not generated yet. Run a scrape first.")
    return FileResponse(
        path=target,
        filename=target.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/download/csv")
def download_csv() -> FileResponse:
    if not CSV_PATH.exists():
        raise HTTPException(status_code=404, detail="CSV file not generated yet. Run a scrape first.")
    return FileResponse(
        path=CSV_PATH,
        filename=CSV_PATH.name,
        media_type="text/csv",
    )


@app.get("/report/visual", response_class=HTMLResponse)
def view_visual_report() -> HTMLResponse:
    if DB_PATH.exists():
        need_regen = (
            not HTML_REPORT_PATH.exists()
            or DB_PATH.stat().st_mtime > HTML_REPORT_PATH.stat().st_mtime
        )
        if need_regen:
            try:
                generate_html_review_report([], HTML_REPORT_PATH, "All Locations")
            except Exception as exc:
                logging.getLogger("realtor_scraper").error(
                    "Failed to regenerate visual report: %s", exc
                )

    if not HTML_REPORT_PATH.exists():
        return HTMLResponse(
            "<h2>Visual Review Report has not been generated yet. Run a scrape to inspect listings.</h2>",
            status_code=404,
        )
    content = HTML_REPORT_PATH.read_text(encoding="utf-8")
    # Rewrite relative image paths to root-absolute /images/ so they load seamlessly
    content = content.replace('src="images/', 'src="/images/')
    return HTMLResponse(content=content)


# -----------------------------------------------------------------------------
# Single Page Application HTML Dashboard Frontend
# -----------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>UPCA Real Estate Marketing & Media - Realtor.ca Scraper & Scheduler</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          fontFamily: {
            sans: ['Inter', 'sans-serif'],
            mono: ['JetBrains Mono', 'monospace'],
          },
          colors: {
            brand: {
              50: '#f0fdf4',
              100: '#dcfce7',
              500: '#22c55e',
              600: '#16a34a',
              700: '#15803d',
              900: '#14532d',
            },
            upca: {
              blue: '#0284c7',
              dark: '#0f172a',
              card: '#1e293b',
              accent: '#38bdf8',
            }
          }
        }
      }
    }
  </script>
  <style>
    body { font-family: 'Inter', sans-serif; }
    .mono { font-family: 'JetBrains Mono', monospace; }
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-track { background: #1e293b; }
    ::-webkit-scrollbar-thumb { background: #475569; border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: #64748b; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col antialiased selection:bg-brand-500 selection:text-white">

  <!-- TOP NAVIGATION HEADER -->
  <header class="border-b border-slate-800 bg-slate-900/90 backdrop-blur sticky top-0 z-40">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-emerald-500 to-sky-500 flex items-center justify-center shadow-lg shadow-emerald-500/20">
          <svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 22V12h6v10"/>
          </svg>
        </div>
        <div>
          <div class="flex items-center space-x-2">
            <h1 class="text-lg font-bold tracking-tight text-white">UPCA Real Estate Marketing & Media</h1>
            <span class="px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">Realtor.ca v0.5</span>
          </div>
          <p class="text-xs text-slate-400">Automated Drone Lead Intelligence & Agent Contact Discovery</p>
        </div>
      </div>

      <!-- Quick Actions -->
      <div class="flex items-center space-x-2">
        <div id="scrapeStatusIndicator" class="flex items-center space-x-2 px-3 py-1.5 rounded-full bg-slate-800/80 border border-slate-700 text-xs">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-500 animate-pulse"></span>
          <span id="statusText" class="font-medium text-slate-300">Ready</span>
        </div>

        <a href="/download/excel" download class="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white transition shadow-sm">
          <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
          Excel (.xlsx)
        </a>
        <a href="/download/csv" download class="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition">
          <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
          Leads CSV
        </a>
        <a href="/report/visual" target="_blank" class="inline-flex items-center px-3 py-1.5 text-xs font-medium rounded-lg bg-sky-600/80 hover:bg-sky-500 text-white transition">
          <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
          Visual Report
        </a>
      </div>
    </div>
  </header>

  <!-- MAIN CONTAINER -->
  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 flex-1 w-full space-y-6">

    <!-- TOP KPI STATS CARDS -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm relative overflow-hidden">
        <div class="text-xs font-medium text-slate-400 uppercase tracking-wider">Total Listings Seen</div>
        <div class="mt-2 text-2xl sm:text-3xl font-extrabold text-white" id="statTotalListings">--</div>
        <div class="mt-1 text-xs text-slate-500" id="statNewToday">-- new today</div>
        <div class="absolute right-3 bottom-3 text-slate-800 pointer-events-none">
          <svg class="w-12 h-12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/></svg>
        </div>
      </div>

      <div class="bg-slate-900 border border-emerald-500/30 rounded-xl p-4 shadow-sm relative overflow-hidden bg-gradient-to-br from-emerald-950/20 to-slate-900">
        <div class="text-xs font-semibold text-emerald-400 uppercase tracking-wider flex items-center">
          <span class="w-2 h-2 rounded-full bg-emerald-400 mr-1.5 animate-ping"></span>
          Drone Leads (No Drone)
        </div>
        <div class="mt-2 text-2xl sm:text-3xl font-extrabold text-emerald-400" id="statNoDroneLeads">--</div>
        <div class="mt-1 text-xs text-emerald-500/80">High Opportunity Marketing Leads</div>
        <div class="absolute right-3 bottom-3 text-emerald-900/30 pointer-events-none">
          <svg class="w-12 h-12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
        </div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm relative overflow-hidden">
        <div class="text-xs font-medium text-sky-400 uppercase tracking-wider">Drone Photos Detected</div>
        <div class="mt-2 text-2xl sm:text-3xl font-extrabold text-sky-400" id="statHasDrone">--</div>
        <div class="mt-1 text-xs text-slate-500">Aerial photography present</div>
        <div class="absolute right-3 bottom-3 text-slate-800 pointer-events-none">
          <svg class="w-12 h-12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"/></svg>
        </div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm relative overflow-hidden">
        <div class="text-xs font-medium text-amber-400 uppercase tracking-wider">Active Cron Schedules</div>
        <div class="mt-2 text-2xl sm:text-3xl font-extrabold text-amber-400" id="statActiveJobs">--</div>
        <div class="mt-1 text-xs text-slate-500">Automated recurring jobs</div>
        <div class="absolute right-3 bottom-3 text-slate-800 pointer-events-none">
          <svg class="w-12 h-12" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        </div>
      </div>
    </div>

    <!-- MAIN TWO-COLUMN WORKSPACE: LEFT CONTROLS, RIGHT DATA -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">

      <!-- LEFT COLUMN: SCRAPER CONTROLS & CRON SCHEDULER (5 cols) -->
      <div class="lg:col-span-5 space-y-6">

        <!-- PANEL TABS: ON-DEMAND vs CRON SCHEDULER -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden shadow-sm">
          <div class="flex border-b border-slate-800 bg-slate-950/60 p-1">
            <button id="tabBtnScrape" onclick="switchControlTab('scrape')" class="flex-1 py-2 px-3 text-xs font-medium rounded-lg text-emerald-400 bg-slate-800/80 shadow-sm flex items-center justify-center transition">
              <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
              Run Scraper Now
            </button>
            <button id="tabBtnSchedule" onclick="switchControlTab('schedule')" class="flex-1 py-2 px-3 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 flex items-center justify-center transition">
              <svg class="w-4 h-4 mr-1.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              Cron Scheduler
            </button>
          </div>

          <!-- TAB CONTENT 1: ON-DEMAND SCRAPER -->
          <div id="tabContentScrape" class="p-5 space-y-4">
            <div>
              <label class="block text-xs font-medium text-slate-300 mb-1.5">Target Canadian Location</label>
              <div class="relative">
                <input id="scrapeLocation" type="text" value="Thorold, Ontario" placeholder="City, Province or Postal Code (e.g., Thorold, Ontario)"
                       class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:ring-2 focus:ring-emerald-500 font-sans">
              </div>
              <!-- Quick Location Pills -->
              <div class="mt-2 flex flex-wrap gap-1.5">
                <button type="button" onclick="setLocation('Thorold, Ontario')" class="px-2 py-0.5 text-xs rounded bg-slate-800 text-slate-400 hover:text-emerald-400 hover:bg-slate-700 transition">Thorold, ON</button>
                <button type="button" onclick="setLocation('St. Catharines, Ontario')" class="px-2 py-0.5 text-xs rounded bg-slate-800 text-slate-400 hover:text-emerald-400 hover:bg-slate-700 transition">St. Catharines, ON</button>
                <button type="button" onclick="setLocation('Niagara Falls, Ontario')" class="px-2 py-0.5 text-xs rounded bg-slate-800 text-slate-400 hover:text-emerald-400 hover:bg-slate-700 transition">Niagara Falls, ON</button>
                <button type="button" onclick="setLocation('Welland, Ontario')" class="px-2 py-0.5 text-xs rounded bg-slate-800 text-slate-400 hover:text-emerald-400 hover:bg-slate-700 transition">Welland, ON</button>
                <button type="button" onclick="setLocation('Toronto, Ontario')" class="px-2 py-0.5 text-xs rounded bg-slate-800 text-slate-400 hover:text-emerald-400 hover:bg-slate-700 transition">Toronto, ON</button>
              </div>
            </div>

            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="block text-xs font-medium text-slate-300 mb-1.5">What to Scrape</label>
                <select id="scrapeFilter" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:ring-2 focus:ring-emerald-500">
                  <option value="all">🎯 All Listings</option>
                  <option value="no_drone" selected>⚡ Only Leads (Without Drone)</option>
                  <option value="has_drone">🚁 With Drone Photos</option>
                  <option value="uncertain">❓ Uncertain (Manual Review)</option>
                </select>
              </div>
              <div>
                <label class="block text-xs font-medium text-slate-300 mb-1.5">Listing Limit</label>
                <select id="scrapeLimit" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:ring-2 focus:ring-emerald-500">
                  <option value="5">5 Listings</option>
                  <option value="10" selected>10 Listings</option>
                  <option value="20">20 Listings</option>
                  <option value="35">35 Listings</option>
                </select>
              </div>
            </div>

            <!-- Start Button -->
            <button id="btnStartScrape" onclick="triggerManualScrape()" class="w-full py-2.5 px-4 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold text-sm shadow-md hover:shadow-emerald-500/20 transition flex items-center justify-center space-x-2">
              <svg id="scrapeSpinner" class="hidden animate-spin -ml-1 mr-2 h-4 w-4 text-white" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>
              <span id="btnStartScrapeText">Start Scrape Now</span>
            </button>
          </div>

          <!-- TAB CONTENT 2: CRON SCHEDULER -->
          <div id="tabContentSchedule" class="p-5 space-y-4 hidden">
            <div>
              <label class="block text-xs font-medium text-slate-300 mb-1">Schedule Job Name</label>
              <input id="schedName" type="text" placeholder="e.g. Thorold Morning Drone Hunt" value="Thorold Daily Drone Lead Finder"
                     class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:ring-2 focus:ring-sky-500">
            </div>

            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="block text-xs font-medium text-slate-300 mb-1">Target Location</label>
                <input id="schedLocation" type="text" value="Thorold, Ontario"
                       class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white focus:ring-2 focus:ring-sky-500">
              </div>
              <div>
                <label class="block text-xs font-medium text-slate-300 mb-1">Filter Type</label>
                <select id="schedFilter" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-white">
                  <option value="no_drone" selected>⚡ Only Leads (Without Drone)</option>
                  <option value="all">🎯 All Listings</option>
                  <option value="has_drone">🚁 With Drone Photos</option>
                </select>
              </div>
            </div>

            <div>
              <label class="block text-xs font-medium text-slate-300 mb-1">Run Frequency / Cron Preset</label>
              <select id="schedPreset" onchange="toggleCustomCronInput()" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:ring-2 focus:ring-sky-500">
                <option value="daily_9am" selected>☀️ Every day at 9:00 AM (0 9 * * *)</option>
                <option value="daily_6pm">🌆 Every day at 6:00 PM (0 18 * * *)</option>
                <option value="every_6h">⏱️ Every 6 Hours</option>
                <option value="every_12h">⏱️ Every 12 Hours</option>
                <option value="every_1h">⏱️ Every 1 Hour</option>
                <option value="custom">⚙️ Custom Cron Expression...</option>
              </select>
            </div>

            <div id="customCronDiv" class="hidden">
              <label class="block text-xs font-medium text-amber-400 mb-1">Custom Cron Expression (standard 5-part)</label>
              <input id="customCronExpr" type="text" value="0 9 * * *" placeholder="min hour dom mon dow (e.g. 0 9 * * *)"
                     class="w-full bg-slate-950 border border-amber-500/40 rounded-lg px-3 py-1.5 text-xs text-white mono">
              <p class="text-[10px] text-slate-500 mt-1">Example: <code class="text-amber-400">0 9 * * 1-5</code> runs Mon-Fri at 9 AM.</p>
            </div>

            <button onclick="createNewScheduleJob()" class="w-full py-2 px-4 rounded-lg bg-sky-600 hover:bg-sky-500 text-white font-medium text-xs transition flex items-center justify-center space-x-1.5">
              <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6v6m0 0v6m0-6h6m-6 0H6"/></svg>
              <span>Add Scheduled Job</span>
            </button>

            <!-- Active Schedules Table -->
            <div class="mt-4 pt-3 border-t border-slate-800">
              <div class="text-xs font-semibold text-slate-300 mb-2 flex items-center justify-between">
                <span>Active Scheduled Cron Jobs</span>
                <button onclick="loadScheduledJobs()" class="text-[10px] text-sky-400 hover:underline">Refresh</button>
              </div>
              <div id="jobsListContainer" class="space-y-2 max-h-48 overflow-y-auto pr-1 text-xs">
                <div class="text-slate-500 text-center py-2">Loading schedules...</div>
              </div>
            </div>
          </div>
        </div>

        <!-- LIVE STREAMING CONSOLE LOGS -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden shadow-sm">
          <div class="px-4 py-2.5 bg-slate-950/80 border-b border-slate-800 flex items-center justify-between">
            <div class="flex items-center space-x-2">
              <span id="consoleDot" class="w-2.5 h-2.5 rounded-full bg-slate-500"></span>
              <span class="text-xs font-semibold uppercase tracking-wider text-slate-200">Scrapling Live Console</span>
              <span id="consoleStateBadge" class="px-2 py-0.5 text-[10px] font-bold uppercase rounded bg-slate-800 text-slate-400 border border-slate-700">IDLE</span>
            </div>
            <button onclick="clearConsole()" class="text-[11px] text-slate-400 hover:text-slate-200">Clear</button>
          </div>
          <!-- Live Progress / Status Banner -->
          <div id="consoleStatusBanner" class="px-4 py-2 bg-slate-950/50 border-b border-slate-800/80 text-xs text-slate-400 flex flex-col space-y-1.5">
            <div class="flex items-center justify-between">
              <span id="consoleBannerText" class="font-medium truncate">Ready — Select a location above and click "Start Scrape Now".</span>
            </div>
            <div id="consoleProgressBarWrap" class="hidden w-full bg-slate-800 h-1.5 rounded-full overflow-hidden">
              <div class="bg-amber-400 h-full w-full animate-pulse"></div>
            </div>
          </div>
          <div id="consoleLogs" class="p-3 bg-black/75 mono text-[11px] text-slate-300 h-72 overflow-y-auto space-y-1.5 leading-relaxed">
            <div class="text-slate-500 italic">Waiting for scrape activity...</div>
          </div>
        </div>

      </div>

      <!-- RIGHT COLUMN: LEADS & LISTINGS TABLE (7 cols) -->
      <div class="lg:col-span-7 space-y-4">

        <!-- Table Filters & Search -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 shadow-sm flex flex-col sm:flex-row items-center justify-between gap-3">
          <!-- Filter Tabs -->
          <div class="flex items-center space-x-1 bg-slate-950 p-1 rounded-lg border border-slate-800 w-full sm:w-auto">
            <button id="pillAll" onclick="setTableFilter('all')" class="px-3 py-1 text-xs font-medium rounded-md text-white bg-slate-800 transition">All (<span id="countAll">0</span>)</button>
            <button id="pillNoDrone" onclick="setTableFilter('no_drone')" class="px-3 py-1 text-xs font-medium rounded-md text-slate-400 hover:text-emerald-400 transition">⚡ Leads (<span id="countNoDrone">0</span>)</button>
            <button id="pillHasDrone" onclick="setTableFilter('has_drone')" class="px-3 py-1 text-xs font-medium rounded-md text-slate-400 hover:text-sky-400 transition">🚁 With Drone (<span id="countHasDrone">0</span>)</button>
            <button id="pillUncertain" onclick="setTableFilter('uncertain')" class="px-3 py-1 text-xs font-medium rounded-md text-slate-400 hover:text-amber-400 transition">❓ Review (<span id="countUncertain">0</span>)</button>
          </div>

          <!-- Search Box -->
          <div class="relative w-full sm:w-64">
            <input id="searchInput" oninput="debounceSearch()" type="text" placeholder="Search agent, phone, address..."
                   class="w-full bg-slate-950 border border-slate-700 rounded-lg pl-8 pr-3 py-1.5 text-xs text-white focus:outline-none focus:ring-2 focus:ring-emerald-500">
            <svg class="w-4 h-4 text-slate-500 absolute left-2.5 top-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>
          </div>
        </div>

        <!-- LISTINGS TABLE -->
        <div class="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden shadow-sm">
          <div class="overflow-x-auto">
            <table class="w-full text-left text-xs">
              <thead class="bg-slate-950/80 border-b border-slate-800 text-slate-400 uppercase tracking-wider text-[10px]">
                <tr>
                  <th class="py-3 px-3">Status / Lead</th>
                  <th class="py-3 px-3">Agent & Contact Info</th>
                  <th class="py-3 px-3">Property Details</th>
                  <th class="py-3 px-3">Price</th>
                  <th class="py-3 px-3">Photos</th>
                  <th class="py-3 px-3 text-right">Actions</th>
                </tr>
              </thead>
              <tbody id="listingsTableBody" class="divide-y divide-slate-800">
                <tr>
                  <td colspan="6" class="text-center py-10 text-slate-500">Loading listings...</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

      </div>

    </div>

  </main>

  <!-- LISTING DETAIL MODAL -->
  <div id="detailModal" class="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm hidden flex items-center justify-center p-4">
    <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto p-6 shadow-2xl relative">
      <button onclick="closeModal()" class="absolute top-4 right-4 text-slate-400 hover:text-white p-1">
        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
      </button>
      <div id="modalContent" class="space-y-4">
        <!-- Injected via JavaScript -->
      </div>
    </div>
  </div>

  <!-- JAVASCRIPT APP LOGIC -->
  <script>
    let currentFilter = 'all';
    let searchQuery = '';
    let searchTimeout = null;
    let isScraperPolling = false;

    // Tab Switching: Scrape vs Schedule
    function switchControlTab(tab) {
      if (tab === 'scrape') {
        document.getElementById('tabContentScrape').classList.remove('hidden');
        document.getElementById('tabContentSchedule').classList.add('hidden');
        document.getElementById('tabBtnScrape').className = 'flex-1 py-2 px-3 text-xs font-medium rounded-lg text-emerald-400 bg-slate-800/80 shadow-sm flex items-center justify-center transition';
        document.getElementById('tabBtnSchedule').className = 'flex-1 py-2 px-3 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 flex items-center justify-center transition';
      } else {
        document.getElementById('tabContentScrape').classList.add('hidden');
        document.getElementById('tabContentSchedule').classList.remove('hidden');
        document.getElementById('tabBtnSchedule').className = 'flex-1 py-2 px-3 text-xs font-medium rounded-lg text-sky-400 bg-slate-800/80 shadow-sm flex items-center justify-center transition';
        document.getElementById('tabBtnScrape').className = 'flex-1 py-2 px-3 text-xs font-medium rounded-lg text-slate-400 hover:text-slate-200 flex items-center justify-center transition';
        loadScheduledJobs();
      }
    }

    function setLocation(loc) {
      document.getElementById('scrapeLocation').value = loc;
      document.getElementById('schedLocation').value = loc;
    }

    function toggleCustomCronInput() {
      const preset = document.getElementById('schedPreset').value;
      const customDiv = document.getElementById('customCronDiv');
      if (preset === 'custom') {
        customDiv.classList.remove('hidden');
      } else {
        customDiv.classList.add('hidden');
      }
    }

    function setTableFilter(filter) {
      currentFilter = filter;
      ['All', 'NoDrone', 'HasDrone', 'Uncertain'].forEach(name => {
        const btn = document.getElementById('pill' + name);
        if (btn) btn.className = 'px-3 py-1 text-xs font-medium rounded-md text-slate-400 hover:text-white transition';
      });
      const activeBtnMap = {
        'all': 'pillAll',
        'no_drone': 'pillNoDrone',
        'has_drone': 'pillHasDrone',
        'uncertain': 'pillUncertain'
      };
      const activeBtn = document.getElementById(activeBtnMap[filter]);
      if (activeBtn) {
        activeBtn.className = 'px-3 py-1 text-xs font-medium rounded-md text-white bg-slate-800 transition';
      }
      loadListings();
    }

    function debounceSearch() {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => {
        searchQuery = document.getElementById('searchInput').value.trim();
        loadListings();
      }, 250);
    }

    // Load Stats
    async function loadStats() {
      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        document.getElementById('statTotalListings').innerText = data.total_listings;
        document.getElementById('statNoDroneLeads').innerText = data.no_drone_leads;
        document.getElementById('statHasDrone').innerText = data.has_drone_count;
        document.getElementById('statActiveJobs').innerText = data.active_jobs_count;
        document.getElementById('statNewToday').innerText = `${data.new_today_count} discovered today`;

        document.getElementById('countAll').innerText = data.total_listings;
        document.getElementById('countNoDrone').innerText = data.no_drone_leads;
        document.getElementById('countHasDrone').innerText = data.has_drone_count;
        document.getElementById('countUncertain').innerText = data.uncertain_count;
      } catch (e) {
        console.error('Failed to load stats', e);
      }
    }

    // Load Listings
    async function loadListings() {
      const tbody = document.getElementById('listingsTableBody');
      try {
        const url = `/api/listings?filter=${encodeURIComponent(currentFilter)}&search=${encodeURIComponent(searchQuery)}`;
        const res = await fetch(url);
        const data = await res.json();

        if (!data.records || data.records.length === 0) {
          tbody.innerHTML = `<tr><td colspan="6" class="text-center py-10 text-slate-500">No listings found matching filter.</td></tr>`;
          return;
        }

        tbody.innerHTML = data.records.map(r => {
          let badgeHtml = '';
          if (r.drone_photo_status === 'No drone photos detected') {
            badgeHtml = `<span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">⚡ Lead: No Drone</span>`;
          } else if (r.drone_photo_status === 'Drone photos detected') {
            badgeHtml = `<span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-sky-500/10 text-sky-400 border border-sky-500/20">🚁 Drone Photos</span>`;
          } else {
            badgeHtml = `<span class="inline-flex items-center px-2 py-0.5 rounded text-[11px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">❓ Needs Review</span>`;
          }

          const agentName = r.agent_name || 'Agent Name Not Listed';
          const agentPhone = r.agent_phone ? `<a href="tel:${r.agent_phone}" class="text-emerald-400 hover:underline flex items-center font-mono text-[11px]"><svg class="w-3 h-3 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 5a2 2 0 012-2h3.28a1 1 0 01.948.684l1.498 4.493a1 1 0 01-.502 1.21l-2.257 1.13a11.042 11.042 0 005.516 5.516l1.13-2.257a1 1 0 011.21-.502l4.493 1.498a1 1 0 01.684.949V19a2 2 0 01-2 2h-1C9.716 21 3 14.284 3 6V5z"/></svg>${r.agent_phone}</a>` : '<span class="text-slate-500 text-[10px]">Phone unavailable</span>';
          
          let emailRoute = '';
          if (r.agent_email && r.agent_email.includes('@')) {
            emailRoute = `<a href="mailto:${r.agent_email}" class="text-sky-400 hover:underline text-[10px] truncate block">${r.agent_email}</a>`;
          } else if (r.agent_email) {
            emailRoute = `<a href="${r.canonical_url}" target="_blank" class="text-slate-400 hover:text-sky-300 text-[10px] flex items-center"><svg class="w-2.5 h-2.5 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>Realtor Contact Form</a>`;
          } else {
            emailRoute = `<a href="${r.canonical_url}" target="_blank" class="text-slate-500 text-[10px]">Realtor.ca Portal</a>`;
          }

          const brokerage = r.brokerage_name || r.agent_or_brokerage || 'Brokerage N/A';

          return `
            <tr class="hover:bg-slate-800/40 transition">
              <td class="py-3 px-3 align-top whitespace-nowrap">
                ${badgeHtml}
                <div class="text-[10px] text-slate-500 mt-1">First seen: ${r.date_first_seen ? r.date_first_seen.split('T')[0] : 'N/A'}</div>
              </td>
              <td class="py-3 px-3 align-top">
                <div class="font-bold text-white text-xs">${agentName}</div>
                <div class="mt-0.5">${agentPhone}</div>
                <div class="mt-0.5">${emailRoute}</div>
                <div class="text-[10px] text-slate-400 mt-0.5 truncate max-w-[200px]" title="${brokerage}">🏢 ${brokerage}</div>
              </td>
              <td class="py-3 px-3 align-top">
                <div class="font-medium text-slate-200 max-w-[220px] truncate" title="${r.property_address}">${r.property_address || 'Address N/A'}</div>
                <div class="text-[11px] text-slate-400">${r.city || ''}, ${r.province || ''}</div>
                <div class="text-[10px] text-slate-500">MLS®: ${r.mls_number || 'N/A'}</div>
              </td>
              <td class="py-3 px-3 align-top whitespace-nowrap">
                <div class="font-bold text-emerald-400 text-sm">${r.asking_price || 'N/A'}</div>
              </td>
              <td class="py-3 px-3 align-top whitespace-nowrap">
                <div class="flex items-center space-x-1.5">
                  <span class="px-2 py-0.5 rounded bg-slate-800 text-[11px] text-slate-300 font-mono">${r.num_accessible_photos || 0} pics</span>
                </div>
              </td>
              <td class="py-3 px-3 align-top text-right whitespace-nowrap">
                <div class="flex items-center justify-end space-x-2">
                  <button onclick='viewListingDetail(${JSON.stringify(r).replace(/'/g, "&apos;")})' class="p-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 transition" title="Inspect Details">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
                  </button>
                  <a href="${r.canonical_url}" target="_blank" class="p-1.5 rounded-lg bg-sky-600/30 hover:bg-sky-600/60 text-sky-300 transition" title="Open on Realtor.ca">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>
                  </a>
                </div>
              </td>
            </tr>
          `;
        }).join('');
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" class="text-center py-10 text-red-400">Failed to load listings: ${e.message}</td></tr>`;
      }
    }

    // Update Console Status Banner & Header Badge
    function updateConsoleStateUI(state, bannerText) {
      const dot = document.getElementById('consoleDot');
      const badge = document.getElementById('consoleStateBadge');
      const banner = document.getElementById('consoleStatusBanner');
      const textEl = document.getElementById('consoleBannerText');
      const barWrap = document.getElementById('consoleProgressBarWrap');

      if (state === 'running') {
        dot.className = 'w-2.5 h-2.5 rounded-full bg-amber-400 animate-ping';
        badge.className = 'px-2 py-0.5 text-[10px] font-bold uppercase rounded bg-amber-500/20 text-amber-300 border border-amber-500/40';
        badge.innerText = 'WORKING';
        banner.className = 'px-4 py-2 bg-amber-950/30 border-b border-amber-500/30 text-xs text-amber-200 flex flex-col space-y-1.5';
        textEl.innerText = bannerText || '🟡 Working — Scraping listings and analyzing gallery photos...';
        barWrap.classList.remove('hidden');
      } else if (state === 'completed') {
        dot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-500';
        badge.className = 'px-2 py-0.5 text-[10px] font-bold uppercase rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40';
        badge.innerText = 'COMPLETED';
        banner.className = 'px-4 py-2 bg-emerald-950/30 border-b border-emerald-500/30 text-xs text-emerald-300 flex flex-col space-y-1.5';
        textEl.innerText = bannerText || '🟢 Completed — Scrape finished and database updated.';
        barWrap.classList.add('hidden');
      } else if (state === 'failed') {
        dot.className = 'w-2.5 h-2.5 rounded-full bg-red-500';
        badge.className = 'px-2 py-0.5 text-[10px] font-bold uppercase rounded bg-red-500/20 text-red-300 border border-red-500/40';
        badge.innerText = 'FAILED';
        banner.className = 'px-4 py-2 bg-red-950/30 border-b border-red-500/30 text-xs text-red-300 flex flex-col space-y-1.5';
        textEl.innerText = bannerText || '🔴 Failed — Check console output below for error details.';
        barWrap.classList.add('hidden');
      } else {
        dot.className = 'w-2.5 h-2.5 rounded-full bg-slate-500';
        badge.className = 'px-2 py-0.5 text-[10px] font-bold uppercase rounded bg-slate-800 text-slate-400 border border-slate-700';
        badge.innerText = 'IDLE';
        banner.className = 'px-4 py-2 bg-slate-950/50 border-b border-slate-800/80 text-xs text-slate-400 flex flex-col space-y-1.5';
        textEl.innerText = bannerText || 'Ready — Select a location above and click "Start Scrape Now".';
        barWrap.classList.add('hidden');
      }
    }

    // Trigger Manual Scrape
    async function triggerManualScrape() {
      const location = document.getElementById('scrapeLocation').value.trim();
      const filter = document.getElementById('scrapeFilter').value;
      const limit = parseInt(document.getElementById('scrapeLimit').value);

      if (!location) {
        alert('Please enter a target Canadian location (e.g., Thorold, Ontario)');
        return;
      }

      const btn = document.getElementById('btnStartScrape');
      const btnText = document.getElementById('btnStartScrapeText');
      const spinner = document.getElementById('scrapeSpinner');
      const consoleEl = document.getElementById('consoleLogs');

      btn.disabled = true;
      spinner.classList.remove('hidden');
      btnText.innerText = 'Scraping in progress...';

      // Immediate feedback in Live Console before HTTP round-trip completes
      updateConsoleStateUI('running', `🟡 Working — Starting scrape for ${location} (filter: ${filter}, limit: ${limit})...`);
      const nowStr = new Date().toTimeString().split(' ')[0];
      consoleEl.innerHTML = `<div class="text-amber-300 font-semibold">[${nowStr}] ⏳ Sending scrape request for '${escapeHtml(location)}' (Limit: ${limit}, Filter: ${escapeHtml(filter)})...</div>`;

      try {
        const res = await fetch('/api/scrape/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ location, filter, limit, delay: 1.0, max_images: 50 })
        });
        const data = await res.json();
        if (!res.ok) {
          const errDetail = data.detail || 'Failed to start scrape';
          updateConsoleStateUI('failed', `🔴 Failed: ${errDetail}`);
          consoleEl.innerHTML += `<div class="text-red-400 font-bold">[${nowStr}] ❌ Error: ${escapeHtml(errDetail)}</div>`;
          btn.disabled = false;
          spinner.classList.add('hidden');
          btnText.innerText = 'Start Scrape Now';
        } else {
          startPollingScrape();
        }
      } catch (e) {
        updateConsoleStateUI('failed', `🔴 Network Error: ${e.message}`);
        consoleEl.innerHTML += `<div class="text-red-400 font-bold">[${nowStr}] ❌ Network Error: ${escapeHtml(e.message)}</div>`;
        btn.disabled = false;
        spinner.classList.add('hidden');
        btnText.innerText = 'Start Scrape Now';
      }
    }

    // Polling Scraper Status & Console Logs
    let pollTickCount = 0;
    function startPollingScrape() {
      if (isScraperPolling) return;
      isScraperPolling = true;
      pollTickCount = 0;

      const pollFn = async () => {
        try {
          const res = await fetch('/api/scrape/status');
          const data = await res.json();

          const indicator = document.getElementById('scrapeStatusIndicator');
          const statusText = document.getElementById('statusText');
          const btn = document.getElementById('btnStartScrape');
          const btnText = document.getElementById('btnStartScrapeText');
          const spinner = document.getElementById('scrapeSpinner');

          // Render Logs
          const consoleEl = document.getElementById('consoleLogs');
          if (data.logs && data.logs.length > 0) {
            consoleEl.innerHTML = data.logs.map(l => {
              let color = 'text-slate-300';
              if (l.level === 'ERROR' || l.message.includes('❌')) color = 'text-red-400 font-bold bg-red-950/30 px-1.5 py-0.5 rounded';
              else if (l.level === 'SUCCESS' || l.message.includes('✅ COMPLETED')) color = 'text-emerald-300 font-bold bg-emerald-950/40 px-1.5 py-0.5 rounded border border-emerald-500/30';
              else if (l.level === 'START' || l.message.includes('🚀')) color = 'text-amber-300 font-bold bg-amber-950/30 px-1.5 py-0.5 rounded';
              else if (l.level === 'WARNING') color = 'text-amber-400';
              else if (l.message.includes('No drone photos')) color = 'text-emerald-400 font-semibold';
              else if (l.message.includes('Drone photos detected')) color = 'text-sky-400';
              return `<div class="${color}">[${l.timestamp}] ${escapeHtml(l.message)}</div>`;
            }).join('');
            consoleEl.scrollTop = consoleEl.scrollHeight;
          }

          if (data.is_running) {
            indicator.className = 'flex items-center space-x-2 px-3 py-1.5 rounded-full bg-amber-500/10 border border-amber-500/30 text-xs text-amber-300';
            statusText.innerText = 'Scraping Active';
            btn.disabled = true;
            spinner.classList.remove('hidden');
            btnText.innerText = 'Scraping in progress...';
            updateConsoleStateUI('running', `🟡 WORKING: ${data.current_step || data.current_task || 'Scraping in progress...'}`);

            pollTickCount++;
            if (pollTickCount % 3 === 0) {
              loadStats();
              loadListings();
            }
          } else {
            indicator.className = 'flex items-center space-x-2 px-3 py-1.5 rounded-full bg-slate-800/80 border border-slate-700 text-xs text-slate-300';
            statusText.innerText = 'Ready';
            btn.disabled = false;
            spinner.classList.add('hidden');
            btnText.innerText = 'Start Scrape Now';

            if (data.state === 'failed' || data.last_error) {
              updateConsoleStateUI('failed', `🔴 FAILED: ${data.last_error || 'Scrape encountered an error.'}`);
            } else if (data.state === 'completed' && data.last_summary) {
              const s = data.last_summary;
              const inspected = s.listings_inspected !== undefined ? s.listings_inspected : s.listings_collected;
              updateConsoleStateUI(
                'completed',
                `🟢 COMPLETED: ${s.city} — Inspected ${inspected} listings (${s.no_drone_count} Leads without drone, ${s.drone_detected_count} with drone, ${s.photos_downloaded} photos analyzed)`
              );
            } else {
              updateConsoleStateUI('idle');
            }

            clearInterval(timer);
            isScraperPolling = false;
            loadStats();
            loadListings();
          }
        } catch (e) {
          console.error(e);
        }
      };

      pollFn();
      const timer = setInterval(pollFn, 1000);
    }

    // Cron Jobs Management
    async function loadScheduledJobs() {
      const container = document.getElementById('jobsListContainer');
      try {
        const res = await fetch('/api/jobs');
        const jobs = await res.json();

        if (!jobs || jobs.length === 0) {
          container.innerHTML = `<div class="text-slate-500 text-center py-2">No scheduled jobs created yet.</div>`;
          return;
        }

        container.innerHTML = jobs.map(j => `
          <div class="bg-slate-950 border border-slate-800 rounded-lg p-2.5 flex items-center justify-between">
            <div>
              <div class="font-medium text-white flex items-center space-x-2">
                <span>${escapeHtml(j.name)}</span>
                <span class="px-1.5 py-0.5 rounded text-[10px] ${j.status === 'active' ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20' : 'bg-slate-800 text-slate-400'}">${j.status}</span>
              </div>
              <div class="text-[10px] text-slate-400 mt-0.5">
                📍 ${j.location} • Limit: ${j.limit} • Filter: ${j.drone_filter}
              </div>
              <div class="text-[10px] text-sky-400 mt-0.5 font-mono">
                Trigger: ${j.schedule_type === 'cron' ? j.cron_expression : j.interval_hours + 'h' + j.interval_minutes + 'm'} | Next: ${j.next_run_time || 'Pending'}
              </div>
            </div>
            <div class="flex items-center space-x-1">
              <button onclick="runJobNow('${j.id}')" title="Run Now" class="p-1 rounded bg-slate-800 hover:bg-slate-700 text-emerald-400 text-[10px]">▶</button>
              ${j.status === 'active' ? 
                `<button onclick="pauseJob('${j.id}')" title="Pause" class="p-1 rounded bg-slate-800 hover:bg-slate-700 text-amber-400 text-[10px]">⏸</button>` :
                `<button onclick="resumeJob('${j.id}')" title="Resume" class="p-1 rounded bg-slate-800 hover:bg-slate-700 text-emerald-400 text-[10px]">⏵</button>`
              }
              <button onclick="deleteJob('${j.id}')" title="Delete" class="p-1 rounded bg-slate-800 hover:bg-red-900/50 text-red-400 text-[10px]">🗑</button>
            </div>
          </div>
        `).join('');
      } catch (e) {
        container.innerHTML = `<div class="text-red-400 text-center py-2">Failed to load jobs.</div>`;
      }
    }

    async function createNewScheduleJob() {
      const name = document.getElementById('schedName').value.trim();
      const location = document.getElementById('schedLocation').value.trim();
      const filter = document.getElementById('schedFilter').value;
      const preset = document.getElementById('schedPreset').value;
      const customCron = document.getElementById('customCronExpr').value.trim();

      if (!name) {
        alert('Please give your schedule a name');
        return;
      }

      const body = {
        name,
        location,
        filter,
        limit: 10,
        schedule_type: preset === 'custom' ? 'cron' : (preset.startsWith('every_') ? 'interval' : 'cron'),
        cron_preset: preset !== 'custom' ? preset : null,
        cron_expression: preset === 'custom' ? customCron : '0 9 * * *'
      };

      try {
        const res = await fetch('/api/jobs', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const data = await res.json();
        if (!res.ok) alert(data.detail || 'Error creating job');
        else {
          loadScheduledJobs();
          loadStats();
          alert('Job scheduled successfully!');
        }
      } catch (e) {
        alert('Failed: ' + e.message);
      }
    }

    async function runJobNow(jobId) {
      await fetch(`/api/jobs/${jobId}/run_now`, { method: 'POST' });
      startPollingScrape();
    }

    async function pauseJob(jobId) {
      await fetch(`/api/jobs/${jobId}/pause`, { method: 'POST' });
      loadScheduledJobs();
      loadStats();
    }

    async function resumeJob(jobId) {
      await fetch(`/api/jobs/${jobId}/resume`, { method: 'POST' });
      loadScheduledJobs();
      loadStats();
    }

    async function deleteJob(jobId) {
      if (confirm('Delete this scheduled job?')) {
        await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' });
        loadScheduledJobs();
        loadStats();
      }
    }

    // Modal Inspection
    function viewListingDetail(r) {
      const modal = document.getElementById('detailModal');
      const content = document.getElementById('modalContent');

      content.innerHTML = `
        <div class="border-b border-slate-800 pb-3">
          <div class="text-xs font-semibold text-emerald-400 uppercase tracking-wider">${r.marketing_lead_status || 'Listing Details'}</div>
          <h2 class="text-xl font-bold text-white mt-1">${r.property_address || 'Property Details'}</h2>
          <div class="text-sm text-slate-400">${r.city || ''}, ${r.province || ''} • MLS® ${r.mls_number || 'N/A'}</div>
        </div>

        <div class="grid grid-cols-2 gap-4 text-xs bg-slate-950 p-3.5 rounded-xl border border-slate-800">
          <div>
            <div class="text-slate-400 text-[10px] uppercase">Asking Price</div>
            <div class="text-lg font-extrabold text-emerald-400 mt-0.5">${r.asking_price || 'N/A'}</div>
          </div>
          <div>
            <div class="text-slate-400 text-[10px] uppercase">Drone Photo Status</div>
            <div class="font-semibold text-white mt-0.5">${r.drone_photo_status || 'N/A'}</div>
          </div>
        </div>

        <div class="bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-2">
          <div class="text-xs font-bold text-slate-200 uppercase tracking-wider flex items-center">
            <svg class="w-4 h-4 mr-1.5 text-sky-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>
            Agent & Brokerage Contact Info
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-xs pt-1">
            <div><span class="text-slate-500">Agent Name:</span> <span class="font-semibold text-white">${r.agent_name || 'N/A'}</span></div>
            <div><span class="text-slate-500">Agent Phone:</span> <span class="font-mono text-emerald-400 font-bold">${r.agent_phone || 'N/A'}</span></div>
            <div><span class="text-slate-500">Agent Email:</span> <span class="text-sky-400 font-mono">${r.agent_email || 'Contact form relay'}</span></div>
            <div><span class="text-slate-500">Brokerage:</span> <span class="text-slate-300">${r.brokerage_name || r.agent_or_brokerage || 'N/A'}</span></div>
          </div>
        </div>

        <div class="text-xs space-y-1">
          <div class="font-semibold text-slate-300">Classification Reason:</div>
          <div class="text-slate-400 bg-slate-950 p-3 rounded-lg border border-slate-800 text-[11px]">${r.classification_reason || 'N/A'}</div>
        </div>

        <div class="flex items-center justify-end space-x-3 pt-3 border-t border-slate-800">
          <button onclick="closeModal()" class="px-4 py-1.5 text-xs rounded-lg bg-slate-800 text-slate-300 hover:bg-slate-700">Close</button>
          <a href="${r.canonical_url}" target="_blank" class="px-4 py-1.5 text-xs rounded-lg bg-sky-600 hover:bg-sky-500 text-white font-medium">Open on Realtor.ca</a>
        </div>
      `;

      modal.classList.remove('hidden');
    }

    function closeModal() {
      document.getElementById('detailModal').classList.add('hidden');
    }

    function clearConsole() {
      document.getElementById('consoleLogs').innerHTML = '<div class="text-slate-500 italic">Console cleared.</div>';
      updateConsoleStateUI('idle');
      fetch('/api/scrape/clear_logs', { method: 'POST' });
    }

    function escapeHtml(str) {
      if (!str) return '';
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // Initial Load
    window.addEventListener('DOMContentLoaded', () => {
      loadStats();
      loadListings();
      loadScheduledJobs();
      startPollingScrape();
    });
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(content=DASHBOARD_HTML)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8501))
    print(f"Starting UPCA Real Estate Dashboard at http://127.0.0.1:{port}")
    uvicorn.run("dashboard:app", host="127.0.0.1", port=port, reload=False)
