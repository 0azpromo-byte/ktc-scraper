"""
KTC scraper -> Firestore ("cijene" kolekcija)

Ista arhitektura kao kaufland_scraper.py / eurospin_scraper.py, prilagođeno
KTC mehanizmu:
- direktna stranica poslovnice (RC BJELOVAR PJ-50) sa svim dnevnim CSV
  linkovima, datum je u nazivu u obliku YYYYMMDD
- CSV je ; odvojen, windows-1250 encoding
- Kad artikl NIJE na akciji, "MPC za vrijeme posebnog oblika prodaje" je
  "0.00" (ne prazno!) - ista zamka kao kod Eurospina. Kad JEST na akciji,
  "Maloprodajna cijena" je ta koja je "0.00", a stvarna cijena je u MPC
  stupcu.

Pokretanje lokalno (brzi test, 100 artikala):
    LOKALNI_TEST=true python ktc_scraper.py

Puni run (2000 artikala, za GitHub):
    python ktc_scraper.py
"""

import csv
import hashlib
import io
import os
import re
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore

# ---------- KONFIGURACIJA ----------
SERVICE_ACCOUNT = "serviceAccountKey.json"
TRGOVINA = "KTC"
GRAD = "Bjelovar"
DELIMITER = ";"
CSV_ENCODING = "windows-1250"

# Poznata stranica poslovnice (nema potrebe za pretraživanjem svih gradova)
STORE_URL = "https://www.ktc.hr/cjenici?poslovnica=RC%20BJELOVAR%20PJ-50"
BASE_URL = "https://www.ktc.hr"

LOKALNI_TEST = os.environ.get("LOKALNI_TEST", "false").lower() == "true"

if LOKALNI_TEST:
    KVOTA_HRANA = 50
    KVOTA_OSTALO_UKUPNO = 50
else:
    KVOTA_HRANA = 1000
    KVOTA_OSTALO_UKUPNO = 1000

KATEGORIJE_MAP = {
    "hrana": "HRANA",
    "piće": "PIĆE",
    "pića": "PIĆE",
    "pice": "PIĆE",
    "sredstva za čišćenje": "SREDSTVA ZA ČIŠĆENJE",
    "sredstva za ciscenje": "SREDSTVA ZA ČIŠĆENJE",
    "kozmetika": "KOZMETIKA",
    "toaletne potrepštine": "TOALETNE POTREPŠTINE",
    "proizvodi za kućanstvo": "PROIZVODI ZA KUĆANSTVO",
}

# ---------- INICIJALIZACIJA ----------
if not firebase_admin._apps:
    cred = credentials.Certificate(SERVICE_ACCOUNT)
    firebase_admin.initialize_app(cred)
db = firestore.client()


# ---------- PRONALAŽENJE CSV-A ----------
def pronadji_csv_url(datum: datetime) -> str | None:
    resp = requests.get(STORE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content.decode("utf-8"), "html.parser")

    datum_str = datum.strftime("%Y%m%d")
    csv_links = soup.select('a[href$=".csv"]')

    for link in csv_links:
        href = link.get("href", "")
        if datum_str in href:
            url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"
            return requests.utils.requote_uri(url)

    return None


