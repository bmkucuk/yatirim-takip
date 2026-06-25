# -*- coding: utf-8 -*-
import requests, time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

TEFAS_URL = "https://www.tefas.gov.tr/api/funds/fonGnlBlgSiraliGetir"
HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": "https://www.tefas.gov.tr",
    "Referer": "https://www.tefas.gov.tr/tr/fon-verileri",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
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

def tefas_aralik_cek(fon_kodu, baslangic, bitis):
    """{date: fiyat} dict döndürür. 28 günlük parçalara böler."""
    sonuc = {}
    bas = baslangic
    while bas <= bitis:
        bit = min(bas + timedelta(days=27), bitis)
        body = {
            "fonTipi": "YAT", "fonKodu": fon_kodu,
            "aramaMetni": fon_kodu, "fonTurKod": None,
            "fonGrubu": None, "sfonTurKod": None,
            "fonTurAciklama": None, "kurucuKod": None,
            "basTarih": bas.strftime("%Y%m%d"),
            "bitTarih": bit.strftime("%Y%m%d"),
            "basSira": 1, "bitSira": 100000,
            "dil": "TR", "sFonTurKod": "",
            "fonKod": "", "fonGrup": "", "fonUnvanTip": "",
        }
        for _ in range(3):
            try:
                r = requests.post(TEFAS_URL, json=body, headers=HEADERS, timeout=20)
                if r.status_code == 429:
                    time.sleep(15)
                    continue
                r.raise_for_status()
                if not r.text.strip():
                    time.sleep(2)
                    continue
                for row in r.json().get("resultList", []):
                    tarih_val = row.get("tarih", "")
                    fiyat = row.get("fiyat")
                    if not tarih_val or fiyat is None:
                        continue
                    try:
                        t = datetime.strptime(str(tarih_val)[:10], "%Y-%m-%d").date()
                        sonuc[t] = float(fiyat)
                    except Exception:
                        continue
                break
            except Exception:
                time.sleep(3)
        bas = bit + timedelta(days=1)
        time.sleep(2)
    return sonuc

def fetch_tefas_fon(fon_kodu, hedef_tarih=None):
    """Tek gün fiyatı — backfill debug için."""
    if hedef_tarih is None:
        hedef_tarih = son_is_gunu()
    veriler = tefas_aralik_cek(fon_kodu, hedef_tarih, hedef_tarih)
    return veriler.get(hedef_tarih)

def fetch_fon_fiyatlari(semboller):
    """Bugünkü fiyatları çek."""
    if not semboller:
        return {}, "yok"
    hedef = son_is_gunu()
    results = {}
    for sembol in semboller:
        veriler = tefas_aralik_cek(sembol, hedef, hedef)
        if veriler:
            results[sembol] = list(veriler.values())[0]
        time.sleep(2)
    return results, "TEFAS" if results else None

def fetch_fon_aralik(semboller, baslangic, bitis):
    """
    Birden fazla fon için tarih aralığındaki tüm fiyatları çek.
    Döndürür: {(sembol, tarih_str): fiyat}
    """
    tum = {}
    for sembol in semboller:
        veriler = tefas_aralik_cek(sembol, baslangic, bitis)
        for gun, fiyat in veriler.items():
            tum[(sembol, str(gun))] = fiyat
        time.sleep(2)
    return tum

def fetch_hisse_fiyatlari(semboller):
    """Yahoo Finance direkt HTTP ile BIST ve ABD hisse fiyatları."""
    if not semboller:
        return {}, "yok"
    import requests as req
    results = {}
    for sembol in semboller:
        # BIST için .IS ekle, ABD için olduğu gibi kullan
        yahoo_sembol = f"{sembol}.IS" if not sembol.endswith(".IS") and len(sembol) <= 6 and sembol.isalpha() else sembol
        try:
            r = req.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sembol}"
                f"?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                for c in reversed(closes):
                    if c is not None:
                        results[sembol] = round(float(c), 4)
                        break
        except Exception:
            pass
    return results, "Yahoo-Finance" if results else None

def fetch_all_prices(fon_sembolleri, hisse_sembolleri):
    today = bugun_str()
    prices, methods, errors = [], [], []
    if fon_sembolleri:
        fon_prices, fon_method = fetch_fon_fiyatlari(fon_sembolleri)
        for s, f in fon_prices.items():
            prices.append((s, today, f))
        if fon_prices:
            methods.append(f"Fon:{fon_method}")
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
