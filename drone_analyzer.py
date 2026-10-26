"""
Full-gallery drone / aerial photo analysis and manual review workflow module
for UPCA Real Estate Marketing & Media.

Inspects all accessible gallery photos per listing using image dimensions,
EXIF metadata, color/horizon/texture visual features (via Pillow + NumPy),
and listing text signals. Because no deep-learning vision model is installed
locally, ambiguous elevated exterior shots are flagged as:
    "Uncertain, manual review needed"
and an interactive HTML visual review report (`output/drone_review_report.html`)
is generated for rapid human verification.
"""

from __future__ import annotations

import html
import io
import json
import re
import sqlite3
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import ExifTags, Image

LABEL_DRONE_DETECTED = "Drone photos detected"
LABEL_NO_DRONE = "No drone photos detected"
LABEL_UNCERTAIN = "Uncertain, manual review needed"

DRONE_EXIF_KEYWORDS = (
    "dji",
    "mavic",
    "phantom",
    "inspire",
    "autel",
    "skydio",
    "fc3",
    "fc6",
    "fc7",
    "fc8",
    "hasselblad l1d",
    "hasselblad l2d",
)

DRONE_TEXT_PATTERNS = re.compile(
    r"\b(drone|aerial\s+(?:view|photo|photography|shot|views|photos)|"
    r"bird'?s[\s-]*eye|overhead\s+(?:view|photo|shot))\b",
    re.IGNORECASE,
)


@dataclass
class PhotoInspectionResult:
    index: int
    url: str
    local_path: str
    width: int
    height: int
    aspect_ratio: float
    has_drone_exif: bool
    exif_summary: str
    is_exterior_candidate: bool
    aerial_score: float
    category: str  # "strong_aerial", "elevated_or_wide_exterior", "ground_exterior", "interior"
    notes: str


@dataclass
class GalleryAnalysisResult:
    total_accessible_photos: int
    inspected_photos_count: int
    drone_photo_status: str
    detection_confidence: str
    classification_reason: str
    marketing_lead_status: str
    candidate_photo_indices: List[int] = field(default_factory=list)
    photo_results: List[PhotoInspectionResult] = field(default_factory=list)


def _check_exif_for_drone(img: Image.Image) -> Tuple[bool, str]:
    """Check PIL Image EXIF tags for drone camera make/model or GPS altitude."""
    try:
        exif_data = img.getexif()
        if not exif_data:
            return False, ""

        make_model_parts: List[str] = []
        for tag_id, value in exif_data.items():
            tag_name = str(ExifTags.TAGS.get(tag_id, tag_id)).lower()
            if tag_name in ("make", "model", "software", "lensmodel"):
                val_str = str(value).strip()
                if val_str:
                    make_model_parts.append(f"{tag_name}={val_str}")
                    if any(k in val_str.lower() for k in DRONE_EXIF_KEYWORDS):
                        return True, f"EXIF {tag_name}: {val_str}"
        return False, "; ".join(make_model_parts)
    except Exception:
        return False, ""


def analyze_single_photo(
    img_bytes: bytes,
    index: int,
    url: str,
    local_path: str,
) -> Optional[PhotoInspectionResult]:
    """
    Analyze a single gallery image's dimensions, EXIF metadata, sky/horizon layout,
    vegetation/roof texture distribution, and perspective cues.
    """
    try:
        with Image.open(io.BytesIO(img_bytes)) as img:
            width, height = img.size
            if width <= 0 or height <= 0:
                return None
            aspect_ratio = round(width / float(height), 3)
            has_drone_exif, exif_summary = _check_exif_for_drone(img)

            # Downsample to 240x160 RGB for fast, deterministic feature extraction
            rgb = img.convert("RGB").resize((240, 160), Image.Resampling.BILINEAR)
            arr = np.asarray(rgb, dtype=np.float32) / 255.0
    except Exception:
        return None

    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]

    # Split vertically into top (0-30%), upper-mid (30-55%), and lower (30-100%)
    h = arr.shape[0]
    top_end = int(h * 0.30)
    mid_end = int(h * 0.55)

    # 1. Sky / bright horizon detection in top 30% vs middle
    # Sky pixels: blue-dominant (b > r + 0.05 and b > 0.42) or bright uniform cloud/overcast
    sky_mask_top = ((b[:top_end, :] > r[:top_end, :] + 0.04) & (b[:top_end, :] > 0.42)) | (
        (r[:top_end, :] > 0.78)
        & (g[:top_end, :] > 0.78)
        & (b[:top_end, :] > 0.80)
        & (np.abs(r[:top_end, :] - b[:top_end, :]) < 0.07)
    )
    sky_Mask_mid = (b[top_end:mid_end, :] > r[top_end:mid_end, :] + 0.05) & (
        b[top_end:mid_end, :] > 0.45
    )
    top_sky_ratio = float(np.mean(sky_mask_top))
    mid_sky_ratio = float(np.mean(sky_Mask_mid))

    # 2. Warm indoor lighting check (interiors strongly skew R > G > B with warm walls/wood floors)
    warm_indoor_mask = (r > b + 0.08) & (r > g + 0.02) & (b < 0.65)
    warm_indoor_ratio = float(np.mean(warm_indoor_mask))

    # 3. Outdoor foliage / grass / trees / earth / roof shingle mask in lower 70%
    r_low, g_low, b_low = r[top_end:, :], g[top_end:, :], b[top_end:, :]
    green_foliage_low = (g_low > r_low + 0.02) & (g_low > b_low + 0.03) & (g_low > 0.18)
    foliage_low_ratio = float(np.mean(green_foliage_low))

    # Also check foliage in top 30% (occurs in top-down / high-angle oblique drone shots)
    green_foliage_top = (
        (g[:top_end, :] > r[:top_end, :] + 0.02)
        & (g[:top_end, :] > b[:top_end, :] + 0.03)
        & (g[:top_end, :] > 0.18)
    )
    foliage_top_ratio = float(np.mean(green_foliage_top))

    # 4. Spatial edge density (gray luminance gradients) in lower 70% and top 30%
    gray = 0.299 * r + 0.587 * g + 0.114 * b
    gy, gx = np.gradient(gray)
    grad_mag = np.hypot(gx, gy)
    edge_density_low = float(np.mean(grad_mag[top_end:, :] > 0.06))
    edge_density_top = float(np.mean(grad_mag[:top_end, :] > 0.06))

    # Ratio of vertical vs horizontal edges (ground-level rooms & close facades have strong vertical lines)
    vert_edge_strength = float(np.mean(np.abs(gx[top_end:, :])))
    horiz_edge_strength = float(np.mean(np.abs(gy[top_end:, :])))
    vh_ratio = vert_edge_strength / max(horiz_edge_strength, 1e-4)

    # 5. High-saturation lot-boundary overlay lines (yellow/red/cyan polygon lines often drawn on drone photos)
    bright_yellow_red_overlay = (
        ((r > 0.85) & (g > 0.75) & (b < 0.20))  # bright yellow boundary line
        | ((r > 0.85) & (g < 0.20) & (b < 0.20))  # bright red boundary line
    )
    overlay_ratio = float(np.mean(bright_yellow_red_overlay))

    # Determine if exterior vs interior
    is_exterior = (
        (top_sky_ratio >= 0.14 and warm_indoor_ratio < 0.50)
        or (foliage_low_ratio >= 0.12)
        or (foliage_top_ratio >= 0.10 and warm_indoor_ratio < 0.40)
    )

    aerial_score = 0.0
    notes_list: List[str] = []

    if has_drone_exif:
        aerial_score = 0.95
        notes_list.append(exif_summary)

    if is_exterior:
        # Pattern A1: High-altitude oblique neighborhood aerial shot
        # Sky restricted to top strip (0.20..0.55), little sky in mid-frame (<0.18),
        # and high horizon/roof/tree edge density in BOTH upper-horizon and lower-frame.
        if (
            0.20 <= top_sky_ratio <= 0.55
            and mid_sky_ratio <= 0.18
            and edge_density_top >= 0.32
            and edge_density_low >= 0.38
            and vh_ratio <= 0.95
        ):
            aerial_score += 0.78
            notes_list.append(
                f"high-altitude oblique neighborhood horizon "
                f"(top_sky={top_sky_ratio:.2f}, top_edges={edge_density_top:.2f}, low_edges={edge_density_low:.2f})"
            )
        # Pattern A2: Moderate high-horizon wide landscape profile
        elif 0.08 <= top_sky_ratio <= 0.44 and mid_sky_ratio < 0.18:
            if edge_density_low >= 0.28 and foliage_low_ratio >= 0.12 and vh_ratio < 1.15:
                aerial_score += 0.48
                notes_list.append(
                    f"high-horizon wide landscape profile (top_sky={top_sky_ratio:.2f}, "
                    f"low_edges={edge_density_low:.2f}, foliage={foliage_low_ratio:.2f})"
                )
            elif edge_density_low >= 0.22 and foliage_low_ratio >= 0.08:
                aerial_score += 0.30
                notes_list.append(
                    f"elevated/wide exterior perspective (top_sky={top_sky_ratio:.2f}, "
                    f"low_edges={edge_density_low:.2f})"
                )

        # Pattern B: Top-down / nadir or steep overhead view (minimal sky, outdoor foliage & roof texture filling top & bottom)
        if (
            top_sky_ratio < 0.12
            and foliage_top_ratio >= 0.08
            and foliage_low_ratio >= 0.12
            and edge_density_top >= 0.28
            and edge_density_low >= 0.28
        ):
            aerial_score += 0.76
            notes_list.append(
                f"top-down/overhead roof & lot geometry across full frame "
                f"(top_foliage={foliage_top_ratio:.2f}, low_foliage={foliage_low_ratio:.2f}, "
                f"top_edges={edge_density_top:.2f}, low_edges={edge_density_low:.2f})"
            )

        # Pattern C: Wide exterior with substantial lot/foliage and multi-structure complexity
        if foliage_low_ratio >= 0.22 and edge_density_low >= 0.24:
            aerial_score += 0.22
            notes_list.append(f"wide lot/greenery coverage ({foliage_low_ratio:.2f})")

        # Pattern D: Lot boundary graphic overlay on an exterior shot
        if 0.0015 <= overlay_ratio <= 0.035 and edge_density_low >= 0.16:
            aerial_score += 0.25
            notes_list.append("possible lot-boundary graphic overlay")

        # Penalize standard eye-level street facade shots where sky floods into the mid-frame,
        # top 30% is mostly empty sky with low edge density, or strong vertical architectural lines dominate
        if mid_sky_ratio >= 0.26 or vh_ratio >= 1.20 or (top_sky_ratio >= 0.55 and edge_density_top < 0.22):
            aerial_score = max(0.0, aerial_score - 0.28)
            notes_list.append("eye-level facade/sky geometry")

    aerial_score = round(min(1.0, max(0.0, aerial_score)), 3)

    if aerial_score >= 0.72:
        category = "strong_aerial"
    elif aerial_score >= 0.45:
        category = "elevated_or_wide_exterior"
    elif is_exterior:
        category = "ground_exterior"
    else:
        category = "interior"

    return PhotoInspectionResult(
        index=index,
        url=url,
        local_path=local_path,
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        has_drone_exif=has_drone_exif,
        exif_summary=exif_summary,
        is_exterior_candidate=is_exterior,
        aerial_score=aerial_score,
        category=category,
        notes="; ".join(notes_list) if notes_list else category,
    )