def preuzmi_csv(url: str) -> str:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    for enc in (CSV_ENCODING, "utf-8-sig", "utf-8"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Ne mogu dekodirati CSV s {url}")


# ---------- OBRADA CSV-A ----------
def normaliziraj_kategoriju(raw: str) -> str | None:
    if not raw:
        return None
    raw_lower = re.sub(r"\s+", " ", raw.strip().lower())
    for kljuc, vrijednost in KATEGORIJE_MAP.items():
        if kljuc in raw_lower:
            return vrijednost
    return raw.strip().upper()


def parsiraj_broj(raw: str) -> float | None:
    """Vraća None za prazno ILI '0'/'0.00' - KTC koristi 0.00 kao "nema
    vrijednosti" umjesto praznog polja."""
    if not raw or not raw.strip():
        return None
    try:
        vrijednost = float(raw.strip().replace(",", "."))
    except ValueError:
        return None
    return vrijednost if vrijednost != 0 else None


def obradi_csv(sadrzaj: str) -> list[dict]:
    proizvodi = []
    reader = csv.DictReader(io.StringIO(sadrzaj), delimiter=DELIMITER)

    for row in reader:
        barkod = (row.get("Barkod") or "").strip()
        naziv = (row.get("Naziv proizvoda") or "").strip()
        kategorija_raw = (row.get("Kategorija") or "").strip()

        redovna_cijena = parsiraj_broj(row.get("Maloprodajna cijena", ""))
        akcijska_cijena = parsiraj_broj(row.get("MPC za vrijeme posebnog oblika prodaje", ""))

        if not barkod or not naziv:
            continue
        if redovna_cijena is None and akcijska_cijena is None:
            continue

        kategorija = normaliziraj_kategoriju(kategorija_raw)
        if kategorija is None:
            continue

        if redovna_cijena is not None:
            tip = "redovno"
            cijena = redovna_cijena
            stara_cijena = None
        else:
            tip = "akcija"
            cijena = akcijska_cijena
            stara_cijena = parsiraj_broj(row.get("Najniža cijena u posljednjih 30 dana", ""))

        proizvodi.append({
            "barkod": barkod,
            "naziv": naziv,
            "cijena": cijena,
            "stara_cijena": stara_cijena,
            "trgovina": TRGOVINA,
            "grad": GRAD,
            "kategorija": kategorija,
            "tip": tip,
            "datum": datetime.now().strftime("%Y-%m-%d"),
        })

    return proizvodi


# ---------- ODABIR PROIZVODA ----------
def deterministicki_kljuc(p: dict) -> str:
    return hashlib.md5(p["barkod"].encode()).hexdigest()


def odaberi_proizvode(proizvodi: list[dict]) -> list[dict]:
    kategorije: dict[str, list[dict]] = {}
    for p in proizvodi:
        kategorije.setdefault(p["kategorija"], []).append(p)

    print("🔍 Pronađene kategorije:")
    for kat, lista in sorted(kategorije.items(), key=lambda x: -len(x[1])):
        print(f"   {kat}: {len(lista)}")

    odabrani = []

    hrana = sorted(kategorije.get("HRANA", []), key=deterministicki_kljuc)
    odabrani.extend(hrana[:KVOTA_HRANA])
    print(f"  📌 HRANA: odabrano {min(len(hrana), KVOTA_HRANA)}/{len(hrana)}")

    ostale_kat = {k: v for k, v in kategorije.items() if k != "HRANA"}
    ukupno_ostalo_dostupno = sum(len(v) for v in ostale_kat.values())

    for kat, lista in sorted(ostale_kat.items(), key=lambda x: -len(x[1])):
        udio = len(lista) / ukupno_ostalo_dostupno if ukupno_ostalo_dostupno else 0
        broj = round(udio * KVOTA_OSTALO_UKUPNO)
        broj = min(broj, len(lista))
        lista_sortirana = sorted(lista, key=deterministicki_kljuc)
        odabrani.extend(lista_sortirana[:broj])
        print(f"  📌 {kat}: odabrano {broj}/{len(lista)}")

    return odabrani


# ---------- SPREMANJE ----------
def spremi_u_firestore(proizvodi: list[dict], batch_size: int = 500) -> int:
    ukupno = len(proizvodi)
    print(f"📦 Spremanje {ukupno} proizvoda u Firestore...")
    batch = db.batch()
    brojac = 0
    for p in proizvodi:
        doc_id = f"{p['barkod']}_{p['trgovina']}_{p['grad']}".replace(" ", "_")
        doc_ref = db.collection("cijene").document(doc_id)
        data = {k: v for k, v in p.items() if v is not None}
        batch.set(doc_ref, data, merge=True)
        brojac += 1
        if brojac % batch_size == 0:
            batch.commit()
            print(f"  ✅ Spremljeno {brojac}/{ukupno}")
            batch = db.batch()
    if brojac % batch_size != 0:
        batch.commit()
        print(f"  ✅ Spremljeno {brojac}/{ukupno}")
    return brojac


# ---------- GLAVNI DIO ----------
def main():
    print("\n" + "=" * 50)
    print(f"🛒 {TRGOVINA} ({GRAD}) Automatski Scraper")
    nacin = f"LOKALNI TEST ({KVOTA_HRANA + KVOTA_OSTALO_UKUPNO} proizvoda)" if LOKALNI_TEST else f"PUNI RUN ({KVOTA_HRANA + KVOTA_OSTALO_UKUPNO} proizvoda)"
    print(f"   način rada: {nacin}")
    print("=" * 50 + "\n")

    danas = datetime.now()
    csv_url = pronadji_csv_url(danas)

    if not csv_url:
        jucer = danas - timedelta(days=1)
        print(f"⚠️ Nema cjenika za danas ({danas:%d.%m.%Y}), provjeravam jučer...")
        csv_url = pronadji_csv_url(jucer)

    if not csv_url:
        print("❌ Nije pronađen cjenik ni za danas ni za jučer!")
        return

    print(f"✅ Pronađen cjenik: {csv_url}")

    print("📥 Preuzimam CSV...")
    csv_sadrzaj = preuzmi_csv(csv_url)

    print("🔄 Obrađujem CSV...")
    proizvodi = obradi_csv(csv_sadrzaj)
    if not proizvodi:
        print("❌ Nema proizvoda za obradu!")
        return
    print(f"✅ Ukupno proizvoda u CSV-u: {len(proizvodi)}")

    odabrani = odaberi_proizvode(proizvodi)
    print(f"\n📊 Ukupno odabrano za {TRGOVINA}: {len(odabrani)}")

    if odabrani:
        spremi_u_firestore(odabrani)
        print(f"\n✅ Završeno! Upisano {len(odabrani)} dokumenata za {TRGOVINA}.")
    else:
        print("❌ Nema odabranih proizvoda, ništa nije spremljeno.")


if __name__ == "__main__":
    main()
