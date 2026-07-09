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
                    time.sleep(1)
                    continue
                r.raise_for_status()
                if not r.text.strip():
                    time.sleep(1)
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
                time.sleep(1)
        bas = bit + timedelta(days=1)
        time.sleep(1)
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
        time.sleep(1)
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
        time.sleep(1)
    return tum

def fetch_hisse_toplu(semboller, tur_map=None):
    """Yahoo v7/finance/quote ile toplu anlık fiyat çek — tek istekte tüm semboller."""
    if not semboller:
        return {}
    import requests as req
    yahoo_semboller = []
    sembol_map = {}  # yahoo_sembol → orijinal sembol
    for sembol in semboller:
        tur = (tur_map or {}).get(sembol, "BIST")
        if tur == "ABD":
            ys = sembol
        else:
            ys = f"{sembol}.IS" if not sembol.endswith(".IS") else sembol
        yahoo_semboller.append(ys)
        sembol_map[ys] = sembol
    try:
        r = req.get(
            f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={','.join(yahoo_semboller)}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            results = {}
            for item in data.get("quoteResponse", {}).get("result", []):
                ys = item.get("symbol", "")
                fiyat = item.get("regularMarketPrice")
                if fiyat and ys in sembol_map:
                    results[sembol_map[ys]] = round(float(fiyat), 4)
            return results
    except Exception:
        pass
    return {}


def fetch_hisse_fiyatlari(semboller, tur_map=None):
    """Yahoo Finance direkt HTTP ile BIST ve ABD hisse fiyatları.
    tur_map: {sembol: tur} — tur bilgisi varsa BIST için .IS ekle, ABD için ekleme.
    """
    if not semboller:
        return {}, "yok"
    import requests as req
    results = {}
    for sembol in semboller:
        tur = (tur_map or {}).get(sembol, "")
        if tur == "BIST":
            yahoo_sembol = f"{sembol}.IS" if not sembol.endswith(".IS") else sembol
        elif tur == "ABD":
            yahoo_sembol = sembol
        else:
            # tur bilinmiyorsa eski mantık
            yahoo_sembol = f"{sembol}.IS" if not sembol.endswith(".IS") and len(sembol) <= 6 and sembol.isalpha() else sembol
        try:
            r = req.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sembol}"
                f"?interval=1d&range=30d",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                result = data["chart"]["result"][0]
                timestamps = result.get("timestamp", [])
                closes = result["indicators"]["quote"][0]["close"]
                # Tüm günleri kaydet
                for ts, c in zip(timestamps, closes):
                    if c is not None:
                        from datetime import datetime as _dt
                        tarih = _dt.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                        if sembol not in results:
                            results[sembol] = {}
                        results[sembol][tarih] = round(float(c), 4)
        except Exception:
            pass
    return results, "Yahoo-Finance" if results else None

def fetch_hisse_detay_toplu(semboller):
    """Yahoo v7/finance/quote ile toplu anlık fiyat + günlük değişim (%) — tek istekte.
    Döndürür: {sembol: {"fiyat": float, "degisim": float|None}}
    """
    if not semboller:
        return {}
    yahoo_semboller = []
    sembol_map = {}
    for sembol in semboller:
        ys = f"{sembol}.IS" if not sembol.endswith(".IS") else sembol
        yahoo_semboller.append(ys)
        sembol_map[ys] = sembol
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={','.join(yahoo_semboller)}",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=15
        )
        if r.status_code == 200:
            data = r.json()
            sonuc = {}
            for item in data.get("quoteResponse", {}).get("result", []):
                ys = item.get("symbol", "")
                fiyat = item.get("regularMarketPrice")
                degisim = item.get("regularMarketChangePercent")
                if fiyat is not None and ys in sembol_map:
                    sonuc[sembol_map[ys]] = {
                        "fiyat": round(float(fiyat), 4),
                        "degisim": round(float(degisim), 2) if degisim is not None else None,
                    }
            return sonuc
    except Exception:
        pass
    return {}


def _tl_sayi(s):
    """'1.343,00' veya '-3,93' gibi TR formatlı sayıyı float'a çevir."""
    s = s.strip()
    try:
        return float(s.replace(".", "").replace(",", "."))
    except Exception:
        return None


def fetch_milliyet_fiyatlar(semboller):
    """uzmanpara.milliyet.com.tr 'Tüm Hisseler' sayfasından (harf bazlı) fiyat + günlük değişim çek.
    Yahoo'da bulunamayan sembolleri tamamlamak için fallback olarak kullanılır.
    Döndürür: {sembol: {"fiyat": float, "degisim": float}}
    """
    import re
    from bs4 import BeautifulSoup

    hedef = {s.upper() for s in semboller if s}
    if not hedef:
        return {}
    harfler = sorted({s[0] for s in hedef})
    sayi_re = re.compile(r"^-?[\d\.]+,\d+$")
    sonuc = {}
    for harf in harfler:
        try:
            url = f"https://uzmanpara.milliyet.com.tr/canli-borsa/bist-TUM-hisseleri/?Harf={harf}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for tr in soup.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 4:
                    continue
                a = tds[0].find("a")
                if not a:
                    continue
                kod = a.get_text(strip=True).upper()
                if kod not in hedef or kod in sonuc:
                    continue
                degerler = [td.get_text(strip=True) for td in tds[1:]]
                fiyat = degisim = None
                for v in degerler:
                    if sayi_re.match(v):
                        if fiyat is None:
                            fiyat = _tl_sayi(v)
                        elif degisim is None:
                            degisim = _tl_sayi(v)
                            break
                if fiyat is not None and degisim is not None:
                    sonuc[kod] = {"fiyat": fiyat, "degisim": degisim}
        except Exception:
            continue
        time.sleep(0.2)
    return sonuc


def fetch_fon_icerik_fiyatlari(semboller):
    """Fon içerik analizi için: önce Yahoo (tek istek, hızlı), eksik kalanlar için
    Milliyet Uzmanpara (harf bazlı) fallback. Döndürür: {sembol: {"fiyat", "degisim"}}
    """
    semboller = list(dict.fromkeys(semboller))  # sırayı koru, tekilleştir
    sonuc = fetch_hisse_detay_toplu(semboller)
    eksikler = [s for s in semboller if s not in sonuc or sonuc[s].get("degisim") is None]
    if eksikler:
        milliyet = fetch_milliyet_fiyatlar(eksikler)
        for k, v in milliyet.items():
            sonuc[k] = v
    return sonuc


def fetch_all_prices(fon_sembolleri, hisse_sembolleri, tur_map=None):
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
        hisse_prices, hisse_method = fetch_hisse_fiyatlari(hisse_sembolleri, tur_map=tur_map)
        for s, gun_dict in hisse_prices.items():
            for tarih, fiyat in gun_dict.items():
                prices.append((s, tarih, fiyat))
        if hisse_prices:
            methods.append(f"Hisse:{hisse_method}")
        else:
            errors.append(f"Hisse alınamadı:{','.join(hisse_sembolleri)}")
    return {
        "prices": prices,
        "method": " | ".join(methods) if methods else "başarısız",
        "errors": "; ".join(errors)
    }
