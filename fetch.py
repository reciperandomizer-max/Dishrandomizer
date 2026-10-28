"""
fetch.py — finds a recipe for every dish in Supabase that doesn't have one yet.

Order tried for each dish:
  1. Hebbar's Kitchen   2. Swasthi's Recipes   3. Archana's Kitchen
  4. Ruchik Randhap     5. TheMealDB (free API)

Saves title, picture link, description, servings, times, ingredients and steps,
plus the source name and link, into the `recipes` table.

Needs two environment variables (set as GitHub secrets):
  SUPABASE_URL          e.g. https://abcd1234.supabase.co
  SUPABASE_SERVICE_KEY  the service_role key (Project Settings > API Keys)
"""

import os
import re
import sys
import time
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_html

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SB = {
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
}
WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}
PAUSE = 1.0  # seconds between requests to each site, to be polite

SITES = [
    {
        "name": "Hebbar's Kitchen",
        "host": "hebbarskitchen.com",
        "search": [
            "https://hebbarskitchen.com/wp-json/wp/v2/search?search={q}&per_page=10",
            "https://hebbarskitchen.com/?s={q}",
        ],
    },
    {
        "name": "Swasthi's Recipes",
        "host": "indianhealthyrecipes.com",
        "search": [
            "https://www.indianhealthyrecipes.com/wp-json/wp/v2/search?search={q}&per_page=10",
            "https://www.indianhealthyrecipes.com/?s={q}",
        ],
    },
    {
        "name": "Archana's Kitchen",
        "host": "archanaskitchen.com",
        "search": [
            "https://www.archanaskitchen.com/search?q={q}",
            "https://www.archanaskitchen.com/search?search={q}",
        ],
    },
    {
        "name": "Ruchik Randhap",
        "host": "ruchikrandhap.com",
        "search": [
            "https://www.ruchikrandhap.com/wp-json/wp/v2/search?search={q}&per_page=10",
            "https://www.ruchikrandhap.com/?s={q}",
        ],
    },
]

# Link paths that are never a single recipe page
SKIP_PATH = re.compile(
    r"/(tag|category|categories|page|search|author|wp-|feed|collections?|"
    r"articles?|videos?|login|account|cart|about|contact|privacy)(/|$)",
    re.I,
)
STOP_WORDS = {"recipe", "recipes", "and", "with", "the", "a", "of", "all", "veg", "types", "different"}


# ---------- helpers ----------

def search_term(dish_name: str) -> str:
    """'Sambar (all veg)' -> 'Sambar'; 'Grilled chicken salad + corn' -> 'Grilled chicken salad'."""
    q = re.sub(r"\(.*?\)", " ", dish_name)
    q = q.split("+")[0]
    return re.sub(r"\s+", " ", q).strip() or dish_name


def words(text: str) -> set:
    """Lower-case words, with a plural 's' dropped so 'sannas' matches 'sanna'."""
    out = set()
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in STOP_WORDS or len(w) < 2:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def get(url: str, **kw):
    try:
        r = requests.get(url, headers=WEB, timeout=25, **kw)
        time.sleep(PAUSE)
        return r if r.ok else None
    except requests.RequestException as e:
        print(f"    ! {url} -> {e}")
        return None


def candidate_links(site: dict, query: str) -> list:
    """Search one site and return recipe page links, best matches first."""
    want = words(query)
    found = []
    for pattern in site["search"]:
        r = get(pattern.format(q=quote_plus(query)))
        if not r:
            continue
        links = []
        if "json" in r.headers.get("Content-Type", ""):
            try:
                links = [item.get("url", "") for item in r.json() if isinstance(item, dict)]
            except ValueError:
                links = []
        else:
            soup = BeautifulSoup(r.text, "html.parser")
            links = [urljoin(r.url, a["href"]) for a in soup.find_all("a", href=True)]
        for link in links:
            u = urlparse(link)
            if site["host"] not in u.netloc or SKIP_PATH.search(u.path) or u.path.strip("/") == "":
                continue
            score = len(want & words(u.path))
            if score:
                found.append((score, link.split("#")[0].split("?")[0]))
        if found:
            break
    found.sort(key=lambda x: -x[0])
    seen, ordered = set(), []
    for _, link in found:
        if link not in seen:
            seen.add(link)
            ordered.append(link)
    return ordered[:5]


