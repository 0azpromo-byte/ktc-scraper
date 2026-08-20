"""KTC scraper -> Firestore ("cijene" kolekcija)


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
import time
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    _ZAGREB = ZoneInfo("Europe/Zagreb")
except Exception:
    _ZAGREB = timezone(timedelta(hours=2))


import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter


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


# ---------- PROVJERA DUPLIKATA ----------
def vec_scrapano_danas(trgovina: str) -> bool:
    """Provjeri postoji li već današnji datum za ovu trgovinu u cijene kolekciji."""
    today = datetime.now().strftime("%Y-%m-%d")
    check = (
        db.collection("cijene")
        .where(filter=FieldFilter("trgovina", "==", trgovina))
        .where(filter=FieldFilter("datum", "==", today))
        .limit(1)
        .get()
    )
    return len(check) > 0


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
        barkod = (row.get("Barkod") or "").strip().strip("'\"")
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
    # Akcije imaju prioritet - uvijek su uključene, ne gube se u kvoti
    akcije = sorted((p for p in proizvodi if p.get("tip") == "akcija"), key=deterministicki_kljuc)

    kategorije: dict[str, list[dict]] = {}
    for p in proizvodi:
        if p.get("tip") != "akcija":
            kategorije.setdefault(p["kategorija"], []).append(p)

    print("🔍 Pronađene kategorije:")
    for kat, lista in sorted(kategorije.items(), key=lambda x: -len(x[1])):
        print(f"   {kat}: {len(lista)}")

    odabrani = list(akcije)
    preostala_kvota = (KVOTA_HRANA + KVOTA_OSTALO_UKUPNO) - len(odabrani)
    if preostala_kvota <= 0:
        return odabrani

    # 1. Hrana - fiksna kvota (ostatak nakon akcija)
    hrana = sorted(kategorije.get("HRANA", []), key=deterministicki_kljuc)
    broj_hrana = min(len(hrana), preostala_kvota // 2)
    odabrani.extend(hrana[:broj_hrana])
    print(f"  📌 HRANA: odabrano {broj_hrana}/{len(hrana)}")

    # 2. Ostale kategorije - proporcionalno po dostupnosti (ostatak)
    preostalo_ostalo = preostala_kvota - broj_hrana
    ostale_kat = {k: v for k, v in kategorije.items() if k != "HRANA"}
    ukupno_ostalo_dostupno = sum(len(v) for v in ostale_kat.values())

    for kat, lista in sorted(ostale_kat.items(), key=lambda x: -len(x[1])):
        udio = len(lista) / ukupno_ostalo_dostupno if ukupno_ostalo_dostupno else 0
        broj = min(round(udio * preostalo_ostalo), len(lista))
        odabrani.extend(sorted(lista, key=deterministicki_kljuc)[:broj])
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
            for attempt in range(3):
                try:
                    batch.commit()
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print(f"  ⚠️ Batch error (pokusaj {attempt+2}/3): {e}")
                    time.sleep(5)
            print(f"  ✅ Spremljeno {brojac}/{ukupno}")
            batch = db.batch()
    if brojac % batch_size != 0:
        for attempt in range(3):
            try:
                batch.commit()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  ⚠️ Batch error (pokusaj {attempt+2}/3): {e}")
                time.sleep(5)
        print(f"  ✅ Spremljeno {brojac}/{ukupno}")
    return brojac


# ---------- PADOVI CIJENA ----------
def detektiraj_i_spremi_padove(db, trgovina, grad, novi_proizvodi, batch_size=500):
    danas = datetime.now().strftime("%Y-%m-%d")
    print(f"  [price_drop] Provjeravam padove cijena za {trgovina} ({grad})...")
    doc_refs = []
    for p in novi_proizvodi:
        doc_id = f"{p['barkod']}_{trgovina}_{grad}".replace(" ", "_")
        doc_refs.append(db.collection("cijene").document(doc_id))
    postojeca = {}
    snapshots = db.get_all(doc_refs)
    for snap in snapshots:
        if snap.exists:
            data = snap.to_dict()
            postojeca[data.get("barkod", "")] = data
    padovi = []
    for p in novi_proizvodi:
        barkod = p["barkod"]
        if barkod not in postojeca:
            continue
        stara = postojeca[barkod].get("cijena")
        nova = p.get("cijena")
        if stara is None or nova is None:
            continue
        if stara > nova and p.get("tip") == "redovno":
            postotak = round((stara - nova) / stara * 100, 1)
            padovi.append({"barkod": barkod, "naziv": p.get("naziv", ""), "trgovina": trgovina, "grad": grad, "kategorija": p.get("kategorija"), "cijena_stara": stara, "cijena_nova": nova, "postotak": postotak, "datum": danas, "tip_pada": "redovno"})
    if not padovi:
        print(f"  [price_drop] Nema padova cijena za {trgovina}.")
        return []
    batch = db.batch()
    brojac = 0
    for pad in padovi:
        doc_id = f"{pad['barkod']}_{pad['trgovina']}_{pad['datum']}".replace(" ", "_")
        batch.set(db.collection("price_drops").document(doc_id), pad)
        brojac += 1
        if brojac % batch_size == 0:
            batch.commit()
            batch = db.batch()
    if brojac % batch_size != 0:
        batch.commit()
    print(f"  [price_drop] ✅ Pronađeno {len(padovi)} padova cijena za {trgovina}.")
    return padovi


# ---------- GLAVNI DIO ----------
def main():
    # Limit: ne scrapaj prije 08:00 po hrvatskom vremenu
    zagreb_sad = datetime.now(_ZAGREB)
    if zagreb_sad.hour < 8:
        print(f"⏰ Još nije 08:00 po hrvatskom vremenu ({zagreb_sad:%H:%M}). Preskačem scrapanje.")
        return

    print("\n" + "=" * 50)
    print(f"🛒 {TRGOVINA} ({GRAD}) Automatski Scraper")
    nacin = f"LOKALNI TEST ({KVOTA_HRANA + KVOTA_OSTALO_UKUPNO} proizvoda)" if LOKALNI_TEST else f"PUNI RUN ({KVOTA_HRANA + KVOTA_OSTALO_UKUPNO} proizvoda)"
    print(f"   način rada: {nacin}")
    print("=" * 50 + "\n")

    # Provjera duplikata - preskoči ako je već scrapano danas
    if vec_scrapano_danas(TRGOVINA):
        print(f"⏭️ {TRGOVINA} je već scrapano danas ({datetime.now():%Y-%m-%d}). Preskačem.")
        return

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
        detektiraj_i_spremi_padove(db, TRGOVINA, GRAD, odabrani)
        spremi_u_firestore(odabrani)
        print(f"\n✅ Završeno! Upisano {len(odabrani)} dokumenata za {TRGOVINA}.")
    else:
        print("❌ Nema odabranih proizvoda, ništa nije spremljeno.")


if __name__ == "__main__":
    main()
