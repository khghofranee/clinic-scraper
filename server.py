"""
Clinic Marketplace MCP Server
==============================
Tools Claude calls weekly via Elsa:
  1. discover_clinics      – find all clinics in a city (Perplexity)
  2. scrape_platform       – get rating + reviews per platform (Perplexity)
  3. compute_rating        – weighted marketplace formula
  4. save_result           – write snapshot rows to Postgres
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
import asyncpg
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Clinic Scraper")

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
PERPLEXITY_URL   = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"
HTTP_TIMEOUT     = float(os.getenv("HTTP_TIMEOUT", "60"))

COUNTRY_PLATFORMS: dict[str, list[str]] = {
    "IE": ["google", "whatclinic", "facebook", "trustpilot", "localitybiz", "yelp"],
    "GB": ["google", "trustpilot", "whatclinic", "facebook", "nhs", "doctify"],
    "DE": ["google", "facebook", "trustpilot", "jameda"],
    "HR": ["google", "facebook", "whatclinic", "najdoktor"],
    "CH": ["google", "facebook", "trustpilot", "onedoc", "doctify"],
}

PLATFORM_HINTS = {
    "google":      "Google Maps / Google Reviews",
    "facebook":    "Facebook page rating",
    "whatclinic":  "WhatClinic.com listing",
    "trustpilot":  "Trustpilot business page",
    "localitybiz": "LocalityBiz.ie listing",
    "yelp":        "Yelp listing",
    "nhs":         "NHS Reviews listing",
    "doctify":     "Doctify.com profile",
    "najdoktor":   "Najdoktor.hr listing",
    "onedoc":      "OneDoc.ch listing",
    "jameda":      "Jameda.de listing",
}

COUNTRY_NAMES = {
    "IE": "Ireland", "GB": "United Kingdom",
    "DE": "Germany", "HR": "Croatia", "CH": "Switzerland",
}


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def _week_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-W%V")


async def _ask_perplexity(system: str, user: str) -> Any:
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY must be set")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            PERPLEXITY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": PERPLEXITY_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "temperature": 0,
            },
        )
        resp.raise_for_status()

    raw   = resp.json()["choices"][0]["message"]["content"].strip()
    clean = re.sub(r"```(?:json)?|```", "", raw).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        for pattern in [r"\[.*\]", r"\{.*\}"]:
            m = re.search(pattern, clean, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    continue
        raise ValueError(f"Cannot parse Perplexity response: {clean[:300]}")


async def _db() -> asyncpg.Connection:
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL must be set")
    return await asyncpg.connect(dsn)


# ─────────────────────────────────────────────
# Tool 1 – discover_clinics
# ─────────────────────────────────────────────
@mcp.tool()
async def discover_clinics(city: str, country: str = "IE") -> list[dict[str, Any]]:
    """
    Find all clinic names and types in a city using Perplexity web search.
    Returns a list of {id, name, city, type, country}.
    Also upserts each clinic into the DB so Claude can reference clinic_id later.
    """
    country_name = COUNTRY_NAMES.get(country, country)

    system = (
        "You are a web search assistant. Search thoroughly and return ONLY "
        "a raw JSON array. No markdown, no explanation."
    )
    user = (
        f"Find ALL types of private clinics in {city}, {country_name}. "
        f"Include: dental, cosmetic, hair, laser, skin, eye, fertility, "
        f"physio, GP, weight loss, orthodontic, and any others.\n"
        f"Aim for 15+ real clinics.\n"
        f"Return ONLY a JSON array:\n"
        f'[{{"name": "...", "type": "dental|cosmetic|hair|etc", "address": "..."}}]'
    )

    data = await _ask_perplexity(system, user)
    if not isinstance(data, list):
        return []

    conn = await _db()
    clinics = []
    try:
        for i, item in enumerate(data):
            name = (item.get("name") or "").strip()
            if not name:
                continue
            # Generate a stable ID from country + city + index
            clinic_id = f"clinic_{country.lower()}_{city.lower()[:3]}_{i+1:04d}"

            await conn.execute(
                """
                INSERT INTO clinics (id, name, city, clinic_type, country, address)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT (id) DO UPDATE
                    SET name=EXCLUDED.name, clinic_type=EXCLUDED.clinic_type
                """,
                clinic_id, name, city,
                item.get("type", "clinic"),
                country,
                item.get("address"),
            )
            clinics.append({
                "clinic_id": clinic_id,
                "name":      name,
                "city":      city,
                "type":      item.get("type", "clinic"),
                "country":   country,
            })
    finally:
        await conn.close()

    return clinics


# ─────────────────────────────────────────────
# Tool 2 – scrape_platform
# ─────────────────────────────────────────────
@mcp.tool()
async def scrape_platform(
    clinic_name: str,
    city: str,
    country: str,
    platform: str,
) -> dict[str, Any]:
    """
    Use Perplexity to find a clinic's rating and review count on one platform.
    Returns {platform, rating, review_count, source_url}.
    """
    country_name = COUNTRY_NAMES.get(country, country)
    hint         = PLATFORM_HINTS.get(platform.lower(), f"{platform} listing")

    system = (
        "You are a precise web data extraction assistant. "
        "Search the web and return ONLY a raw JSON object. "
        "No markdown, no explanation. "
        "If the clinic has no listing, set fields to null."
    )
    user = (
        f"Search for the {hint} of '{clinic_name}' in {city}, {country_name}.\n"
        f"Find the current average star rating (1.0–5.0) and total review count.\n"
        f"If multiple branches exist, pick the one with the most reviews.\n"
        f"Return ONLY:\n"
        f'{{"rating": <float or null>, "review_count": <integer or null>, "source_url": "<url or null>"}}'
    )

    data   = await _ask_perplexity(system, user)
    rating = float(data["rating"]) if data.get("rating") is not None else None

    if rating is not None and not (1.0 <= rating <= 5.0):
        rating = None

    return {
        "platform":     platform.lower(),
        "rating":       rating,
        "review_count": int(data["review_count"]) if data.get("review_count") else 0,
        "source_url":   data.get("source_url"),
    }


# ─────────────────────────────────────────────
# Tool 3 – compute_rating
# ─────────────────────────────────────────────
@mcp.tool()
def compute_rating(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Weighted marketplace rating: Σ(rating × reviews) / Σ(reviews).
    Input: list of scrape_platform results.
    """
    valid = [s for s in sources if s.get("rating") and s.get("review_count")]
    total = sum(int(s["review_count"]) for s in valid)
    if not total:
        return {"marketplace_rating": None, "marketplace_reviews": 0}

    weighted = sum(float(s["rating"]) * int(s["review_count"]) for s in valid)
    return {
        "marketplace_rating":  round(weighted / total, 2),
        "marketplace_reviews": total,
        "platforms_scraped":   [s["platform"] for s in valid],
    }


