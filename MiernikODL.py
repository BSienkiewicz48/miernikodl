# app.py
# Streamlit app: odległości drogowe do wybranej destynacji dla miejscowości z pliku XLSX
# Wymagania:
#   pip install streamlit pandas openpyxl requests
#
# Uruchom:
#   streamlit run app.py

import io
import math
import re
import time
from typing import Optional, Tuple, Dict

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter, Retry

# -------------------- Konfiguracja strony --------------------
st.set_page_config(page_title="Dystanse do destynacji (OSM/OSRM)", layout="wide")

st.title("📍 Dystanse drogowe do wybranej destynacji")
st.write(
    "Wgraj plik XLSX z kolumną miejscowości, wpisz nazwę destynacji (miasto), "
    "a aplikacja doda dystans po drogach, czas przejazdu i dystans w linii prostej."
)

# -------------------- Stałe i pomocnicze --------------------
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_BASE = "https://router.project-osrm.org"

def make_session() -> requests.Session:
    sess = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess

SESSION = make_session()

def build_user_agent(contact_email: Optional[str]) -> str:
    contact = contact_email.strip() if contact_email else ""
    if contact:
        return f"distance-streamlit-app/1.0 (contact: {contact})"
    # Zachęcam do podania maila; poniżej fallback:
    return "distance-streamlit-app/1.0 (contact: bsienkiewicz42@gmail.com)"

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0088  # km
    from math import radians, sin, cos, atan2, sqrt
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlmb / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

