# -*- coding: utf-8 -*-
"""
Fiyat çekme modülü.
FON:   TEFAS fonGnlBlgSiraliGetir (mevcut çalışan script'ten alındı)
HISSE: Yahoo Finance (.IS suffix)
"""
import requests, time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

TEFAS_API_URL = "https://www.tefas.gov.tr/api/funds/fonGnlBlgSiraliGetir"
TEFAS_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/tr/fon-verileri",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    ),
}

def bugun_str():
    return str(datetime.now(ZoneInfo("Europe/Istanbul")).date())

def son_is_gunu():
    d = datetime.now(ZoneInfo("Europe/Istanbul")).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def normalize_yahoo_sembol(sembol):
    if sembol.endswith(".IS"):
        return sembol
    return f"{sembol.split('.')[0]}.IS"

# ── TEFAS ────────────────────────────────────────────────────────────────────

def fetch_tefas_fon(fon_kodu, hedef_tarih=None):
    """
    Tek bir fon için bugünkü (veya hedef_tarih) fiyatı çeker.
    Başarılıysa float, değilse None döner.
    """
    if hedef_tarih is None:
        hedef_tarih = son_is_gunu()

    bas = hedef_tarih.strftime("%Y%m%d")
    bit = hedef_tarih.strftime("%Y%m%d")

    body = {
        "fonTipi": "YAT",
        "fonKodu": fon_kodu,
        "aramaMetni": fon_kodu,
        "fonTurKod": None,
        "fonGrubu": None,
        "sfonTurKod": None,
        "fonTurAciklama": None,
        "kurucuKod": None,
        "basTarih": bas,
        "bitTarih": bit,
        "basSira": 1,
        "bitSira": 100,
        "dil": "TR",
        "sFonTurKod": "",
        "fonKod": "",
        "fonGrup": "",
        "fonUnvanTip": "",
    }

    for deneme in range(3):
        try:
            r = requests.post(
                TEFAS_API_URL,
                json=body,
                headers=TEFAS_HEADERS,
                timeout=20
            )
            if r.status_code == 429:
                time.sleep(15)
                continue
            if r.status_code != 200 or not r.text.strip():
                time.sleep(3)
                continue

            for row in r.json().get("resultList", []):
                fiyat = row.get("fiyat")
                if fiyat is not None:
                    return float(fiyat)
            return None  # Veri geldi ama boş (tatil günü vs.)
        except Exception:
            time.sleep(3)
    return None

def fetch_fon_fiyatlari(semboller):
    """Tüm fon sembollerinin bugünkü fiyatını çeker."""
    if not semboller:
        return {}, "yok"

    hedef = son_is_gunu()
    results = {}

    for sembol in semboller:
        fiyat = fetch_tefas_fon(sembol, hedef)
        if fiyat:
            results[sembol] = fiyat
        time.sleep(2)  # Rate limit koruması

    return results, "TEFAS" if results else None

# ── Borsa/ETF (Yahoo Finance) ────────────────────────────────────────────────

def fetch_hisse_fiyatlari(semboller):
    if not semboller:
        return {}, "yok"
    try:
        import yfinance as yf
        results = {}
        for sembol in semboller:
            try:
                ticker = yf.Ticker(normalize_yahoo_sembol(sembol))
                hist = ticker.history(period="5d")
                if not hist.empty:
                    fiyat = float(hist["Close"].iloc[-1])
                    if fiyat > 0:
                        results[sembol] = fiyat
            except Exception:
                pass
        return results, "Yahoo-Finance" if results else None
    except Exception as e:
        return {}, str(e)

# ── Ana Fonksiyon ─────────────────────────────────────────────────────────────

def fetch_all_prices(fon_sembolleri, hisse_sembolleri):
    today = bugun_str()
    prices, methods, errors = [], [], []

    if fon_sembolleri:
        fon_prices, fon_method = fetch_fon_fiyatlari(fon_sembolleri)
        for s, f in fon_prices.items():
            prices.append((s, today, f))
        if fon_prices:
            methods.append(f"Fon:{fon_method}")
            eksik = [s for s in fon_sembolleri if s not in fon_prices]
            if eksik:
                errors.append(f"Alınamadı:{','.join(eksik)}")
        else:
            errors.append(f"Fon alınamadı:{','.join(fon_sembolleri)}")

    if hisse_sembolleri:
        hisse_prices, hisse_method = fetch_hisse_fiyatlari(hisse_sembolleri)
        for s, f in hisse_prices.items():
            prices.append((s, today, f))
        if hisse_prices:
            methods.append(f"Hisse:{hisse_method}")
        else:
            errors.append(f"Hisse alınamadı:{','.join(hisse_sembolleri)}")

    return {
        "prices": prices,
        "method": " | ".join(methods) if methods else "başarısız",
        "errors": "; ".join(errors)
    }