def classify_listing_gallery(
    photo_results: List[PhotoInspectionResult],
    total_accessible_photos: int,
    description_text: str,
    is_new_listing: bool,
) -> GalleryAnalysisResult:
    """
    Classify the full photo gallery of a listing into one of:
      - Drone photos detected
      - No drone photos detected
      - Uncertain, manual review needed

    Never marks a listing as missing drone photos merely because photo #1 is a
    ground-level shot; evaluates all inspected gallery photos.
    """
    inspected_count = len(photo_results)
    text_match = DRONE_TEXT_PATTERNS.search(description_text or "")
    matched_phrase = text_match.group(0) if text_match else ""

    exif_hits = [p for p in photo_results if p.has_drone_exif]
    strong_aerial = [p for p in photo_results if p.category == "strong_aerial"]
    elevated_candidates = [
        p for p in photo_results if p.category in ("strong_aerial", "elevated_or_wide_exterior")
    ]
    exterior_photos = [p for p in photo_results if p.is_exterior_candidate]

    lead_age_tag = "New Listing" if is_new_listing else "Previously Seen"

    # Case 1: EXIF drone tag or explicit aerial text backed by elevated/aerial exterior photo(s),
    # or multiple strong aerial visual matches across the gallery.
    if exif_hits:
        status = LABEL_DRONE_DETECTED
        confidence = "High (Drone camera EXIF metadata detected)"
        hit_indices = ", ".join(f"#{p.index}" for p in exif_hits)
        reason = (
            f"Inspected {inspected_count}/{total_accessible_photos} gallery photos; "
            f"drone EXIF metadata found on photo(s) {hit_indices} ({exif_hits[0].exif_summary})."
        )
        lead_status = f"Not a Drone Lead - Drone Photos Present ({lead_age_tag})"
        candidate_indices = [p.index for p in exif_hits]

    elif matched_phrase and elevated_candidates:
        status = LABEL_DRONE_DETECTED
        confidence = "Medium-High (Explicit aerial/drone text + elevated exterior photos; verify in HTML report)"
        hit_indices = ", ".join(f"#{p.index}" for p in elevated_candidates[:5])
        reason = (
            f"Inspected all {inspected_count}/{total_accessible_photos} gallery photos; "
            f"listing description mentions '{matched_phrase}' and photo(s) {hit_indices} "
            f"show elevated/wide exterior views."
        )
        lead_status = f"Not a Drone Lead - Drone Photos Likely Present ({lead_age_tag})"
        candidate_indices = [p.index for p in elevated_candidates]

    elif len(strong_aerial) >= 2:
        status = LABEL_DRONE_DETECTED
        confidence = "Medium (Multiple high-angle/aerial heuristic matches; no ML vision model installed - verify in HTML report)"
        hit_indices = ", ".join(f"#{p.index} (score {p.aerial_score:.2f})" for p in strong_aerial[:4])
        reason = (
            f"Inspected all {inspected_count}/{total_accessible_photos} gallery photos; "
            f"{len(strong_aerial)} photos ({hit_indices}) exhibit high-horizon/overhead landscape features."
        )
        lead_status = f"Low Priority - Drone Photos Likely Present ({lead_age_tag})"
        candidate_indices = [p.index for p in strong_aerial]

    # Case 2: No strong aerial photos and no ambiguous elevated exterior photos across the ENTIRE gallery
    elif len(elevated_candidates) == 0 and not matched_phrase:
        status = LABEL_NO_DRONE
        confidence = (
            f"Medium-High (All {inspected_count} gallery photos inspected; "
            "0 aerial/elevated candidates found - confirm in HTML Review Report)"
        )
        reason = (
            f"Inspected all {inspected_count}/{total_accessible_photos} accessible gallery photos "
            f"({len(exterior_photos)} standard ground-level exterior, "
            f"{inspected_count - len(exterior_photos)} interior); no overhead, roof, or aerial lot views detected."
        )
        lead_status = (
            "High Priority Lead - New Listing Without Drone Photos"
            if is_new_listing
            else "Qualified Lead - No Drone Photos Detected (Previously Seen)"
        )
        candidate_indices = []

    # Case 3: Ambiguous elevated/wide exterior shots exist (or text mentions aerial without clear visual match)
    # Since no vision model is installed, flag honestly for quick manual review rather than guessing!
    else:
        status = LABEL_UNCERTAIN
        confidence = (
            "Manual Review Needed (Heuristic feature check only, no ML vision model installed - "
            "use HTML Review Report)"
        )
        cand_str = ", ".join(f"#{p.index} ({p.aerial_score:.2f})" for p in elevated_candidates[:5])
        reason_parts = [
            f"Inspected all {inspected_count}/{total_accessible_photos} accessible gallery photos "
            f"({len(exterior_photos)} exterior, {inspected_count - len(exterior_photos)} interior)."
        ]
        if elevated_candidates:
            reason_parts.append(
                f"Found {len(elevated_candidates)} wide/elevated exterior photo(s) [{cand_str}] "
                "that could be either elevated ground/2nd-storey shots or low-altitude drone shots."
            )
        if matched_phrase:
            reason_parts.append(f"Description mentions '{matched_phrase}'.")
        reason_parts.append("Visual confirmation required in output/drone_review_report.html.")
        reason = " ".join(reason_parts)
        lead_status = (
            "Potential Lead - Manual Review Needed (New Listing)"
            if is_new_listing
            else "Potential Lead - Manual Review Needed (Previously Seen)"
        )
        candidate_indices = [p.index for p in elevated_candidates]

    return GalleryAnalysisResult(
        total_accessible_photos=total_accessible_photos,
        inspected_photos_count=inspected_count,
        drone_photo_status=status,
        detection_confidence=confidence,
        classification_reason=reason,
        marketing_lead_status=lead_status,
        candidate_photo_indices=candidate_indices,
        photo_results=photo_results,
    )