# ─────────────────────────────────────────────
# Tool 4 – save_result
# ─────────────────────────────────────────────
@mcp.tool()
async def save_result(
    clinic_id:            str,
    country:              str,
    platform_results:     list[dict[str, Any]],
    marketplace_rating:   float | None,
    marketplace_reviews:  int,
) -> dict[str, Any]:
    """
    Write one weekly snapshot to Postgres:
    - One row per platform in platform_snapshots
    - One row in marketplace_snapshots (upserted)
    """
    week  = _week_stamp()
    conn  = await _db()

    try:
        # ── per-platform rows ───────────────────────
        for pr in platform_results:
            await conn.execute(
                """
                INSERT INTO platform_snapshots
                    (clinic_id, country, week_stamp, platform,
                     platform_rating, platform_reviews, source_url)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                """,
                clinic_id, country, week,
                pr["platform"],
                pr.get("rating"),
                pr.get("review_count", 0),
                pr.get("source_url"),
            )

        # ── marketplace aggregate row ────────────────
        platforms_scraped = [pr["platform"] for pr in platform_results if pr.get("rating")]
        await conn.execute(
            """
            INSERT INTO marketplace_snapshots
                (clinic_id, country, week_stamp,
                 marketplace_rating, marketplace_reviews, platforms_scraped)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (clinic_id, week_stamp)
            DO UPDATE SET
                marketplace_rating  = EXCLUDED.marketplace_rating,
                marketplace_reviews = EXCLUDED.marketplace_reviews,
                platforms_scraped   = EXCLUDED.platforms_scraped,
                computed_at         = NOW()
            """,
            clinic_id, country, week,
            marketplace_rating, marketplace_reviews,
            platforms_scraped,
        )
    finally:
        await conn.close()

    return {
        "saved":              True,
        "clinic_id":          clinic_id,
        "week_stamp":         week,
        "marketplace_rating": marketplace_rating,
        "platforms_saved":    len(platform_results),
    }


if __name__ == "__main__":
    mcp.run(transport="sse")
