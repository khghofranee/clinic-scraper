"""
Clinic Marketplace MCP Server — streamable-http transport
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

mcp = FastMCP(
    "Clinic Scraper",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8080")),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    ),
)

PERPLEXITY_URL   = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar"
HTTP_TIMEOUT     = float(os.getenv("HTTP_TIMEOUT", "60"))

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

def _week_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-W%V")

async def _ask_perplexity(system: str, user: str) -> Any:
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        raise RuntimeError("PERPLEXITY_API_KEY is not set")
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
        raise ValueError(f"Cannot parse: {clean[:300]}")

async def _get_db():
    import asyncpg
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return await asyncpg.connect(dsn)

@mcp.tool()
async def discover_clinics(city: str, country: str = "IE") -> list[dict[str, Any]]:
    """Find all clinics in a city using Perplexity. Saves them to DB."""
    country_name = COUNTRY_NAMES.get(country, country)
    system = "You are a web search assistant. Return ONLY a raw JSON array. No markdown."
    user = (
        f"Find ALL private clinics in {city}, {country_name}: dental, cosmetic, hair, "
        f"laser, skin, eye, fertility, physio, GP, weight loss. Aim for 15+.\n"
        f'Return ONLY: [{{"name":"...","type":"...","address":"..."}}]'
    )
    data = await _ask_perplexity(system, user)
    if not isinstance(data, list):
        return []
    conn = await _get_db()
    clinics = []
    try:
        for i, item in enumerate(data):
            name = (item.get("name") or "").strip()
            if not name:
                continue
            clinic_id = f"clinic_{country.lower()}_{city.lower()[:3]}_{i+1:04d}"
            await conn.execute(
                "INSERT INTO clinics (id,name,city,clinic_type,country,address) "
                "VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (id) DO UPDATE "
                "SET name=EXCLUDED.name, clinic_type=EXCLUDED.clinic_type",
                clinic_id, name, city, item.get("type","clinic"), country, item.get("address"),
            )
            clinics.append({"clinic_id":clinic_id,"name":name,"city":city,"type":item.get("type","clinic"),"country":country})
    finally:
        await conn.close()
    return clinics

@mcp.tool()
async def scrape_platform(clinic_name: str, city: str, country: str, platform: str) -> dict[str, Any]:
    """Use Perplexity to get rating + review count for a clinic on one platform."""
    hint = PLATFORM_HINTS.get(platform.lower(), f"{platform} listing")
    country_name = COUNTRY_NAMES.get(country, country)
    system = "Return ONLY raw JSON. No markdown. Null if not found."
    user = (
        f"Find the {hint} of '{clinic_name}' in {city}, {country_name}. "
        f"Return ONLY: "
        f'{{"rating":<float or null>,"review_count":<int or null>,"source_url":"<url or null>"}}'
    )
    data = await _ask_perplexity(system, user)
    rating = float(data["rating"]) if data.get("rating") is not None else None
    if rating is not None and not (1.0 <= rating <= 5.0):
        rating = None
    return {"platform":platform.lower(),"rating":rating,
            "review_count":int(data["review_count"]) if data.get("review_count") else 0,
            "source_url":data.get("source_url")}

@mcp.tool()
def compute_rating(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Weighted marketplace rating: sum(rating x reviews) / sum(reviews)."""
    valid = [s for s in sources if s.get("rating") and s.get("review_count")]
    total = sum(int(s["review_count"]) for s in valid)
    if not total:
        return {"marketplace_rating":None,"marketplace_reviews":0}
    weighted = sum(float(s["rating"])*int(s["review_count"]) for s in valid)
    return {"marketplace_rating":round(weighted/total,2),"marketplace_reviews":total,
            "platforms_scraped":[s["platform"] for s in valid]}

@mcp.tool()
async def save_result(clinic_id: str, country: str, platform_results: list[dict[str,Any]],
                      marketplace_rating: float | None, marketplace_reviews: int) -> dict[str,Any]:
    """Write weekly snapshot rows to Postgres."""
    week = _week_stamp()
    conn = await _get_db()
    try:
        for pr in platform_results:
            await conn.execute(
                "INSERT INTO platform_snapshots (clinic_id,country,week_stamp,platform,"
                "platform_rating,platform_reviews,source_url) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                clinic_id, country, week, pr["platform"],
                pr.get("rating"), pr.get("review_count",0), pr.get("source_url"),
            )
        await conn.execute(
            "INSERT INTO marketplace_snapshots (clinic_id,country,week_stamp,"
            "marketplace_rating,marketplace_reviews,platforms_scraped) VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (clinic_id,week_stamp) DO UPDATE SET "
            "marketplace_rating=EXCLUDED.marketplace_rating,"
            "marketplace_reviews=EXCLUDED.marketplace_reviews,"
            "platforms_scraped=EXCLUDED.platforms_scraped,computed_at=NOW()",
            clinic_id, country, week, marketplace_rating, marketplace_reviews,
            [pr["platform"] for pr in platform_results if pr.get("rating")],
        )
    finally:
        await conn.close()
    return {"saved":True,"clinic_id":clinic_id,"week_stamp":week,
            "marketplace_rating":marketplace_rating,"platforms_saved":len(platform_results)}

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