def _extract_base_city(city_raw: str) -> str:
    if not city_raw:
        return "Unknown"
    base = city_raw.split("(")[0].strip()
    return base.title() if base else "Unknown"


def _find_listing_image_folder(images_dir: Path, listing_id: str, mls_number: str) -> Optional[Path]:
    if not images_dir.exists():
        return None
    if listing_id:
        direct = images_dir / f"listing_{listing_id}"
        if direct.exists() and direct.is_dir() and any(direct.glob("photo_*.*")):
            return direct
    if mls_number:
        mls_lower = mls_number.strip().lower()
        for folder in sorted(images_dir.glob("listing_*")):
            if folder.is_dir():
                for img in folder.glob("photo_*.*"):
                    if mls_lower in img.name.lower():
                        return folder
    return None


def resolve_and_load_all_db_listings(
    db_path: Path,
    images_dir: Path,
) -> List[Dict[str, Any]]:
    """
    Loads ALL listings stored in SQLite (`listing_history.db`), enriches any missing
    agent contact fields or photo inspection results from `output/images/`, and caches
    `photo_results_json` in SQLite for fast subsequent loads.
    """
    if not db_path.exists():
        return []

    listings: List[Dict[str, Any]] = []
    updates_to_cache: List[Tuple[str, str, str]] = []

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(listing_history)")
        existing_cols = {row[1] for row in cur.fetchall()}
        for col in [
            "property_type",
            "bedrooms",
            "bathrooms",
            "interior_area",
            "lot_size",
            "description_text",
            "main_photo_url",
            "local_image_folder",
            "photo_results_json",
        ]:
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE listing_history ADD COLUMN {col} TEXT")
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM listing_history ORDER BY date_first_seen DESC, listing_id DESC"
        ).fetchall()

    for r in rows:
        item = dict(r)
        listing_id = str(item.get("listing_id") or "").strip()
        mls_number = str(item.get("mls_number") or "").strip()
        item["listing_url"] = item.get("canonical_url") or item.get("listing_url") or ""
        item["listing_date"] = item.get("published_listing_date") or item.get("listing_date") or ""

        # Ensure agent_name / brokerage_name fallback from agent_or_brokerage
        if not item.get("agent_name") and item.get("agent_or_brokerage"):
            aob = str(item["agent_or_brokerage"])
            m = re.match(r"^(.*?)\s*\((.*?)\)$", aob)
            if m:
                item["agent_name"] = m.group(1).strip()
                item["brokerage_name"] = item.get("brokerage_name") or m.group(2).strip()
            else:
                item["agent_name"] = aob.strip()

        item["listing_agent_or_brokerage"] = item.get("agent_or_brokerage") or (
            f"{item.get('agent_name', '')} ({item.get('brokerage_name', '')})".strip(" ()")
        )
        item["base_city"] = _extract_base_city(str(item.get("city") or ""))

        times_seen = int(item.get("times_seen") or 1)
        item["is_new_listing"] = times_seen <= 1

        # Load or reconstruct photo inspection results
        photo_results: List[PhotoInspectionResult] = []
        candidate_indices: List[int] = []
        raw_json = item.get("photo_results_json")

        if raw_json:
            try:
                parsed = json.loads(raw_json)
                candidate_indices = [int(x) for x in (parsed.get("candidate_photo_indices") or [])]
                for p_dict in parsed.get("photo_results") or []:
                    photo_results.append(PhotoInspectionResult(**p_dict))
            except Exception:
                photo_results = []

        if not photo_results:
            folder = _find_listing_image_folder(images_dir, listing_id, mls_number)
            if folder is not None:
                img_files = sorted(folder.glob("photo_*.*"))
                for idx, img_file in enumerate(img_files, start=1):
                    try:
                        img_bytes = img_file.read_bytes()
                        rel_path = f"images/{folder.name}/{img_file.name}"
                        cdn_url = f"/images/{folder.name}/{img_file.name}"
                        res = analyze_single_photo(
                            img_bytes=img_bytes,
                            index=idx,
                            url=cdn_url,
                            local_path=rel_path,
                        )
                        if res is not None:
                            photo_results.append(res)
                    except Exception:
                        continue

                if photo_results:
                    gal = classify_listing_gallery(
                        photo_results=photo_results,
                        total_accessible_photos=int(item.get("num_accessible_photos") or len(photo_results)),
                        description_text=str(item.get("description_text") or ""),
                        is_new_listing=item["is_new_listing"],
                    )
                    candidate_indices = gal.candidate_photo_indices
                    payload_str = json.dumps(
                        {
                            "candidate_photo_indices": candidate_indices,
                            "photo_results": [asdict(p) for p in photo_results],
                        },
                        ensure_ascii=False,
                    )
                    updates_to_cache.append((f"output/images/{folder.name}", payload_str, listing_id))

        item["photo_inspection_results"] = photo_results
        item["candidate_photo_indices"] = candidate_indices
        listings.append(item)

    if updates_to_cache:
        try:
            with sqlite3.connect(db_path) as conn:
                for rel_folder, payload_str, lid in updates_to_cache:
                    conn.execute(
                        "UPDATE listing_history SET local_image_folder = ?, photo_results_json = ? WHERE listing_id = ?",
                        (rel_folder, payload_str, lid),
                    )
                conn.commit()
        except Exception:
            pass

    return listings