def safe(fn):
    try:
        v = fn()
        return v if v not in ("", None) else None
    except Exception:
        return None


def to_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def scrape(site_name: str, url: str):
    r = get(url)
    if not r:
        return None
    try:
        s = scrape_html(r.text, org_url=url, supported_only=False)
    except Exception as e:
        print(f"    ! could not read {url}: {e}")
        return None
    ingredients = safe(s.ingredients) or []
    steps = safe(s.instructions_list) or []
    if not ingredients or not steps:
        return None
    return {
        "title": safe(s.title),
        "source_name": site_name,
        "source_url": url,
        "image_url": safe(s.image),
        "description": safe(s.description),
        "yields": safe(s.yields),
        "prep_time": to_int(safe(s.prep_time)),
        "cook_time": to_int(safe(s.cook_time)),
        "total_time": to_int(safe(s.total_time)),
        "ingredients": ingredients,
        "instructions": steps,
    }


def from_mealdb(query: str):
    r = get(f"https://www.themealdb.com/api/json/v1/1/search.php?s={quote_plus(query)}")
    if not r:
        return None
    meals = (r.json() or {}).get("meals") or []
    if not meals:
        return None
    m = meals[0]
    ingredients = []
    for i in range(1, 21):
        ing = (m.get(f"strIngredient{i}") or "").strip()
        qty = (m.get(f"strMeasure{i}") or "").strip()
        if ing:
            ingredients.append(f"{qty} {ing}".strip())
    steps = [s.strip() for s in re.split(r"\r?\n+", m.get("strInstructions") or "") if s.strip()]
    if not ingredients or not steps:
        return None
    return {
        "title": m.get("strMeal"),
        "source_name": "TheMealDB",
        "source_url": m.get("strSource") or f"https://www.themealdb.com/meal/{m.get('idMeal')}",
        "image_url": m.get("strMealThumb"),
        "description": None,
        "yields": None,
        "prep_time": None,
        "cook_time": None,
        "total_time": None,
        "ingredients": ingredients,
        "instructions": steps,
    }


def find_recipe(dish_name: str):
    query = search_term(dish_name)
    for site in SITES:
        for link in candidate_links(site, query):
            recipe = scrape(site["name"], link)
            if recipe:
                return recipe
    return from_mealdb(query)


# ---------- Supabase ----------

def dishes_without_recipe() -> list:
    dishes = requests.get(f"{SB_URL}/rest/v1/dishes?select=id,name&order=id", headers=SB, timeout=30)
    dishes.raise_for_status()
    have = requests.get(f"{SB_URL}/rest/v1/recipes?select=dish_id", headers=SB, timeout=30)
    have.raise_for_status()
    done = {row["dish_id"] for row in have.json()}
    return [d for d in dishes.json() if d["id"] not in done]


def save(dish_id: int, recipe: dict):
    body = {"dish_id": dish_id, **recipe}
    r = requests.post(
        f"{SB_URL}/rest/v1/recipes",
        headers={**SB, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=body,
        timeout=30,
    )
    r.raise_for_status()


def main():
    todo = dishes_without_recipe()
    print(f"{len(todo)} dish(es) need a recipe.\n")
    found, missing = 0, []
    for d in todo:
        print(f"- {d['name']}")
        recipe = find_recipe(d["name"])
        if recipe:
            save(d["id"], recipe)
            found += 1
            print(f"    saved from {recipe['source_name']}: {recipe['source_url']}")
        else:
            missing.append(d["name"])
            print("    no recipe found")
    print(f"\nDone. Saved {found}, not found {len(missing)}.")
    if missing:
        print("Not found: " + ", ".join(missing))


if __name__ == "__main__":
    sys.exit(main())
