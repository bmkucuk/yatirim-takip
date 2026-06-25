# -*- coding: utf-8 -*-
"""
Fiyat çekme modülü.
Sıra: 1) TEFAS direkt HTTP  2) fintables.com  3) Manuel buton (boş sonuç döner)
Borsa: Yahoo Finance (yfinance) - BIST için .IS suffix
"""
import requests
from datetime import date, datetime
from zoneinfo import ZoneInfo

def bugun_str():
    return str(datetime.now(ZoneInfo("Europe/Istanbul")).date())

# ── TEFAS ────────────────────────────────────────────────────────────────────

def fetch_tefas_direct(semboller):
    """
    Yöntem 1: TEFAS resmi API'ye cookie olmadan POST.
    Başarılı olursa {sembol: fiyat} döner.
    """
    if not semboller:
        return {}, None
    
    today = datetime.now(ZoneInfo("Europe/Istanbul"))
    tarih_str = today.strftime("%d.%m.%Y")
    
    results = {}
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.tefas.gov.tr/",
        "Origin": "https://www.tefas.gov.tr",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    
    try:
        # Önce ana sayfayı ziyaret et (cookie almak için)
        session.get("https://www.tefas.gov.tr/BorsaYatirimFonu.aspx", timeout=10)
        
        for sembol in semboller:
            data = {
                "fontip": "YAT",
                "sfonkod": sembol,
                "bastarih": tarih_str,
                "bittarih": tarih_str,
                "fonturkod": ""
            }
            r = session.post(
                "https://www.tefas.gov.tr/api/DB/BindHistoryInfo",
                data=data,
                timeout=10
            )
            if r.status_code == 200:
                j = r.json()
                data_list = j.get("data", [])
                if data_list:
                    fiyat = float(data_list[0].get("FIYAT", 0))
                    if fiyat > 0:
                        results[sembol] = fiyat
        
        return results, "TEFAS-direkt" if results else None
    except Exception as e:
        return {}, str(e)

def fetch_fintables(semboller):
    """
    Yöntem 2: fintables.com API (ücretsiz, kayıt gerektirmez).
    """
    if not semboller:
        return {}, None
    
    results = {}
    today_str = bugun_str()
    
    try:
        for sembol in semboller:
            url = f"https://api.fintables.com/funds/{sembol}/nav/"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                j = r.json()
                # En son fiyatı al
                if isinstance(j, list) and j:
                    fiyat = float(j[-1].get("price", 0))
                    if fiyat > 0:
                        results[sembol] = fiyat
                elif isinstance(j, dict):
                    fiyat = float(j.get("nav", j.get("price", 0)))
                    if fiyat > 0:
                        results[sembol] = fiyat
        
        return results, "fintables.com" if results else None
    except Exception as e:
        return {}, str(e)

def fetch_fon_fiyatlari(semboller):
    """TEFAS fon fiyatlarını sırayla dener."""
    if not semboller:
        return {}, "yok"
    
    # Yöntem 1
    results, method = fetch_tefas_direct(semboller)
    if results:
        return results, method
    
    # Yöntem 2
    results, method = fetch_fintables(semboller)
    if results:
        return results, method
    
    # Yöntem 3: Başarısız
    return {}, "başarısız-manuel-giriş-gerekli"

# ── Borsa (Yahoo Finance) ────────────────────────────────────────────────────

def fetch_hisse_fiyatlari(semboller):
    """
    Yahoo Finance ile BIST hisse fiyatlarını çek.
    BIST sembollerine otomatik .IS ekler.
    """
    if not semboller:
        return {}, "yok"
    
    try:
        import yfinance as yf
        results = {}
        
        for sembol in semboller:
            # .IS suffix ekle (BIST için Yahoo Finance formatı)
            yahoo_sembol = sembol if sembol.endswith(".IS") else f"{sembol}.IS"
            
            try:
                ticker = yf.Ticker(yahoo_sembol)
                hist = ticker.history(period="2d")
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
    """
    Tüm sembollerin fiyatlarını çeker.
    Döndürür: {"prices": [(sembol, tarih, fiyat), ...], "method": str, "errors": str}
    """
    today = bugun_str()
    prices = []
    methods = []
    errors = []
    
    # Fonlar
    if fon_sembolleri:
        fon_prices, fon_method = fetch_fon_fiyatlari(fon_sembolleri)
        for sembol, fiyat in fon_prices.items():
            prices.append((sembol, today, fiyat))
        if fon_method:
            methods.append(f"Fon: {fon_method}")
        else:
            errors.append(f"Fon fiyatı alınamadı: {fon_sembolleri}")
    
    # Hisseler
    if hisse_sembolleri:
        hisse_prices, hisse_method = fetch_hisse_fiyatlari(hisse_sembolleri)
        for sembol, fiyat in hisse_prices.items():
            prices.append((sembol, today, fiyat))
        if hisse_method:
            methods.append(f"Hisse: {hisse_method}")
        else:
            errors.append(f"Hisse fiyatı alınamadı: {hisse_sembolleri}")
    
    return {
        "prices": prices,
        "method": " | ".join(methods) if methods else "başarısız",
        "errors": "; ".join(errors)
    }
