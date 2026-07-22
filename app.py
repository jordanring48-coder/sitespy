import os
import re
import json
import time
import io
import csv
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory, Response
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
WEBSITE_TIMEOUT = 10
PLACES_TIMEOUT = 30
FIELD_MASK = (
    "places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
    "places.websiteUri,places.rating,places.userRatingCount,"
    "places.types,places.id"
)

# Social media domains we check for "Social Only" status
SOCIAL_DOMAINS = {
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "linkedin.com": "LinkedIn",
    "twitter.com": "Twitter",
    "x.com": "X",
    "tiktok.com": "TikTok",
    "youtube.com": "YouTube",
    "pinterest.com": "Pinterest",
    "snapchat.com": "Snapchat",
    "reddit.com": "Reddit",
}

# US state abbreviations + common city/location words for niche extraction
STATE_ABBR = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

LOCATION_WORDS = {
    "near", "in", "at", "nearby", "around", "downtown", "metro",
    "county", "city", "town", "village", "area", "region", "suburb",
    "north", "south", "east", "west", "central", "northern", "southern",
    "eastern", "western",
}

# Major US cities (lowercase) — stripped from niche extraction
MAJOR_CITIES = {
    "new york", "los angeles", "chicago", "houston", "phoenix", "philadelphia",
    "san antonio", "san diego", "dallas", "austin", "san jose", "fort worth",
    "jacksonville", "columbus", "charlotte", "indianapolis", "san francisco",
    "seattle", "denver", "nashville", "oklahoma city", "el paso", "washington",
    "boston", "las vegas", "portland", "memphis", "louisville", "baltimore",
    "milwaukee", "albuquerque", "tucson", "fresno", "sacramento", "mesa",
    "atlanta", "kansas city", "omaha", "colorado springs", "raleigh",
    "long beach", "virginia beach", "miami", "oakland", "minneapolis",
    "tampa", "tulsa", "arlington", "new orleans", "cleveland", "bakersfield",
    "honolulu", "anaheim", "aurora", "santa ana", "st louis", "riverside",
    "corpus christi", "lexington", "stockton", "henderson", "saint paul",
    "st paul", "pittsburgh", "cincinnati", "anchorage", "greensboro",
    "plano", "newark", "lincoln", "orlando", "irvine", "toledo", "durham",
    "chula vista", "fort wayne", "jersey city", "scottsdale", "norfolk",
    "madison", "orlando", "reno", "buffalo", "boise", "spokane", "richmond",
    "providence", "des moines", "mobile", "montgomery", "augusta", "baton rouge",
    "rochester", "akron", "huntsville", "fayetteville", "shreveport",
    "grand rapids", "salt lake city", "tallahassee", "worcester", "knoxville",
    "new haven", "hartford", "oxnard", "tempe", "san bernardino",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def extract_niche(query: str) -> str:
    """Strip location words, city names, and state abbreviations from query to infer niche."""
    lower = query.lower()
    tokens = lower.split()
    # Build a mask of tokens to remove (by index)
    remove = [False] * len(tokens)

    for i, t in enumerate(tokens):
        clean = t.strip(",.!?;:'\"")
        if clean in STATE_ABBR or clean in LOCATION_WORDS:
            remove[i] = True
        elif clean.isdigit():
            remove[i] = True

    # Check multi-word city names
    for city in MAJOR_CITIES:
        city_tokens = city.split()
        if len(city_tokens) <= 1:
            continue
        # Try to match city tokens as a contiguous sequence
        for start in range(len(tokens) - len(city_tokens) + 1):
            if all(
                tokens[start + j].strip(",.!?;:'\"") == city_tokens[j]
                for j in range(len(city_tokens))
            ):
                for j in range(len(city_tokens)):
                    remove[start + j] = True

    # Also check single-word cities
    single_cities = {c for c in MAJOR_CITIES if " " not in c}
    for i, t in enumerate(tokens):
        clean = t.strip(",.!?;:'\"")
        if clean in single_cities:
            remove[i] = True

    filtered = [tokens[i] for i in range(len(tokens)) if not remove[i]]
    return " ".join(filtered) if filtered else lower


def priority_score(status: str, mobile_friendly: bool, design: str) -> int:
    """Lower = better lead (higher priority)."""
    score_map = {
        "No Website": 0,
        "Social Only": 1,
        "Website Unavailable": 2,
    }
    if status in score_map:
        return score_map[status]

    # Scoring for analyzed websites
    if status == "Poorly Built" and not mobile_friendly:
        return 3
    if status == "Poorly Built":
        return 4
    if status == "Outdated" and not mobile_friendly:
        return 5
    if status == "Outdated":
        return 6
    if not mobile_friendly:
        return 7
    # Modern + Mobile Friendly (or Basic + Mobile Friendly)
    return 8


def classify_social(website: str):
    """Return (True, platform_name) if the website is a social media URL, else (False, None)."""
    if not website:
        return False, None
    try:
        host = urlparse(website).hostname or ""
        host = host.lower().lstrip("www.")
        for domain, platform in SOCIAL_DOMAINS.items():
            if host == domain or host.endswith("." + domain):
                return True, platform
    except Exception:
        pass
    return False, None


def fetch_and_analyze(website: str):
    """Fetch a website and analyze its design quality.
    Returns a dict with all analysis fields.
    """
    result = {
        "mobileFriendly": False,
        "design": "N/A",
        "signals": [],
        "status": "Website Unavailable",
        "socialPlatform": None,
        "designRaw": "N/A",
    }

    if not website:
        result["status"] = "No Website"
        return result

    # Check social first (no need to fetch)
    is_social, platform = classify_social(website)
    if is_social:
        result["status"] = "Social Only"
        result["socialPlatform"] = platform
        result["design"] = "N/A"
        return result

    # Fetch the website
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(website, timeout=WEBSITE_TIMEOUT, headers=headers, allow_redirects=True)

        if resp.status_code >= 400:
            result["status"] = "Website Unavailable"
            return result

        html = resp.text
        return analyze_html(html, result)

    except requests.Timeout:
        result["status"] = "Website Unavailable"
        return result
    except requests.ConnectionError:
        result["status"] = "Website Unavailable"
        return result
    except Exception:
        result["status"] = "Website Unavailable"
        return result


def analyze_html(html: str, result: dict):
    """Parse HTML and extract design signals."""
    soup = BeautifulSoup(html, "lxml")
    signals = []

    # Mobile-friendly check
    viewport = soup.find("meta", attrs={"name": "viewport"})
    if viewport and "width=device-width" in viewport.get("content", ""):
        result["mobileFriendly"] = True
        signals.append("Mobile Viewport")

    # CSS framework detection via class patterns in the whole HTML
    html_lower = html.lower()
    has_tailwind = bool(re.search(r'class="[^"]*?(bg-|text-|p-[0-9]|m-[0-9]|w-[0-9]|h-[0-9])', html_lower))
    has_bootstrap = bool(re.search(r'\b(col-md-|col-sm-|col-lg-|btn-primary|navbar|container-fluid)\b', html_lower))

    if has_tailwind:
        signals.append("Tailwind")
    if has_bootstrap:
        signals.append("Bootstrap")

    # Semantic HTML5 elements
    semantic_tags = ["article", "section", "nav", "header", "footer", "main"]
    has_semantic = False
    for tag in semantic_tags:
        if soup.find(tag):
            has_semantic = True
            break
    if has_semantic:
        signals.append("Semantic HTML5")

    # Modern CSS (grid/flex)
    has_grid = "display: grid" in html_lower or "display:grid" in html_lower
    has_flex = "display: flex" in html_lower or "display:flex" in html_lower
    has_flexbox = "flexbox" in html_lower
    if has_grid:
        signals.append("CSS Grid")
    if has_flex:
        signals.append("Flexbox")
    modern_css = has_grid or has_flex or has_flexbox
    if modern_css:
        # Avoid duplicating "Grid/Flexbox" if already added individually
        if not has_grid and not has_flex:
            signals.append("Flexbox")

    # Outdated signal detection
    outdated_signals = []
    if soup.find("font"):
        outdated_signals.append("<font> tags")
    if soup.find("marquee"):
        outdated_signals.append("<marquee> tags")
    # Flash embeds
    if re.search(r'\.swf["\']|<embed[^>]*\.swf|<object[^>]*\.swf', html_lower, re.IGNORECASE):
        outdated_signals.append("Flash embeds")
    # Old copyright
    copyright_match = re.search(r'©\s*(20\d{2})', html)
    if copyright_match:
        year = int(copyright_match.group(1))
        if year < 2018:
            outdated_signals.append(f"Old copyright ({year})")
    # <table> used for layout (no thead/tbody with data)
    tables = soup.find_all("table")
    layout_table = False
    for t in tables:
        if not t.find(["thead", "tbody"]) and not t.get("role"):
            # Heuristic: table without thead/tbody and no data-role might be layout
            tbody = t.find("tbody")
            if not tbody:
                layout_table = True
                break
    if layout_table and tables:
        outdated_signals.append("Table-based layout")
    # Broken images
    imgs = soup.find_all("img")
    broken_img_count = 0
    for img in imgs:
        src = img.get("src", "")
        if not src:
            broken_img_count += 1
        elif src.startswith("broken") or "missing" in src.lower() or "placeholder" in src.lower():
            broken_img_count += 1
    if broken_img_count > 0:
        outdated_signals.append(f"Broken images ({broken_img_count})")

    has_outdated = len(outdated_signals) > 0
    if has_outdated:
        signals.extend(outdated_signals)

    # Determine design quality
    has_framework = has_tailwind or has_bootstrap
    has_semantic_html = has_semantic
    has_modern = modern_css

    if outdated_signals:
        result["design"] = "Outdated"
        result["status"] = "Outdated"
    elif has_framework and has_semantic_html and has_modern:
        result["design"] = "Modern"
        result["status"] = "Modern"
    elif has_framework or has_semantic_html or has_modern:
        result["design"] = "Basic"
        result["status"] = "Basic"
    else:
        result["design"] = "Poorly Built"
        result["status"] = "Poorly Built"

    result["signals"] = signals

    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    """Serve the index.html frontend."""
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.route("/api/analyze", methods=["GET"])
def analyze():
    """Main endpoint: search Google Places and analyze business websites."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Missing required parameter: q"}), 400

    try:
        max_results = int(request.args.get("max", "20"))
    except ValueError:
        max_results = 20
    max_results = max(1, min(max_results, 50))  # Clamp 1–50

    # Check API key
    if not GOOGLE_PLACES_API_KEY:
        return jsonify({
            "error": "GOOGLE_PLACES_API_KEY environment variable is not set",
            "results": [],
        }), 200  # Return 200 with empty results so frontend doesn't crash

    niche = extract_niche(query)

    # 1. Call Google Places API (New)
    try:
        places_headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": FIELD_MASK,
        }
        places_body = {
            "textQuery": query,
            "maxResultCount": max_results,
        }
        places_resp = requests.post(
            PLACES_URL,
            headers=places_headers,
            json=places_body,
            timeout=PLACES_TIMEOUT,
        )
        places_data = places_resp.json()
    except requests.Timeout:
        return jsonify({"error": "Google Places API timed out", "results": []}), 200
    except requests.ConnectionError:
        return jsonify({"error": "Could not connect to Google Places API", "results": []}), 200
    except Exception as e:
        return jsonify({"error": f"Google Places API error: {str(e)}", "results": []}), 200

    # Check for API errors
    if places_resp.status_code != 200:
        error_msg = places_data.get("error", {}).get("message", "Unknown Places API error")
        return jsonify({"error": error_msg, "results": []}), 200

    places = places_data.get("places", [])

    # 2. Analyze each place's website
    results = []
    for place in places:
        place_id = place.get("id", "")
        name = place.get("displayName", {}).get("text", "Unknown")
        address = place.get("formattedAddress", "")
        phone = place.get("nationalPhoneNumber", "")
        rating = place.get("rating")
        rating_count = place.get("userRatingCount", 0)
        website = place.get("websiteUri", "")
        types = place.get("types", [])

        # Analyze website
        analysis = fetch_and_analyze(website)

        status = analysis["status"]
        design = analysis["design"]
        mobile_friendly = analysis["mobileFriendly"]
        signals = analysis["signals"]
        social_platform = analysis["socialPlatform"]

        prio = priority_score(status, mobile_friendly, design)

        business = {
            "id": place_id,
            "name": name,
            "niche": niche,
            "address": address,
            "phone": phone,
            "rating": rating,
            "ratingCount": rating_count,
            "website": website,
            "status": status,
            "design": design,
            "mobileFriendly": mobile_friendly,
            "signals": signals,
            "socialPlatform": social_platform,
            "priority": prio,
            "lastAnalyzed": datetime.now(timezone.utc).isoformat(),
        }
        results.append(business)

    # Sort by priority (lower = better lead)
    results.sort(key=lambda r: r["priority"])

    return jsonify({"results": results, "count": len(results)})


@app.route("/api/save-lead", methods=["POST"])
def save_lead():
    """Accept lead data — frontend handles persistence via localStorage."""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400
    # In the future we might persist to a database; for now just acknowledge
    return jsonify({"success": True, "message": "Lead saved"}), 200


# ---------------------------------------------------------------------------
# Export endpoints
# ---------------------------------------------------------------------------

CSV_HEADERS = [
    "Business Name", "Niche", "Address", "Phone", "Rating", "Reviews",
    "Website", "Status", "Design", "Mobile Friendly", "Signals", "Priority",
    "Social Platform",
]


def _get_leads_from_request():
    """Extract leads list from request body (JSON) or query param."""
    # Try JSON body first (works for both GET and POST with body)
    data = request.get_json(silent=True)
    if data and isinstance(data, dict) and "leads" in data:
        return data["leads"]

    # Try query param: ?leads=<JSON array>
    leads_param = request.args.get("leads", "")
    if leads_param:
        try:
            parsed = json.loads(leads_param)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _build_csv(leads):
    """Build CSV content with stats header + data rows."""
    output = io.StringIO()
    writer = csv.writer(output)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Count by status
    status_counts = {}
    for lead in leads:
        st = lead.get("status", "Unknown")
        status_counts[st] = status_counts.get(st, 0) + 1

    # Top niches
    niche_counts = {}
    for lead in leads:
        niche = lead.get("niche", "Unknown")
        niche_counts[niche] = niche_counts.get(niche, 0) + 1
    top_niches = sorted(niche_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    # Stats header
    writer.writerow([f"# SiteSpy Lead Export — {today_str}"])
    writer.writerow([f"# Total Leads: {len(leads)}"])
    by_status = ", ".join(f"{st} ({cnt})" for st, cnt in sorted(status_counts.items()))
    writer.writerow([f"# By Status: {by_status}"])
    top_niches_str = ", ".join(f"{n} ({c})" for n, c in top_niches)
    writer.writerow([f"# Top Niches: {top_niches_str}"])
    writer.writerow([])  # blank row

    # Data header
    writer.writerow(CSV_HEADERS)

    # Data rows
    for lead in leads:
        row = [
            lead.get("name", ""),
            lead.get("niche", ""),
            lead.get("address", ""),
            lead.get("phone", ""),
            lead.get("rating", ""),
            lead.get("ratingCount", ""),
            lead.get("website", ""),
            lead.get("status", ""),
            lead.get("design", ""),
            "Yes" if lead.get("mobileFriendly") else "No",
            "; ".join(lead.get("signals", [])),
            lead.get("priority", ""),
            lead.get("socialPlatform", ""),
        ]
        writer.writerow(row)

    return output.getvalue()


@app.route("/api/export", methods=["GET", "POST"])
def export_csv():
    """Return saved leads as a downloadable CSV file."""
    leads = _get_leads_from_request()
    if leads is None:
        return jsonify({"error": "No leads provided. Send JSON body with 'leads' array."}), 400
    if not isinstance(leads, list):
        return jsonify({"error": "'leads' must be an array."}), 400

    csv_content = _build_csv(leads)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return Response(
        csv_content,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=sitespy-leads-{today_str}.csv",
            "Content-Type": "text/csv; charset=utf-8",
        },
    )


@app.route("/api/export-to-sheets", methods=["POST"])
def export_to_sheets():
    """Create or append to a Google Sheet with lead data."""
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Invalid JSON body"}), 400

    leads = data.get("leads", [])
    if not isinstance(leads, list) or not leads:
        return jsonify({"error": "No leads provided. Send JSON body with 'leads' array."}), 400

    sheet_name = data.get("sheetName", "SiteSpy Leads")
    spreadsheet_id = data.get("spreadsheetId", None)

    # Check for Google credentials
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
    if not creds_json:
        return jsonify({
            "error": "Google Sheets integration not configured. "
                     "Set GOOGLE_SHEETS_CREDENTIALS or download the CSV instead.",
            "configured": False,
        }), 200

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError

        creds_dict = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        service = build("sheets", "v4", credentials=credentials)
    except Exception as e:
        return jsonify({
            "error": f"Failed to initialize Google Sheets client: {str(e)}",
            "configured": False,
        }), 200

    # Build data rows (including header)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Stats
    status_counts = {}
    for lead in leads:
        st = lead.get("status", "Unknown")
        status_counts[st] = status_counts.get(st, 0) + 1
    by_status = ", ".join(f"{st} ({cnt})" for st, cnt in sorted(status_counts.items()))

    niche_counts = {}
    for lead in leads:
        niche = lead.get("niche", "Unknown")
        niche_counts[niche] = niche_counts.get(niche, 0) + 1
    top_niches = sorted(niche_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    top_niches_str = ", ".join(f"{n} ({c})" for n, c in top_niches)

    values = [
        [f"SiteSpy Lead Export — {today_str}"],
        [f"Total Leads: {len(leads)}"],
        [f"By Status: {by_status}"],
        [f"Top Niches: {top_niches_str}"],
        [],
        CSV_HEADERS,
    ]

    for lead in leads:
        row = [
            lead.get("name", ""),
            lead.get("niche", ""),
            lead.get("address", ""),
            lead.get("phone", ""),
            str(lead.get("rating", "")),
            str(lead.get("ratingCount", "")),
            lead.get("website", ""),
            lead.get("status", ""),
            lead.get("design", ""),
            "Yes" if lead.get("mobileFriendly") else "No",
            "; ".join(lead.get("signals", [])),
            str(lead.get("priority", "")),
            lead.get("socialPlatform", ""),
        ]
        values.append(row)

    try:
        if spreadsheet_id:
            # Append to existing sheet
            body = {"values": values}
            service.spreadsheets().values().append(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body=body,
            ).execute()
            sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        else:
            # Create new spreadsheet
            spreadsheet_body = {
                "properties": {"title": sheet_name},
                "sheets": [{"properties": {"title": sheet_name}}],
            }
            sheet = service.spreadsheets().create(
                body=spreadsheet_body, fields="spreadsheetId"
            ).execute()
            spreadsheet_id = sheet.get("spreadsheetId")

            # Populate with data
            body = {"values": values}
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"'{sheet_name}'!A1",
                valueInputOption="RAW",
                body=body,
            ).execute()

            sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"

        return jsonify({
            "success": True,
            "spreadsheetId": spreadsheet_id,
            "url": sheet_url,
        }), 200

    except HttpError as e:
        return jsonify({
            "error": f"Google Sheets API error: {str(e)}",
            "configured": True,
        }), 200
    except Exception as e:
        return jsonify({
            "error": f"Failed to export to Google Sheets: {str(e)}",
            "configured": True,
        }), 200


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port, debug=False)
