# -*- coding: utf-8 -*-
"""
Fiyat çekme modülü.
FON:   1) TEFAS BindHistoryInfo  2) TEFAS FonAnaliz sayfası scrape  3) collectapi.com
HISSE: Yahoo Finance (.IS suffix)
"""
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def bugun_str():
    return str(datetime.now(ZoneInfo("Europe/Istanbul")).date())

def son_is_gunu():
    """Bugün veya önceki son iş günü."""
    d = datetime.now(ZoneInfo("Europe/Istanbul"))
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d

def normalize_yahoo_sembol(sembol):
    if sembol.endswith(".IS"):
        return sembol
    return f"{sembol.split('.')[0]}.IS"

# ── TEFAS Yöntem 1: Resmi API ─────────────────────────────────────────────────

def fetch_tefas_api(semboller):
    """TEFAS BindHistoryInfo POST API."""
    if not semboller:
        return {}, None

    d = son_is_gunu()
    tarih_str = d.strftime("%d.%m.%Y")
    results = {}

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "tr-TR,tr;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://www.tefas.gov.tr",
        "Referer": "https://www.tefas.gov.tr/FonAnaliz.aspx",
    })

    try:
        # Cookie edinmek için önce ana sayfa
        sess.get("https://www.tefas.gov.tr/FonAnaliz.aspx", timeout=15)

        for sembol in semboller:
            try:
                r = sess.post(
                    "https://www.tefas.gov.tr/api/DB/BindHistoryInfo",
                    data={"fontip":"YAT","sfonkod":sembol,
                          "bastarih":tarih_str,"bittarih":tarih_str,"fonturkod":""},
                    timeout=15
                )
                if r.status_code == 200:
                    j = r.json()
                    lst = j.get("data", [])
                    if lst:
                        fiyat = float(lst[0].get("FIYAT", 0))
                        if fiyat > 0:
                            results[sembol] = fiyat
            except Exception:
                pass

        return results, "TEFAS-API" if results else None
    except Exception:
        return {}, None

# ── TEFAS Yöntem 2: FonAnaliz sayfası scrape ─────────────────────────────────

def fetch_tefas_scrape(semboller):
    """TEFAS FonAnaliz.aspx sayfasından fiyat parse et."""
    if not semboller:
        return {}, None
    results = {}
    try:
        for sembol in semboller:
            try:
                r = requests.get(
                    f"https://www.tefas.gov.tr/FonAnaliz.aspx?FonKod={sembol}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=15
                )
                if r.status_code == 200:
                    # Fiyat genellikle "Birim Pay Değeri" sonrası geliyor
                    import re
                    m = re.search(r'Birim Pay De[ğg]eri.*?(\d[\d\.,]+)', r.text, re.DOTALL)
                    if not m:
                        m = re.search(r'"FIYAT"\s*:\s*"?([\d\.]+)"?', r.text)
                    if m:
                        try:
                            fiyat = float(m.group(1).replace(',','.'))
                            if fiyat > 0:
                                results[sembol] = fiyat
                        except Exception:
                            pass
            except Exception:
                pass
        return results, "TEFAS-scrape" if results else None
    except Exception:
        return {}, None

# ── collectapi.com ────────────────────────────────────────────────────────────

def fetch_collectapi(semboller):
    """collectapi.com - ücretsiz tier (günde 100 istek)."""
    if not semboller:
        return {}, None
    results = {}
    try:
        for sembol in semboller:
            try:
                r = requests.get(
                    f"https://api.collectapi.com/economy/fund?fonCode={sembol}",
                    headers={
                        "authorization": "apikey 6P5SkOesMnfrqMNd3NpwXX:6gSGMiNJuSfq1rGHLIYHXq",
                        "content-type": "application/json"
                    },
                    timeout=10
                )
                if r.status_code == 200:
                    j = r.json()
                    if j.get("success") and j.get("result"):
                        fiyat = float(j["result"][0].get("price", 0))
                        if fiyat > 0:
                            results[sembol] = fiyat
            except Exception:
                pass
        return results, "collectapi.com" if results else None
    except Exception:
        return {}, None

# ── FON Ana Fonksiyon ─────────────────────────────────────────────────────────

def fetch_fon_fiyatlari(semboller):
    if not semboller:
        return {}, "yok"

    for fn, name in [(fetch_tefas_api, "TEFAS-API"),
                     (fetch_tefas_scrape, "TEFAS-scrape"),
                     (fetch_collectapi, "collectapi")]:
        results, method = fn(semboller)
        if results:
            # Eksik kalanları bir sonraki yöntemle tamamla
            eksik = [s for s in semboller if s not in results]
            if eksik:
                for fn2, _ in [(fetch_tefas_api, ""), (fetch_tefas_scrape, ""),
                               (fetch_collectapi, "")]:
                    if fn2 == fn:
                        continue
                    r2, _ = fn2(eksik)
                    results.update(r2)
                    eksik = [s for s in eksik if s not in results]
                    if not eksik:
                        break
            return results, method

    return {}, "başarısız"

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
