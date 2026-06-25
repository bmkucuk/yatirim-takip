# -*- coding: utf-8 -*-
"""
Fiyat çekme modülü.
FON: 1) TEFAS direkt HTTP  2) fintables.com
HISSE/ETF: Yahoo Finance (.IS suffix, .F gibi suffix'ler temizlenir)
"""
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

def bugun_str():
    return str(datetime.now(ZoneInfo("Europe/Istanbul")).date())

def normalize_yahoo_sembol(sembol):
    """
    BIST sembollerini Yahoo Finance formatına çevir.
    ZPX30.F → ZPX30.IS
    THYAO   → THYAO.IS
    THYAO.IS → THYAO.IS (değişmez)
    """
    # Zaten .IS ile bitiyorsa dokunma
    if sembol.endswith(".IS"):
        return sembol
    # .F, .E, .N gibi BIST market suffix'lerini temizle
    base = sembol.split(".")[0]
    return f"{base}.IS"

# ── TEFAS ────────────────────────────────────────────────────────────────────

def fetch_tefas_direct(semboller):
    """Yöntem 1: TEFAS resmi API'ye cookie ile POST."""
    if not semboller:
        return {}, None

    today = datetime.now(ZoneInfo("Europe/Istanbul"))
    tarih_str = today.strftime("%d.%m.%Y")
    results = {}

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.tefas.gov.tr/",
        "Origin": "https://www.tefas.gov.tr",
        "Content-Type": "application/x-www-form-urlencoded",
    })

    try:
        sess.get("https://www.tefas.gov.tr/BorsaYatirimFonu.aspx", timeout=10)
        for sembol in semboller:
            try:
                r = sess.post(
                    "https://www.tefas.gov.tr/api/DB/BindHistoryInfo",
                    data={"fontip": "YAT", "sfonkod": sembol,
                          "bastarih": tarih_str, "bittarih": tarih_str, "fonturkod": ""},
                    timeout=10
                )
                if r.status_code == 200:
                    j = r.json()
                    data_list = j.get("data", [])
                    if data_list:
                        fiyat = float(data_list[0].get("FIYAT", 0))
                        if fiyat > 0:
                            results[sembol] = fiyat
            except Exception:
                pass
        return results, "TEFAS-direkt" if results else None
    except Exception as e:
        return {}, str(e)

def fetch_fintables(semboller):
    """Yöntem 2: fintables.com API."""
    if not semboller:
        return {}, None

    results = {}
    try:
        for sembol in semboller:
            try:
                r = requests.get(
                    f"https://api.fintables.com/funds/{sembol}/nav/",
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

def fetch_fon_fiyatlari(semboller):
    """TEFAS fon fiyatlarını sırayla dener."""
    if not semboller:
        return {}, "yok"

    results, method = fetch_tefas_direct(semboller)
    if results:
        # Başarısız olanlar için fintables dene
        eksik = [s for s in semboller if s not in results]
        if eksik:
            r2, _ = fetch_fintables(eksik)
            results.update(r2)
        return results, method

    results, method = fetch_fintables(semboller)
    if results:
        return results, method

    return {}, "başarısız-manuel-giriş-gerekli"

# ── Borsa/ETF (Yahoo Finance) ────────────────────────────────────────────────

def fetch_hisse_fiyatlari(semboller):
    """
    Yahoo Finance ile BIST hisse ve ETF fiyatlarını çek.
    .F, .E gibi market suffix'leri otomatik .IS'e çevrilir.
    """
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
                        results[sembol] = fiyat  # Orijinal sembolle kaydet
            except Exception:
                pass

        return results, "Yahoo-Finance" if results else None
    except ImportError:
        return {}, "yfinance-kurulu-değil"
    except Exception as e:
        return {}, str(e)

# ── Ana Fonksiyon ─────────────────────────────────────────────────────────────

def fetch_all_prices(fon_sembolleri, hisse_sembolleri):
    """
    Tüm sembollerin güncel fiyatlarını çeker.
    Döndürür: {"prices": [(sembol, tarih, fiyat), ...], "method": str, "errors": str}
    """
    today = bugun_str()
    prices = []
    methods = []
    errors = []

    if fon_sembolleri:
        fon_prices, fon_method = fetch_fon_fiyatlari(fon_sembolleri)
        for sembol, fiyat in fon_prices.items():
            prices.append((sembol, today, fiyat))
        if fon_method and fon_method != "başarısız-manuel-giriş-gerekli":
            methods.append(f"Fon: {fon_method}")
        else:
            errors.append(f"Fon fiyatı alınamadı: {', '.join(fon_sembolleri)}")

    if hisse_sembolleri:
        hisse_prices, hisse_method = fetch_hisse_fiyatlari(hisse_sembolleri)
        for sembol, fiyat in hisse_prices.items():
            prices.append((sembol, today, fiyat))
        if hisse_method:
            methods.append(f"Hisse: {hisse_method}")
        else:
            failed = [s for s in hisse_sembolleri if s not in hisse_prices]
            errors.append(f"Hisse fiyatı alınamadı: {', '.join(failed)}")

    return {
        "prices": prices,
        "method": " | ".join(methods) if methods else "başarısız",
        "errors": "; ".join(errors)
    }
