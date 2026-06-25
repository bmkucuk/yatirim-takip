# -*- coding: utf-8 -*-
"""
Fiyat çekme modülü.
FON: 1) TEFAS BindHistoryInfo API  2) collectapi.com  3) fonbul.com scrape
HISSE/ETF: Yahoo Finance (.IS suffix)
"""
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def bugun_str():
    return str(datetime.now(ZoneInfo("Europe/Istanbul")).date())

def normalize_yahoo_sembol(sembol):
    if sembol.endswith(".IS"):
        return sembol
    base = sembol.split(".")[0]
    return f"{base}.IS"

# ── TEFAS Yöntem 1: BindHistoryInfo ──────────────────────────────────────────

def fetch_tefas_v1(semboller):
    """TEFAS resmi API - tam session ile."""
    if not semboller:
        return {}, None

    today = datetime.now(ZoneInfo("Europe/Istanbul"))
    # Hafta sonu ise önceki cuma'ya git
    if today.weekday() >= 5:
        delta = today.weekday() - 4
        today = today - timedelta(days=delta)
    tarih_str = today.strftime("%d.%m.%Y")

    results = {}
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.tefas.gov.tr/FonAnaliz.aspx",
        "Origin": "https://www.tefas.gov.tr",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })

    try:
        # Cookie al
        sess.get("https://www.tefas.gov.tr/FonAnaliz.aspx", timeout=15)

        for sembol in semboller:
            try:
                r = sess.post(
                    "https://www.tefas.gov.tr/api/DB/BindHistoryInfo",
                    data={
                        "fontip": "YAT",
                        "sfonkod": sembol,
                        "bastarih": tarih_str,
                        "bittarih": tarih_str,
                        "fonturkod": ""
                    },
                    timeout=15
                )
                if r.status_code == 200:
                    try:
                        j = r.json()
                        data_list = j.get("data", [])
                        if data_list:
                            fiyat = float(data_list[0].get("FIYAT", 0))
                            if fiyat > 0:
                                results[sembol] = fiyat
                    except Exception:
                        pass
            except Exception:
                pass

        return results, "TEFAS" if results else None
    except Exception as e:
        return {}, str(e)

# ── TEFAS Yöntem 2: fonbul.com ────────────────────────────────────────────────

def fetch_fonbul(semboller):
    """fonbul.com JSON API."""
    if not semboller:
        return {}, None
    results = {}
    try:
        for sembol in semboller:
            try:
                r = requests.get(
                    f"https://www.fonbul.com/api/fund/{sembol}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10
                )
                if r.status_code == 200:
                    j = r.json()
                    fiyat = float(j.get("price", j.get("nav", j.get("lastPrice", 0))))
                    if fiyat > 0:
                        results[sembol] = fiyat
            except Exception:
                pass
        return results, "fonbul.com" if results else None
    except Exception as e:
        return {}, str(e)

# ── TEFAS Yöntem 3: fintables ─────────────────────────────────────────────────

def fetch_fintables(semboller):
    """fintables.com API."""
    if not semboller:
        return {}, None
    results = {}
    try:
        for sembol in semboller:
            try:
                r = requests.get(
                    f"https://api.fintables.com/funds/{sembol}/nav/",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=10
                )
                if r.status_code == 200:
                    j = r.json()
                    if isinstance(j, list) and j:
                        fiyat = float(j[-1].get("price", 0))
                        if fiyat > 0:
                            results[sembol] = fiyat
                    elif isinstance(j, dict):
                        fiyat = float(j.get("nav", j.get("price", 0)))
                        if fiyat > 0:
                            results[sembol] = fiyat
            except Exception:
                pass
        return results, "fintables.com" if results else None
    except Exception as e:
        return {}, str(e)

# ── FON Ana Fonksiyon ─────────────────────────────────────────────────────────

def fetch_fon_fiyatlari(semboller):
    if not semboller:
        return {}, "yok"

    # Yöntem 1: TEFAS
    results, method = fetch_tefas_v1(semboller)
    if results:
        eksik = [s for s in semboller if s not in results]
        if eksik:
            r2, _ = fetch_fonbul(eksik)
            results.update(r2)
            if eksik2 := [s for s in eksik if s not in results]:
                r3, _ = fetch_fintables(eksik2)
                results.update(r3)
        return results, "TEFAS"

    # Yöntem 2: fonbul
    results, method = fetch_fonbul(semboller)
    if results:
        return results, "fonbul.com"

    # Yöntem 3: fintables
    results, method = fetch_fintables(semboller)
    if results:
        return results, "fintables.com"

    return {}, "başarısız"

# ── Borsa/ETF (Yahoo Finance) ────────────────────────────────────────────────

def fetch_hisse_fiyatlari(semboller):
    if not semboller:
        return {}, "yok"
    try:
        import yfinance as yf
        results = {}
        for sembol in semboller:
            yahoo_sembol = normalize_yahoo_sembol(sembol)
            try:
                ticker = yf.Ticker(yahoo_sembol)
                hist = ticker.history(period="5d")
                if not hist.empty:
                    fiyat = float(hist["Close"].iloc[-1])
                    if fiyat > 0:
                        results[sembol] = fiyat
            except Exception:
                pass
        return results, "Yahoo-Finance" if results else None
    except ImportError:
        return {}, "yfinance-kurulu-değil"
    except Exception as e:
        return {}, str(e)

# ── Ana Fonksiyon ─────────────────────────────────────────────────────────────

def fetch_all_prices(fon_sembolleri, hisse_sembolleri):
    today = bugun_str()
    prices = []
    methods = []
    errors = []

    if fon_sembolleri:
        fon_prices, fon_method = fetch_fon_fiyatlari(fon_sembolleri)
        for sembol, fiyat in fon_prices.items():
            prices.append((sembol, today, fiyat))
        if fon_prices:
            methods.append(f"Fon: {fon_method}")
        else:
            errors.append(f"Fon alınamadı: {', '.join(fon_sembolleri)}")

    if hisse_sembolleri:
        hisse_prices, hisse_method = fetch_hisse_fiyatlari(hisse_sembolleri)
        for sembol, fiyat in hisse_prices.items():
            prices.append((sembol, today, fiyat))
        if hisse_prices:
            methods.append(f"Hisse: {hisse_method}")
        else:
            errors.append(f"Hisse alınamadı: {', '.join(hisse_sembolleri)}")

    return {
        "prices": prices,
        "method": " | ".join(methods) if methods else "başarısız",
        "errors": "; ".join(errors)
    }