def generate_html_review_report(
    listings: List[Dict[str, Any]],
    report_path: Path,
    location_label: str = "All Locations",
) -> None:
    """
    Generate an interactive, modern Visual Review & Lead Intelligence Dashboard
    (`output/drone_review_report.html`) for UPCA Real Estate Marketing & Media.
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Also merge with all listings from output/listing_history.db if present so the visual report
    # always displays ALL scraped listings across all cities!
    db_path = report_path.parent / "listing_history.db"
    images_dir = report_path.parent / "images"
    if db_path.exists():
        db_listings = resolve_and_load_all_db_listings(db_path, images_dir)
        if db_listings:
            # Merge live in-memory photo results if newer
            live_by_id = {str(x.get("listing_id") or ""): x for x in listings if x.get("listing_id")}
            merged: List[Dict[str, Any]] = []
            for db_item in db_listings:
                lid = str(db_item.get("listing_id") or "")
                if lid in live_by_id:
                    live_item = live_by_id[lid]
                    for k, v in live_item.items():
                        if v not in (None, "", []):
                            db_item[k] = v
                    db_item["base_city"] = _extract_base_city(str(db_item.get("city") or ""))
                merged.append(db_item)
            listings = merged

    total = len(listings)
    no_drone_count = sum(1 for x in listings if x.get("drone_photo_status") == LABEL_NO_DRONE)
    uncertain_count = sum(1 for x in listings if x.get("drone_photo_status") == LABEL_UNCERTAIN)
    drone_count = sum(1 for x in listings if x.get("drone_photo_status") == LABEL_DRONE_DETECTED)
    new_count = sum(1 for x in listings if x.get("is_new_listing"))
    total_photos_count = sum(len(x.get("photo_inspection_results") or []) for x in listings)

    # Collect unique base cities and their counts
    city_counts: Dict[str, int] = {}
    for x in listings:
        bcity = x.get("base_city") or _extract_base_city(str(x.get("city") or ""))
        x["base_city"] = bcity
        city_counts[bcity] = city_counts.get(bcity, 0) + 1

    sorted_cities = sorted(city_counts.items(), key=lambda kv: (-kv[1], kv[0]))

    # Build Location Filter Pills & Options
    loc_pills_html = [
        f'<button onclick="setLocationFilter(\'all\', this)" data-loc-btn="all" '
        f'class="loc-pill active px-3.5 py-1.5 rounded-lg text-xs font-semibold transition flex items-center gap-1.5 bg-sky-600 text-white shadow-sm">'
        f'<span>🌐 All Locations</span><span class="px-1.5 py-0.2 rounded-full bg-black/25 text-[11px]">{total}</span></button>'
    ]
    loc_options_html = [f'<option value="all">🌐 All Locations ({total})</option>']
    for city_name, c_count in sorted_cities:
        esc_city = html.escape(city_name)
        loc_pills_html.append(
            f'<button onclick="setLocationFilter(\'{esc_city}\', this)" data-loc-btn="{esc_city}" '
            f'class="loc-pill px-3.5 py-1.5 rounded-lg text-xs font-medium transition flex items-center gap-1.5 bg-slate-800/90 text-slate-300 hover:bg-slate-700 hover:text-white border border-slate-700">'
            f'<span>📍 {esc_city}</span><span class="px-1.5 py-0.2 rounded-full bg-slate-900 text-slate-400 text-[11px]">{c_count}</span></button>'
        )
        loc_options_html.append(f'<option value="{esc_city}">📍 {esc_city} ({c_count})</option>')

    cards_html: List[str] = []
    lightbox_galleries: Dict[str, List[Dict[str, Any]]] = {}

    for card_idx, item in enumerate(listings, start=1):
        lid = str(item.get("listing_id") or f"idx_{card_idx}")
        status = item.get("drone_photo_status", LABEL_UNCERTAIN)
        if status == LABEL_NO_DRONE:
            filter_tag = "nodrone"
            status_badge = (
                '<span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold '
                'bg-emerald-500/15 text-emerald-300 border border-emerald-500/40 shadow-sm">'
                '⚡ No Drone Photos Detected (Lead)</span>'
            )
            card_border_cls = "border-emerald-500/30 hover:border-emerald-500/60"
        elif status == LABEL_DRONE_DETECTED:
            filter_tag = "drone"
            status_badge = (
                '<span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold '
                'bg-sky-500/15 text-sky-300 border border-sky-500/40">'
                '🚁 Drone Photos Detected</span>'
            )
            card_border_cls = "border-slate-800 hover:border-sky-500/40"
        else:
            filter_tag = "uncertain"
            status_badge = (
                '<span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-bold '
                'bg-amber-500/15 text-amber-300 border border-amber-500/40">'
                '❓ Uncertain — Manual Review Needed</span>'
            )
            card_border_cls = "border-amber-500/30 hover:border-amber-500/60"

        is_new = bool(item.get("is_new_listing"))
        times_seen = int(item.get("times_seen") or 1)
        new_badge = (
            '<span class="inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/40">✨ NEW LISTING</span>'
            if is_new
            else f'<span class="inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-medium bg-slate-800 text-slate-400 border border-slate-700">👁️ Seen {times_seen}x</span>'
        )

        base_city = item.get("base_city") or _extract_base_city(str(item.get("city") or ""))
        address = str(item.get("property_address") or "Address Unavailable")
        full_city = str(item.get("city") or "")
        province = str(item.get("province") or "Ontario")
        price = str(item.get("asking_price") or "Price N/A")
        mls_num = str(item.get("mls_number") or "N/A")
        agent_name = str(item.get("agent_name") or item.get("listing_agent_or_brokerage") or "Agent Not Listed")
        agent_phone = str(item.get("agent_phone") or "").strip()
        agent_email = str(item.get("agent_email") or "").strip()
        brokerage_name = str(item.get("brokerage_name") or item.get("agent_or_brokerage") or "Brokerage N/A")
        brokerage_phone = str(item.get("brokerage_phone") or "").strip()
        prop_type = str(item.get("property_type") or "").strip()
        bedrooms = str(item.get("bedrooms") or "").strip()
        bathrooms = str(item.get("bathrooms") or "").strip()
        interior_area = str(item.get("interior_area") or "").strip()
        lot_size = str(item.get("lot_size") or "").strip()
        description_text = str(item.get("description_text") or "").strip()
        listing_url = str(item.get("listing_url") or item.get("canonical_url") or "#")
        date_first_seen = str(item.get("date_first_seen") or "").split("T")[0] or "N/A"
        pub_date = str(item.get("listing_date") or item.get("published_listing_date") or "N/A")

        photo_results: List[PhotoInspectionResult] = item.get("photo_inspection_results") or []
        candidate_set = set(item.get("candidate_photo_indices") or [])

        ordered_photos = sorted(
            photo_results,
            key=lambda p: (0 if p.index in candidate_set else (1 if p.is_exterior_candidate else 2), p.index),
        )

        gallery_js_items: List[Dict[str, Any]] = []
        thumbs_html: List[str] = []

        for p_pos, p in enumerate(ordered_photos):
            rel_src = p.url
            if p.local_path:
                norm_path = str(p.local_path).replace("\\", "/")
                if "images/" in norm_path:
                    rel_src = "/" + norm_path[norm_path.find("images/") :]
                else:
                    rel_src = p.url
            elif str(p.url).startswith("images/"):
                rel_src = "/" + str(p.url)

            fallback_url = p.url if str(p.url).startswith("http") else rel_src

            if p.index in candidate_set:
                cat_type = "candidate"
                border_cls = "border-amber-400 ring-2 ring-amber-400/30 bg-amber-950/30"
                pill_cls = "bg-amber-500 text-slate-950 font-extrabold"
                tag_label = f"🚁 CANDIDATE ({p.aerial_score:.2f})"
            elif p.is_exterior_candidate:
                cat_type = "exterior"
                border_cls = "border-sky-500/50 bg-slate-900"
                pill_cls = "bg-sky-500/20 text-sky-300 border border-sky-500/40 font-semibold"
                tag_label = f"🏡 Exterior ({p.aerial_score:.2f})"
            else:
                cat_type = "interior"
                border_cls = "border-slate-800 bg-slate-900/70 opacity-85 hover:opacity-100"
                pill_cls = "bg-slate-800 text-slate-400 font-medium"
                tag_label = "🛋️ Interior"

            gallery_js_items.append(
                {
                    "src": rel_src,
                    "fallback": fallback_url,
                    "index": p.index,
                    "tag": tag_label,
                    "score": f"{p.aerial_score:.2f}",
                    "dims": f"{p.width}×{p.height}",
                    "notes": p.notes,
                    "address": address,
                    "city": full_city,
                    "price": price,
                    "agent": f"{agent_name} {('(' + agent_phone + ')') if agent_phone else ''}",
                    "listing_url": listing_url,
                }
            )

            thumbs_html.append(
                f'<div onclick="openLightbox(\'{html.escape(lid)}\', {p_pos})" '
                f'data-photo-cat="{cat_type}" '
                f'class="photo-thumb group cursor-pointer flex-shrink-0 w-52 rounded-xl overflow-hidden border-2 {border_cls} transition-all duration-200 hover:-translate-y-1 hover:shadow-xl">'
                f'  <div class="relative h-36 w-full bg-slate-950 overflow-hidden">'
                f'    <img src="{html.escape(rel_src)}" loading="lazy" alt="Photo #{p.index}" '
                f'         onerror="this.onerror=null; this.src=\'{html.escape(fallback_url)}\';" '
                f'         class="w-full h-full object-cover group-hover:scale-105 transition duration-300"/>'
                f'    <span class="absolute top-2 left-2 px-2 py-0.5 rounded bg-black/75 backdrop-blur-sm text-[11px] font-mono font-bold text-white">#{p.index}</span>'
                f'    <span class="absolute top-2 right-2 px-2 py-0.5 rounded text-[10px] {pill_cls}">{html.escape(tag_label)}</span>'
                f'    <span class="absolute bottom-1.5 right-2 px-1.5 py-0.5 rounded bg-black/70 text-[10px] font-mono text-slate-300">{p.width}×{p.height}</span>'
                f'  </div>'
                f'  <div class="p-2 text-[11px] text-slate-400 truncate" title="{html.escape(p.notes)}">'
                f'    {html.escape(p.notes)}'
                f'  </div>'
                f'</div>'
            )

        lightbox_galleries[lid] = gallery_js_items

        # Agent phone HTML
        if agent_phone and agent_phone.lower() != "none":
            phone_html = (
                f'<a href="tel:{html.escape(agent_phone)}" class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg '
                f'bg-emerald-500/15 hover:bg-emerald-500/25 text-emerald-300 border border-emerald-500/30 font-mono text-xs font-bold transition">'
                f'📞 {html.escape(agent_phone)}</a>'
            )
        else:
            phone_html = '<span class="text-slate-500 text-xs italic">Phone not listed</span>'

        # Email / Contact route HTML
        if agent_email and "@" in agent_email:
            email_html = f'<a href="mailto:{html.escape(agent_email)}" class="text-sky-400 hover:underline text-xs font-mono">{html.escape(agent_email)}</a>'
        else:
            email_html = (
                f'<a href="{html.escape(listing_url)}" target="_blank" class="text-sky-400 hover:text-sky-300 text-xs underline">'
                f'✉️ Realtor.ca Agent Message Form ↗</a>'
            )

        # Property specs pills
        spec_pills: List[str] = []
        if prop_type:
            spec_pills.append(f'<span class="px-2.5 py-1 rounded-md bg-slate-800/90 text-slate-200 text-xs">🏠 {html.escape(prop_type)}</span>')
        if bedrooms:
            spec_pills.append(f'<span class="px-2.5 py-1 rounded-md bg-slate-800/90 text-slate-200 text-xs">🛏️ {html.escape(bedrooms)} Beds</span>')
        if bathrooms:
            spec_pills.append(f'<span class="px-2.5 py-1 rounded-md bg-slate-800/90 text-slate-200 text-xs">🛁 {html.escape(bathrooms)} Baths</span>')
        if interior_area:
            spec_pills.append(f'<span class="px-2.5 py-1 rounded-md bg-slate-800/90 text-slate-200 text-xs">📐 {html.escape(interior_area)}</span>')
        if lot_size:
            spec_pills.append(f'<span class="px-2.5 py-1 rounded-md bg-slate-800/90 text-slate-200 text-xs">🌳 Lot: {html.escape(lot_size)}</span>')

        specs_row_html = (
            f'<div class="flex flex-wrap gap-2 mt-2">{"".join(spec_pills)}</div>'
            if spec_pills
            else ""
        )

        desc_html = (
            f'<details class="mt-3 text-xs text-slate-400 bg-slate-950/60 rounded-lg px-3.5 py-2 border border-slate-800/80">'
            f'<summary class="cursor-pointer font-semibold text-slate-300 hover:text-white select-none">📝 View Property Description &amp; Remarks</summary>'
            f'<p class="mt-2 leading-relaxed text-slate-300">{html.escape(description_text)}</p>'
            f'</details>'
            if description_text
            else ""
        )

        searchable_blob = f"{address} {full_city} {base_city} {mls_num} {agent_name} {agent_phone} {brokerage_name} {price}".lower()

        cards_html.append(
            f"""
            <article class="listing-card bg-slate-900/95 border {card_border_cls} rounded-2xl p-5 sm:p-6 shadow-lg transition-all"
                     data-status="{filter_tag}"
                     data-new="{'true' if is_new else 'false'}"
                     data-city="{html.escape(base_city)}"
                     data-search="{html.escape(searchable_blob)}">

              <!-- Card Header -->
              <div class="flex flex-col lg:flex-row lg:items-start justify-between gap-4 pb-4 border-b border-slate-800">
                <div class="space-y-1.5">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="px-2.5 py-0.5 rounded-md bg-sky-500/10 text-sky-400 border border-sky-500/20 text-xs font-semibold">📍 {html.escape(base_city)}</span>
                    {new_badge}
                    {status_badge}
                  </div>
                  <h2 class="text-xl sm:text-2xl font-extrabold text-white tracking-tight pt-1">
                    {html.escape(address)}
                    <span class="text-slate-400 font-normal text-base sm:text-lg">— {html.escape(full_city)}, {html.escape(province)}</span>
                  </h2>
                  {specs_row_html}
                </div>

                <div class="flex flex-row lg:flex-col items-center lg:items-end justify-between gap-2 shrink-0">
                  <div class="text-2xl sm:text-3xl font-black text-emerald-400 tracking-tight">{html.escape(price)}</div>
                  <div class="flex items-center gap-2">
                    <span class="text-xs font-mono text-slate-400 bg-slate-950 px-2.5 py-1 rounded border border-slate-800">MLS® {html.escape(mls_num)}</span>
                    <a href="{html.escape(listing_url)}" target="_blank"
                       class="inline-flex items-center gap-1 px-3 py-1 rounded-lg bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold transition shadow-sm">
                      Open REALTOR.ca ↗
                    </a>
                  </div>
                </div>
              </div>

              <!-- 3-Column Info Grid: Agent Contact | Property Timeline | AI/Heuristic Classification -->
              <div class="grid grid-cols-1 md:grid-cols-3 gap-4 my-4">

                <!-- Box 1: Agent & Brokerage Contact -->
                <div class="bg-slate-950/80 border border-slate-800/90 rounded-xl p-3.5 space-y-2">
                  <div class="text-[11px] font-bold uppercase tracking-wider text-sky-400 flex items-center gap-1.5">
                    👤 Agent &amp; Brokerage Contact
                  </div>
                  <div class="text-sm font-bold text-white">{html.escape(agent_name)}</div>
                  <div class="flex flex-wrap items-center gap-2">
                    {phone_html}
                    {email_html}
                  </div>
                  <div class="text-xs text-slate-400 pt-1 border-t border-slate-800/80">
                    🏢 <strong class="text-slate-300">{html.escape(brokerage_name)}</strong>
                    {f' • <span class="font-mono text-slate-300">{html.escape(brokerage_phone)}</span>' if brokerage_phone else ''}
                  </div>
                </div>

                <!-- Box 2: Listing Metadata & Photo Count -->
                <div class="bg-slate-950/80 border border-slate-800/90 rounded-xl p-3.5 space-y-1.5 text-xs">
                  <div class="text-[11px] font-bold uppercase tracking-wider text-purple-400">
                    📅 Listing Timeline &amp; Media
                  </div>
                  <div class="flex justify-between py-0.5">
                    <span class="text-slate-400">Date First Seen:</span>
                    <span class="font-mono text-slate-200 font-semibold">{html.escape(date_first_seen)}</span>
                  </div>
                  <div class="flex justify-between py-0.5">
                    <span class="text-slate-400">Time on REALTOR.ca:</span>
                    <span class="font-semibold text-amber-300">{html.escape(pub_date)}</span>
                  </div>
                  <div class="flex justify-between py-0.5">
                    <span class="text-slate-400">Gallery Photos Inspected:</span>
                    <span class="font-mono font-bold text-emerald-400">{len(ordered_photos)} / {item.get("num_accessible_photos", len(ordered_photos))} photos</span>
                  </div>
                  <div class="flex justify-between py-0.5">
                    <span class="text-slate-400">Aerial / Elevated Candidates:</span>
                    <span class="font-mono font-bold text-amber-400">{len(candidate_set)} flagged</span>
                  </div>
                </div>

                <!-- Box 3: Drone Lead Analysis -->
                <div class="bg-slate-950/80 border border-slate-800/90 rounded-xl p-3.5 space-y-1.5 text-xs">
                  <div class="text-[11px] font-bold uppercase tracking-wider text-emerald-400">
                    🎯 UPCA Drone Lead Qualification
                  </div>
                  <div>
                    <span class="text-slate-400">Lead Priority:</span>
                    <span class="font-bold text-white ml-1">{html.escape(str(item.get("marketing_lead_status") or ""))}</span>
                  </div>
                  <div>
                    <span class="text-slate-400">Confidence:</span>
                    <span class="text-slate-300 ml-1">{html.escape(str(item.get("detection_confidence") or ""))}</span>
                  </div>
                  <div class="text-slate-300 bg-slate-900/90 p-2 rounded border border-slate-800 text-[11px] leading-relaxed">
                    {html.escape(str(item.get("classification_reason") or ""))}
                  </div>
                </div>

              </div>

              {desc_html}

              <!-- Gallery Strip Header -->
              <div class="mt-4 pt-3 border-t border-slate-800/80">
                <div class="flex items-center justify-between mb-2.5">
                  <div class="text-xs font-semibold text-slate-300 flex items-center gap-2">
                    <span>📷 Full Inspected Photo Gallery ({len(ordered_photos)} photos)</span>
                    <span class="text-[11px] text-slate-500 font-normal">— Click any photo to open fullscreen lightbox</span>
                  </div>
                </div>
                <div class="gallery-container flex gap-3 overflow-x-auto pb-2.5 pt-1">
                  {''.join(thumbs_html) if thumbs_html else '<div class="text-xs text-slate-500 italic py-4">No local gallery images found for this listing.</div>'}
                </div>
              </div>
            </article>
            """
        )

    lightbox_json = json.dumps(lightbox_galleries, ensure_ascii=False)

    doc = f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>UPCA Real Estate Marketing &amp; Media — Visual Gallery &amp; Drone Lead Report</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    body {{ font-family: 'Inter', sans-serif; }}
    .font-mono {{ font-family: 'JetBrains Mono', monospace; }}
    ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
    ::-webkit-scrollbar-track {{ background: #0f172a; border-radius: 4px; }}
    ::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 4px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
    .gallery-grid-mode {{
      display: grid !important;
      grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)) !important;
      overflow-x: visible !important;
    }}
    .gallery-grid-mode .photo-thumb {{
      width: 100% !important;
    }}
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen antialiased selection:bg-emerald-500 selection:text-white">

  <!-- TOP STICKY NAVBAR -->
  <header class="sticky top-0 z-40 bg-slate-900/95 backdrop-blur border-b border-slate-800 shadow-lg">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between gap-4">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-emerald-500 to-sky-500 flex items-center justify-center shadow-lg shadow-emerald-500/20 font-black text-white text-lg">
          🚁
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h1 class="text-base sm:text-lg font-extrabold text-white tracking-tight">UPCA Visual Gallery &amp; Drone Lead Review</h1>
            <span class="hidden sm:inline-block px-2.5 py-0.5 text-[11px] font-bold rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
              {total} Listings • {total_photos_count} Photos
            </span>
          </div>
          <p class="text-xs text-slate-400">Filter by City, Drone Status, New Listings, or Agent Contact Details</p>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <a href="/" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold transition">
          ← Scraper Dashboard
        </a>
        <a href="/download/excel" download class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold transition shadow-sm">
          📥 Excel (.xlsx)
        </a>
        <a href="/download/csv" download class="hidden sm:inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 text-xs font-semibold transition">
          📥 Leads CSV
        </a>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 space-y-6">

    <!-- DYNAMIC KPI SUMMARY BAR -->
    <div class="grid grid-cols-2 sm:grid-cols-5 gap-3">
      <div onclick="setStatusFilter('all')" class="cursor-pointer bg-slate-900 hover:bg-slate-800/80 border border-slate-800 rounded-xl p-3.5 transition">
        <div class="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Showing Listings</div>
        <div id="kpiTotal" class="text-2xl font-black text-white mt-1">{total}</div>
        <div class="text-[11px] text-slate-500">Across selected location</div>
      </div>
      <div onclick="setStatusFilter('nodrone')" class="cursor-pointer bg-gradient-to-br from-emerald-950/40 to-slate-900 hover:from-emerald-900/40 border border-emerald-500/40 rounded-xl p-3.5 transition">
        <div class="text-[11px] font-bold uppercase tracking-wider text-emerald-400">⚡ No Drone (Leads)</div>
        <div id="kpiNoDrone" class="text-2xl font-black text-emerald-400 mt-1">{no_drone_count}</div>
        <div class="text-[11px] text-emerald-500/80">High-priority pitch targets</div>
      </div>
      <div onclick="setStatusFilter('drone')" class="cursor-pointer bg-slate-900 hover:bg-slate-800/80 border border-sky-500/30 rounded-xl p-3.5 transition">
        <div class="text-[11px] font-bold uppercase tracking-wider text-sky-400">🚁 With Drone Photos</div>
        <div id="kpiDrone" class="text-2xl font-black text-sky-400 mt-1">{drone_count}</div>
        <div class="text-[11px] text-slate-500">Aerial shots detected</div>
      </div>
      <div onclick="setStatusFilter('uncertain')" class="cursor-pointer bg-slate-900 hover:bg-slate-800/80 border border-amber-500/30 rounded-xl p-3.5 transition">
        <div class="text-[11px] font-bold uppercase tracking-wider text-amber-400">❓ Uncertain / Review</div>
        <div id="kpiUncertain" class="text-2xl font-black text-amber-400 mt-1">{uncertain_count}</div>
        <div class="text-[11px] text-slate-500">Elevated exterior angle</div>
      </div>
      <div onclick="setStatusFilter('new')" class="cursor-pointer bg-slate-900 hover:bg-slate-800/80 border border-indigo-500/30 rounded-xl p-3.5 transition col-span-2 sm:col-span-1">
        <div class="text-[11px] font-bold uppercase tracking-wider text-indigo-400">✨ New Listings</div>
        <div id="kpiNew" class="text-2xl font-black text-indigo-400 mt-1">{new_count}</div>
        <div class="text-[11px] text-slate-500">First-time discovered</div>
      </div>
    </div>

    <!-- MULTI-FILTER CONTROL PANEL -->
    <div class="bg-slate-900 border border-slate-800 rounded-2xl p-4 sm:p-5 shadow-md space-y-4">

      <!-- Row 1: Location Pills & Dropdown -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-slate-800">
        <div class="space-y-1.5">
          <div class="text-xs font-bold uppercase tracking-wider text-slate-400">1. Filter by Location / City</div>
          <div class="flex flex-wrap gap-2" id="locationPillsContainer">
            {''.join(loc_pills_html)}
          </div>
        </div>
        <div class="sm:w-56 shrink-0">
          <label class="block text-[11px] font-semibold text-slate-400 mb-1">Quick City Select</label>
          <select id="locationSelect" onchange="setLocationFilter(this.value, null)"
                  class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:ring-2 focus:ring-sky-500">
            {''.join(loc_options_html)}
          </select>
        </div>
      </div>

      <!-- Row 2: Drone Status Pills + Search Input + View Toggles -->
      <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-4">
        <div class="space-y-1.5">
          <div class="text-xs font-bold uppercase tracking-wider text-slate-400">2. Filter by Drone Status &amp; New Listings</div>
          <div class="flex flex-wrap gap-2">
            <button onclick="setStatusFilter('all')" data-status-btn="all" class="status-pill px-3.5 py-1.5 rounded-lg text-xs font-bold bg-emerald-600 text-white shadow-sm transition">
              All Statuses
            </button>
            <button onclick="setStatusFilter('nodrone')" data-status-btn="nodrone" class="status-pill px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-emerald-300 hover:bg-slate-700 border border-emerald-500/30 transition">
              ⚡ No Drone Photos
            </button>
            <button onclick="setStatusFilter('drone')" data-status-btn="drone" class="status-pill px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-sky-300 hover:bg-slate-700 border border-sky-500/30 transition">
              🚁 With Drone Photos
            </button>
            <button onclick="setStatusFilter('uncertain')" data-status-btn="uncertain" class="status-pill px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-amber-300 hover:bg-slate-700 border border-amber-500/30 transition">
              ❓ Uncertain (Manual Review)
            </button>
            <button onclick="setStatusFilter('new')" data-status-btn="new" class="status-pill px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-slate-800 text-indigo-300 hover:bg-slate-700 border border-indigo-500/30 transition">
              ✨ New Listings Only
            </button>
          </div>
        </div>

        <!-- Search + Gallery Mode Controls -->
        <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-2.5">
          <div class="relative sm:w-64">
            <input id="searchBox" oninput="applyFilters()" type="text" placeholder="Search address, agent, phone, MLS..."
                   class="w-full bg-slate-950 border border-slate-700 rounded-lg pl-8 pr-3 py-2 text-xs text-white focus:outline-none focus:ring-2 focus:ring-emerald-500"/>
            <span class="absolute left-2.5 top-2 text-slate-500 text-xs">🔍</span>
          </div>

          <select id="photoCatSelect" onchange="applyPhotoFilter()"
                  class="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-slate-200 focus:outline-none focus:ring-2 focus:ring-sky-500">
            <option value="all">📷 All Gallery Photos</option>
            <option value="exterior">🏡 Exterior &amp; Candidates Only</option>
            <option value="candidate">🚁 Aerial Candidates Only</option>
          </select>

          <button id="layoutToggleBtn" onclick="toggleGalleryLayout()"
                  class="px-3 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-slate-200 border border-slate-700 transition whitespace-nowrap">
            ⊞ Expand Grid
          </button>
        </div>
      </div>
    </div>

    <!-- Empty Filter Result Notice -->
    <div id="emptyNotice" class="hidden bg-slate-900 border border-slate-800 rounded-2xl p-12 text-center space-y-3">
      <div class="text-3xl">🔍</div>
      <div class="text-base font-bold text-white">No listings match your current filter combination</div>
      <p class="text-xs text-slate-400">Try selecting "All Locations" or "All Statuses" above, or clear your search query.</p>
      <button onclick="resetAllFilters()" class="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold transition">
        Reset All Filters
      </button>
    </div>

    <!-- LISTINGS CARDS CONTAINER -->
    <div id="cardsContainer" class="space-y-6">
      {''.join(cards_html)}
    </div>

  </main>

  <!-- FULLSCREEN PHOTO LIGHTBOX MODAL -->
  <div id="lightboxModal" class="fixed inset-0 z-50 hidden bg-black/95 backdrop-blur-md flex flex-col justify-between p-4 sm:p-6 select-none">
    <!-- Top Bar -->
    <div class="flex items-center justify-between gap-4 border-b border-slate-800 pb-3">
      <div>
        <div id="lbTitle" class="text-sm sm:text-base font-bold text-white">Property Photo</div>
        <div id="lbSubtitle" class="text-xs text-slate-400">Agent &amp; Classification Details</div>
      </div>
      <div class="flex items-center gap-2">
        <a id="lbRealtorLink" href="#" target="_blank" class="px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold">
          Open Listing ↗
        </a>
        <button onclick="closeLightbox()" class="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-bold">
          ✕ Close (Esc)
        </button>
      </div>
    </div>

    <!-- Center Image Area -->
    <div class="relative flex-1 flex items-center justify-center my-4 overflow-hidden">
      <button onclick="stepLightbox(-1)" class="absolute left-2 sm:left-6 z-10 w-11 h-11 rounded-full bg-slate-900/90 hover:bg-slate-800 border border-slate-700 text-white flex items-center justify-center text-lg font-bold shadow-xl">
        ←
      </button>
      <img id="lbImage" src="" alt="Full Preview" class="max-h-[75vh] max-w-full object-contain rounded-lg shadow-2xl border border-slate-800"/>
      <button onclick="stepLightbox(1)" class="absolute right-2 sm:right-6 z-10 w-11 h-11 rounded-full bg-slate-900/90 hover:bg-slate-800 border border-slate-700 text-white flex items-center justify-center text-lg font-bold shadow-xl">
        →
      </button>
    </div>

    <!-- Bottom Caption Bar -->
    <div class="flex flex-col sm:flex-row items-center justify-between gap-2 border-t border-slate-800 pt-3 text-xs">
      <div class="flex items-center gap-2">
        <span id="lbCounter" class="px-2.5 py-1 rounded bg-slate-800 font-mono font-bold text-white">1 / 1</span>
        <span id="lbBadge" class="px-2.5 py-1 rounded bg-emerald-500/20 text-emerald-300 font-bold">Exterior</span>
        <span id="lbDims" class="font-mono text-slate-400">1600×1067</span>
      </div>
      <div id="lbNotes" class="text-slate-300 font-mono text-center sm:text-right"></div>
    </div>
  </div>

  <script>
    const GALLERIES = {lightbox_json};
    let activeLocation = 'all';
    let activeStatus = 'all';
    let gridMode = false;

    let currentLbListingId = null;
    let currentLbIndex = 0;

    function setLocationFilter(loc, btnEl) {{
      activeLocation = loc;
      document.getElementById('locationSelect').value = loc;
      document.querySelectorAll('.loc-pill').forEach(b => {{
        if (b.dataset.locBtn === loc) {{
          b.className = 'loc-pill active px-3.5 py-1.5 rounded-lg text-xs font-semibold transition flex items-center gap-1.5 bg-sky-600 text-white shadow-sm';
        }} else {{
          b.className = 'loc-pill px-3.5 py-1.5 rounded-lg text-xs font-medium transition flex items-center gap-1.5 bg-slate-800/90 text-slate-300 hover:bg-slate-700 hover:text-white border border-slate-700';
        }}
      }});
      applyFilters();
    }}

    function setStatusFilter(status) {{
      activeStatus = status;
      document.querySelectorAll('.status-pill').forEach(b => {{
        if (b.dataset.statusBtn === status) {{
          b.classList.add('ring-2', 'ring-white', 'bg-emerald-600', 'text-white');
        }} else {{
          b.classList.remove('ring-2', 'ring-white', 'bg-emerald-600', 'text-white');
        }}
      }});
      applyFilters();
    }}

    function resetAllFilters() {{
      document.getElementById('searchBox').value = '';
      document.getElementById('photoCatSelect').value = 'all';
      applyPhotoFilter();
      setLocationFilter('all', null);
      setStatusFilter('all');
    }}

    function applyFilters() {{
      const q = (document.getElementById('searchBox').value || '').trim().toLowerCase();
      let shownTotal = 0, shownNoDrone = 0, shownDrone = 0, shownUncertain = 0, shownNew = 0;

      document.querySelectorAll('.listing-card').forEach(card => {{
        const cCity = card.dataset.city || '';
        const cStatus = card.dataset.status || '';
        const cNew = card.dataset.new === 'true';
        const cSearch = card.dataset.search || '';

        const locMatch = (activeLocation === 'all') || (cCity.toLowerCase() === activeLocation.toLowerCase());
        const searchMatch = !q || cSearch.includes(q);

        // Count KPIs for current location + search filter
        if (locMatch && searchMatch) {{
          shownTotal++;
          if (cStatus === 'nodrone') shownNoDrone++;
          else if (cStatus === 'drone') shownDrone++;
          else if (cStatus === 'uncertain') shownUncertain++;
          if (cNew) shownNew++;
        }}

        let statusMatch = true;
        if (activeStatus === 'new') statusMatch = cNew;
        else if (activeStatus !== 'all') statusMatch = (cStatus === activeStatus);

        if (locMatch && statusMatch && searchMatch) {{
          card.style.display = '';
        }} else {{
          card.style.display = 'none';
        }}
      }});

      document.getElementById('kpiTotal').innerText = shownTotal;
      document.getElementById('kpiNoDrone').innerText = shownNoDrone;
      document.getElementById('kpiDrone').innerText = shownDrone;
      document.getElementById('kpiUncertain').innerText = shownUncertain;
      document.getElementById('kpiNew').innerText = shownNew;

      const visibleCards = Array.from(document.querySelectorAll('.listing-card')).filter(c => c.style.display !== 'none');
      document.getElementById('emptyNotice').classList.toggle('hidden', visibleCards.length > 0);
    }}

    function applyPhotoFilter() {{
      const cat = document.getElementById('photoCatSelect').value;
      document.querySelectorAll('.photo-thumb').forEach(thumb => {{
        const tCat = thumb.dataset.photoCat;
        if (cat === 'all') {{
          thumb.style.display = '';
        }} else if (cat === 'exterior') {{
          thumb.style.display = (tCat === 'exterior' || tCat === 'candidate') ? '' : 'none';
        }} else if (cat === 'candidate') {{
          thumb.style.display = (tCat === 'candidate') ? '' : 'none';
        }}
      }});
    }}

    function toggleGalleryLayout() {{
      gridMode = !gridMode;
      const btn = document.getElementById('layoutToggleBtn');
      btn.innerText = gridMode ? '↔️ Filmstrip View' : '⊞ Expand Grid';
      document.querySelectorAll('.gallery-container').forEach(el => {{
        el.classList.toggle('gallery-grid-mode', gridMode);
      }});
    }}

    function openLightbox(listingId, index) {{
      const items = GALLERIES[listingId] || [];
      if (!items.length) return;
      currentLbListingId = listingId;
      currentLbIndex = index;
      renderLightboxSlide();
      document.getElementById('lightboxModal').classList.remove('hidden');
    }}

    function renderLightboxSlide() {{
      const items = GALLERIES[currentLbListingId] || [];
      if (!items.length) return;
      const item = items[currentLbIndex];
      const imgEl = document.getElementById('lbImage');
      imgEl.onerror = function() {{ this.onerror = null; this.src = item.fallback; }};
      imgEl.src = item.src;

      document.getElementById('lbTitle').innerText = `${{item.address}} (${{item.city}}) — ${{item.price}}`;
      document.getElementById('lbSubtitle').innerText = `Agent: ${{item.agent}}`;
      document.getElementById('lbRealtorLink').href = item.listing_url;
      document.getElementById('lbCounter').innerText = `Photo #${{item.index}} (${{currentLbIndex + 1}} of ${{items.length}})`;
      document.getElementById('lbBadge').innerText = item.tag;
      document.getElementById('lbDims').innerText = item.dims;
      document.getElementById('lbNotes').innerText = item.notes;
    }}

    function stepLightbox(delta) {{
      const items = GALLERIES[currentLbListingId] || [];
      if (!items.length) return;
      currentLbIndex = (currentLbIndex + delta + items.length) % items.length;
      renderLightboxSlide();
    }}

    function closeLightbox() {{
      document.getElementById('lightboxModal').classList.add('hidden');
    }}

    window.addEventListener('keydown', (e) => {{
      const modal = document.getElementById('lightboxModal');
      if (modal.classList.contains('hidden')) return;
      if (e.key === 'Escape') closeLightbox();
      else if (e.key === 'ArrowRight') stepLightbox(1);
      else if (e.key === 'ArrowLeft') stepLightbox(-1);
    }});
  </script>
</body>
</html>
"""
    report_path.write_text(doc, encoding="utf-8")
