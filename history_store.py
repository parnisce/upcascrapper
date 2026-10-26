"""
Persistent listing history and contact store for UPCA Real Estate Marketing & Media.
Tracks canonical listing IDs, contact details, property specs, drone photo analysis, and first-seen dates.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ListingHistoryStore:
    """SQLite-backed store tracking listings, agent contacts, property specs, and drone status."""

    def __init__(self, db_path: Path, json_mirror_path: Optional[Path] = None) -> None:
        self.db_path = db_path
        self.json_mirror_path = json_mirror_path or db_path.with_suffix(".json")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS listing_history (
                    listing_id TEXT PRIMARY KEY,
                    canonical_url TEXT NOT NULL,
                    mls_number TEXT,
                    property_address TEXT,
                    city TEXT,
                    province TEXT,
                    asking_price TEXT,
                    property_type TEXT,
                    bedrooms TEXT,
                    bathrooms TEXT,
                    interior_area TEXT,
                    lot_size TEXT,
                    description_text TEXT,
                    agent_name TEXT,
                    agent_phone TEXT,
                    agent_email TEXT,
                    brokerage_name TEXT,
                    brokerage_phone TEXT,
                    agent_or_brokerage TEXT,
                    published_listing_date TEXT,
                    date_first_seen TEXT NOT NULL,
                    last_checked_timestamp TEXT NOT NULL,
                    times_seen INTEGER NOT NULL DEFAULT 1,
                    num_accessible_photos INTEGER DEFAULT 0,
                    drone_photo_status TEXT,
                    detection_confidence TEXT,
                    classification_reason TEXT,
                    marketing_lead_status TEXT,
                    main_photo_url TEXT,
                    local_image_folder TEXT,
                    photo_results_json TEXT
                )
                """
            )
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(listing_history)")
            existing_cols = {row[1] for row in cur.fetchall()}
            for col, col_type in [
                ("agent_name", "TEXT"),
                ("agent_phone", "TEXT"),
                ("agent_email", "TEXT"),
                ("brokerage_name", "TEXT"),
                ("brokerage_phone", "TEXT"),
                ("property_type", "TEXT"),
                ("bedrooms", "TEXT"),
                ("bathrooms", "TEXT"),
                ("interior_area", "TEXT"),
                ("lot_size", "TEXT"),
                ("description_text", "TEXT"),
                ("main_photo_url", "TEXT"),
                ("local_image_folder", "TEXT"),
                ("photo_results_json", "TEXT"),
            ]:
                if col not in existing_cols:
                    conn.execute(f"ALTER TABLE listing_history ADD COLUMN {col} {col_type}")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_canonical_url ON listing_history(canonical_url)"
            )
            conn.commit()

    def lookup(self, listing_id: str, canonical_url: str) -> Optional[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if listing_id:
                cur.execute(
                    "SELECT * FROM listing_history WHERE listing_id = ?",
                    (listing_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)

            cur.execute(
                "SELECT * FROM listing_history WHERE canonical_url = ?",
                (canonical_url,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def _serialize_photo_results(self, record: Dict[str, Any]) -> str:
        raw_results = record.get("photo_inspection_results") or []
        serialized_photos = []
        for p in raw_results:
            if is_dataclass(p):
                serialized_photos.append(asdict(p))
            elif isinstance(p, dict):
                serialized_photos.append(p)
        payload = {
            "candidate_photo_indices": list(record.get("candidate_photo_indices") or []),
            "photo_results": serialized_photos,
        }
        return json.dumps(payload, ensure_ascii=False)

    def upsert_listing(
        self,
        record: Dict[str, Any],
        current_timestamp: str,
    ) -> Tuple[str, bool, int]:
        listing_id = str(record.get("listing_id") or record.get("listing_url") or "").strip()
        canonical_url = str(record.get("listing_url") or record.get("canonical_url") or "").strip()
        photo_results_json = self._serialize_photo_results(record)

        existing = self.lookup(listing_id, canonical_url)
        if existing is None:
            date_first_seen = current_timestamp
            is_new = True
            times_seen = 1
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO listing_history (
                        listing_id, canonical_url, mls_number, property_address,
                        city, province, asking_price, property_type, bedrooms,
                        bathrooms, interior_area, lot_size, description_text,
                        agent_name, agent_phone, agent_email, brokerage_name,
                        brokerage_phone, agent_or_brokerage, published_listing_date,
                        date_first_seen, last_checked_timestamp, times_seen,
                        num_accessible_photos, drone_photo_status, detection_confidence,
                        classification_reason, marketing_lead_status, main_photo_url,
                        local_image_folder, photo_results_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        listing_id,
                        canonical_url,
                        record.get("mls_number", ""),
                        record.get("property_address", ""),
                        record.get("city", ""),
                        record.get("province", ""),
                        record.get("asking_price", ""),
                        record.get("property_type", ""),
                        record.get("bedrooms", ""),
                        record.get("bathrooms", ""),
                        record.get("interior_area", ""),
                        record.get("lot_size", ""),
                        record.get("description_text", ""),
                        record.get("agent_name", ""),
                        record.get("agent_phone", ""),
                        record.get("agent_email", ""),
                        record.get("brokerage_name", ""),
                        record.get("brokerage_phone", ""),
                        record.get("listing_agent_or_brokerage", ""),
                        record.get("listing_date", ""),
                        date_first_seen,
                        current_timestamp,
                        times_seen,
                        int(record.get("num_accessible_photos") or 0),
                        record.get("drone_photo_status", ""),
                        record.get("detection_confidence", ""),
                        record.get("classification_reason", ""),
                        record.get("marketing_lead_status", ""),
                        record.get("main_photo_url", ""),
                        record.get("local_image_folder", ""),
                        photo_results_json,
                    ),
                )
                conn.commit()
        else:
            date_first_seen = str(existing["date_first_seen"])
            is_new = False
            times_seen = int(existing.get("times_seen") or 1) + 1
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE listing_history
                    SET canonical_url = ?,
                        mls_number = ?,
                        property_address = ?,
                        city = ?,
                        province = ?,
                        asking_price = ?,
                        property_type = ?,
                        bedrooms = ?,
                        bathrooms = ?,
                        interior_area = ?,
                        lot_size = ?,
                        description_text = ?,
                        agent_name = ?,
                        agent_phone = ?,
                        agent_email = ?,
                        brokerage_name = ?,
                        brokerage_phone = ?,
                        agent_or_brokerage = ?,
                        published_listing_date = ?,
                        last_checked_timestamp = ?,
                        times_seen = ?,
                        num_accessible_photos = ?,
                        drone_photo_status = ?,
                        detection_confidence = ?,
                        classification_reason = ?,
                        marketing_lead_status = ?,
                        main_photo_url = ?,
                        local_image_folder = ?,
                        photo_results_json = ?
                    WHERE listing_id = ?
                    """,
                    (
                        canonical_url or existing.get("canonical_url", ""),
                        record.get("mls_number") or existing.get("mls_number", ""),
                        record.get("property_address") or existing.get("property_address", ""),
                        record.get("city") or existing.get("city", ""),
                        record.get("province") or existing.get("province", ""),
                        record.get("asking_price") or existing.get("asking_price", ""),
                        record.get("property_type") or existing.get("property_type", ""),
                        record.get("bedrooms") or existing.get("bedrooms", ""),
                        record.get("bathrooms") or existing.get("bathrooms", ""),
                        record.get("interior_area") or existing.get("interior_area", ""),
                        record.get("lot_size") or existing.get("lot_size", ""),
                        record.get("description_text") or existing.get("description_text", ""),
                        record.get("agent_name") or existing.get("agent_name", ""),
                        record.get("agent_phone") or existing.get("agent_phone", ""),
                        record.get("agent_email") or existing.get("agent_email", ""),
                        record.get("brokerage_name") or existing.get("brokerage_name", ""),
                        record.get("brokerage_phone") or existing.get("brokerage_phone", ""),
                        record.get("listing_agent_or_brokerage") or existing.get("agent_or_brokerage", ""),
                        record.get("listing_date") or existing.get("published_listing_date", ""),
                        current_timestamp,
                        times_seen,
                        int(record.get("num_accessible_photos") or existing.get("num_accessible_photos") or 0),
                        record.get("drone_photo_status") or existing.get("drone_photo_status", ""),
                        record.get("detection_confidence") or existing.get("detection_confidence", ""),
                        record.get("classification_reason") or existing.get("classification_reason", ""),
                        record.get("marketing_lead_status") or existing.get("marketing_lead_status", ""),
                        record.get("main_photo_url") or existing.get("main_photo_url", ""),
                        record.get("local_image_folder") or existing.get("local_image_folder", ""),
                        photo_results_json if record.get("photo_inspection_results") else (existing.get("photo_results_json") or ""),
                        existing["listing_id"],
                    ),
                )
                conn.commit()

        self.export_json_mirror()
        return date_first_seen, is_new, times_seen

    def get_all_records(self, drone_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if drone_filter and drone_filter != "all":
                if drone_filter == "no_drone":
                    cur.execute("SELECT * FROM listing_history WHERE drone_photo_status = 'No drone photos detected' ORDER BY date_first_seen DESC")
                elif drone_filter == "has_drone":
                    cur.execute("SELECT * FROM listing_history WHERE drone_photo_status = 'Drone photos detected' ORDER BY date_first_seen DESC")
                elif drone_filter == "uncertain":
                    cur.execute("SELECT * FROM listing_history WHERE drone_photo_status = 'Uncertain, manual review needed' ORDER BY date_first_seen DESC")
                else:
                    cur.execute("SELECT * FROM listing_history ORDER BY date_first_seen DESC")
            else:
                cur.execute("SELECT * FROM listing_history ORDER BY date_first_seen DESC")
            return [dict(r) for r in cur.fetchall()]

    def export_json_mirror(self) -> None:
        records = self.get_all_records()
        self.json_mirror_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