def geocode_place(name: str, user_agent: str, country_code: Optional[str] = "pl") -> Optional[Tuple[float, float]]:
    """
    Geokoduje nazwę miejscowości do (lat, lon) przez Nominatim.
    Zwraca None jeśli nie znaleziono.
    """
    if not name or not str(name).strip():
        return None
    q = str(name).strip()
    params = {
        "q": q,
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 0
    }
    if country_code:
        params["countrycodes"] = country_code.lower()

    headers = {"User-Agent": user_agent}
    try:
        resp = SESSION.get(NOMINATIM_URL, params=params, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            return (lat, lon)
    except Exception:
        return None
    return None

def osrm_distance_duration_km_min(
    lat1: float, lon1: float, lat2: float, lon2: float, profile: str = "driving"
) -> Optional[Tuple[float, float]]:
    """
    Zwraca (dystans_km, czas_min) trasą drogową wg OSRM. None jeśli błąd.
    """
    coords = f"{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
    url = f"{OSRM_BASE}/route/v1/{profile}/{coords}"
    params = {"overview": "false", "alternatives": "false", "steps": "false"}
    try:
        resp = SESSION.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            dist_km = route["distance"] / 1000.0
            dur_min = route["duration"] / 60.0
            return (dist_km, dur_min)
    except Exception:
        return None
    return None

def safe_slug(text: str) -> str:
    slug = re.sub(r"\s+", "_", text.strip(), flags=re.UNICODE)
    slug = re.sub(r"[^\w\-]+", "_", slug, flags=re.UNICODE)
    return slug.lower()

def dataframe_to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        df.to_excel(writer, index=False)
    bio.seek(0)
    return bio.getvalue()

# -------------------- Panel wejściowy --------------------
with st.sidebar:
    st.header("⚙️ Ustawienia")
    contact_email = st.text_input(
        "Kontakt do User-Agent (zalecane przez open street map)",
        placeholder="można_wprowadzić.nie_trzeba@firma.pl"
    )
    country_code_in = st.text_input(
        "Kod kraju (np. pl). Zostaw puste, aby nie zawężać. Przy pustym wyszukiwanie trwa dłużej, jeśłi masz tylko lokalizacjie z PL to wpisz pl",
        value=""
    ).strip().lower()
    country_code = country_code_in if country_code_in else None
    geocode_delay = st.number_input(
        "Pauza między zapytaniami geokodowania (sek.)",
        min_value=0.0, max_value=3.0, value=1.0, step=0.5,
        help="Nominatim wymaga co najmniej ~1 s między zapytaniami żeby nie obciążać serwera."
    )

uploaded = st.file_uploader("Wgraj plik XLSX z kolumną miejscowości", type=["xlsx"])

dest_name = st.text_input("Docelowa miejscowość (destynacja)", value="Gdańsk")

compute_btn = st.button("🚀 Policz dystanse i czasy")

# -------------------- Przetwarzanie --------------------
if compute_btn:
    if uploaded is None:
        st.error("Wgraj najpierw plik XLSX.")
        st.stop()

    # Wczytaj dane
    try:
        df = pd.read_excel(uploaded)
    except Exception as e:
        st.error(f"Nie udało się odczytać pliku XLSX: {e}")
        st.stop()

    if df.empty:
        st.warning("Plik jest pusty.")
        st.stop()

    # Wybór kolumny z miejscowościami
    if df.shape[1] == 1:
        city_col = df.columns[0]
    else:
        city_col = st.selectbox(
            "Wybierz kolumnę z miejscowościami",
            options=list(df.columns),
            index=0
        )

    if city_col is None:
        st.error("Wybierz kolumnę z miejscowościami.")
        st.stop()

    # Geokoduj destynację
    user_agent = build_user_agent(contact_email)
    with st.status("Geokoduję destynację...", expanded=False) as status:
        dest_coords = geocode_place(dest_name, user_agent=user_agent, country_code=country_code)
        if dest_coords is None and country_code is not None:
            # drugi strzał bez ograniczenia kraju
            dest_coords = geocode_place(dest_name, user_agent=user_agent, country_code=None)
        if dest_coords is None:
            st.error("Nie udało się zgeokodować destynacji. Spróbuj doprecyzować nazwę (np. 'Gdańsk, Polska').")
            status.update(label="Błąd geokodowania destynacji", state="error")
            st.stop()
        status.update(label=f"OK — destynacja: {dest_name} -> {dest_coords}", state="complete")

    dest_lat, dest_lon = dest_coords
    prefix = safe_slug(dest_name)

    # Przygotowanie danych wyjściowych
    out = df.copy()
    series_cities = out[city_col].astype(str).str.strip()
    unique_cities = sorted({c for c in series_cities.dropna().unique() if c})

    # Geokodowanie unikatów
    st.subheader("Krok 1/2 — geokodowanie miejscowości")
    progress = st.progress(0, text="Geokoduję...")
    geo_cache: Dict[str, Tuple[float, float]] = {}
    total = len(unique_cities)

    for i, city in enumerate(unique_cities, start=1):
        coords = geocode_place(city, user_agent=user_agent, country_code=country_code)
        if coords is None and country_code is not None:
            coords = geocode_place(city, user_agent=user_agent, country_code=None)
        if coords is None:
            geo_cache[city] = (float("nan"), float("nan"))
        else:
            geo_cache[city] = coords
        progress.progress(i / total, text=f"Geokoduję... ({i}/{total})")
        # Szanuj Nominatim (≥1 s między zapytaniami)
        if geocode_delay > 0:
            time.sleep(geocode_delay)
    progress.empty()

    # Mapowanie współrzędnych
    out["_lat"] = series_cities.map(lambda c: geo_cache.get(c, (float("nan"), float("nan")))[0])
    out["_lon"] = series_cities.map(lambda c: geo_cache.get(c, (float("nan"), float("nan")))[1])

    # Krok 2: dystanse i czasy (z cache dla powtarzających się współrzędnych)
    st.subheader("Krok 2/2 — trasy OSRM i dystans prosty")
    coords_list = list(zip(out["_lat"].tolist(), out["_lon"].tolist()))
    unique_coords = [(lat, lon) for (lat, lon) in set(coords_list) if isinstance(lat, float) and isinstance(lon, float) and not (math.isnan(lat) or math.isnan(lon))]

    route_cache: Dict[Tuple[float, float], Tuple[float, float, float]] = {}  # (lat,lon) -> (road_km, dur_min, straight_km)
    progress2 = st.progress(0, text="Liczenie tras...")
    total2 = max(len(unique_coords), 1)

    for i, (lat, lon) in enumerate(unique_coords, start=1):
        straight_km = haversine_km(lat, lon, dest_lat, dest_lon)
        route = osrm_distance_duration_km_min(lat, lon, dest_lat, dest_lon, profile="driving")
        if route is not None:
            road_km, dur_min = route
        else:
            road_km, dur_min = float("nan"), float("nan")
        route_cache[(lat, lon)] = (road_km, dur_min, straight_km)
        progress2.progress(i / total2, text=f"Liczenie tras... ({i}/{total2})")
        time.sleep(0.05)  # lekkie oddechy dla publicznego OSRM
    progress2.empty()

    # Uzupełnij kolumny wynikowe
    road_dists, road_durs, straight_dists = [], [], []
    for lat, lon in coords_list:
        if not (isinstance(lat, float) and isinstance(lon, float)) or math.isnan(lat) or math.isnan(lon):
            road_dists.append(float("nan"))
            road_durs.append(float("nan"))
            straight_dists.append(float("nan"))
        else:
            r = route_cache.get((lat, lon))
            if r is None:
                road_dists.append(float("nan"))
                road_durs.append(float("nan"))
                straight_dists.append(float("nan"))
            else:
                road_km, dur_min, straight_km = r
                road_dists.append(road_km)
                road_durs.append(dur_min)
                straight_dists.append(straight_km)

    out[f"{prefix}_distance_km_road"] = road_dists
    out[f"{prefix}_duration_min_road"] = road_durs
    out[f"{prefix}_distance_km_straight"] = straight_dists
    out[f"{prefix}_lat"] = dest_lat
    out[f"{prefix}_lon"] = dest_lon

    # Uporządkuj: wstaw wynikowe kolumny zaraz po kolumnie z miastami
    cols = list(out.columns)
    base_idx = cols.index(city_col)
    for newcol in [
        f"{prefix}_distance_km_road",
        f"{prefix}_duration_min_road",
        f"{prefix}_distance_km_straight",
        "_lat",
        "_lon",
        f"{prefix}_lat",
        f"{prefix}_lon",
    ]:
        if newcol in cols:
            cols.remove(newcol)
    insert_at = base_idx + 1
    for newcol in [
        f"{prefix}_distance_km_road",
        f"{prefix}_duration_min_road",
        f"{prefix}_distance_km_straight",
        "_lat",
        "_lon",
        f"{prefix}_lat",
        f"{prefix}_lon",
    ][::-1]:
        cols.insert(insert_at, newcol)
    out = out[cols]

    st.success("Gotowe! Poniżej podgląd wyników:")
    st.dataframe(out, use_container_width=True)

    # Przyciski pobierania
    xlsx_bytes = dataframe_to_xlsx_bytes(out)
    file_name = f"miejscowosci_enriched_{safe_slug(dest_name)}.xlsx"
    st.download_button(
        "⬇️ Pobierz wynik XLSX",
        data=xlsx_bytes,
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

