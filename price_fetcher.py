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

import os as _os

# TEFAS'ın kendi API'si dakikada 6 istekle sınırlı (429 ERR-224 "Throttling limit").
# Uygulama gunicorn'da 2 worker (2 ayrı process) ile çalışıyor — process-içi bir kilit
# yeterli değil, workerlar birbirinden habersiz aynı anda istek gönderip TEFAS'ı
# throttle'a düşürebiliyordu. Bu yüzden DB ile aynı kalıcı diskte, dosya kilidiyle
# (fcntl.flock) process'ler arası paylaşılan bir zaman damgası kullanıyoruz.
_TEFAS_MIN_ARALIK_SN = 11.0
_tefas_son_istek_zamani = [0.0]  # fallback: dosya kilidi başarısız olursa process-local
_tefas_lock_dizin = _os.path.dirname(_os.environ.get("DB_PATH", "")) or "/tmp"
_TEFAS_LOCK_PATH = _os.path.join(_tefas_lock_dizin, "tefas_rate.lock")

def _tefas_hiz_sinirla():
    try:
        import fcntl
        with open(_TEFAS_LOCK_PATH, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                icerik = f.read().strip()
                son_zaman = float(icerik) if icerik else 0.0
                simdi = time.time()
                beklenecek = _TEFAS_MIN_ARALIK_SN - (simdi - son_zaman)
                if beklenecek > 0:
                    time.sleep(beklenecek)
                f.seek(0)
                f.truncate()
                f.write(str(time.time()))
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except Exception:
        # Dosya kilidi bir sebeple çalışmazsa (izin, disk vb.) process-local'a düş —
        # tek worker'da hâlâ doğru çalışır, çok worker'da en kötü ihtimalle eski davranış.
        simdi = time.monotonic()
        beklenecek = _TEFAS_MIN_ARALIK_SN - (simdi - _tefas_son_istek_zamani[0])
        if beklenecek > 0:
            time.sleep(beklenecek)
        _tefas_son_istek_zamani[0] = time.monotonic()

def _tefas_nokta_fiyat(fon_kodu, hedef_tarih, pencere_gun=4, timeout=8, deneme=2):
    """Belirli bir tarihe yakın (±pencere_gun) TEK bir fiyat noktası çeker — tam
    yıllık backfill yapmadan getiri hesaplamak için hafif/hızlı bir sorgu.
    429 (throttle) veya geçici hatalarda birkaç kez tekrar dener (aksi halde tek
    bir başarısız istek o veri noktasını kalıcı olarak '—' bırakıyordu)."""
    bas = hedef_tarih - timedelta(days=pencere_gun)
    bit = hedef_tarih + timedelta(days=pencere_gun)
    bugun = son_is_gunu()
    if bit > bugun:
        bit = bugun
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
    for deneme_no in range(deneme):
        try:
            _tefas_hiz_sinirla()
            r = requests.post(TEFAS_URL, json=body, headers=HEADERS, timeout=timeout)
            if r.status_code == 429:
                time.sleep(5)  # throttle'a düştük, kısa ek bekleme sonrası tekrar dene
                continue
            if r.status_code != 200 or not r.text.strip():
                continue
            gunler = {}
            for row in r.json().get("resultList", []):
                tarih_val = row.get("tarih", "")
                fiyat = row.get("fiyat")
                if not tarih_val or fiyat is None:
                    continue
                try:
                    t = datetime.strptime(str(tarih_val)[:10], "%Y-%m-%d").date()
                    gunler[t] = float(fiyat)
                except Exception:
                    continue
            if not gunler:
                continue
            en_yakin = min(gunler.keys(), key=lambda d: abs((d - hedef_tarih).days))
            return gunler[en_yakin]
        except Exception:
            continue
    return None


def fon_getiri_hesapla(fon_kodu):
    """Fonun TEFAS'taki güncel fiyatına göre son 1 hafta/1/3/6 ay, 1 yıl ve
    yılbaşından bugüne getirisini hesaplar (fon-detayli-analiz sayfasındaki
    'Getiri Bilgisi' paneliyle aynı mantık).
    Her nokta ayrı, küçük pencereli, hızlı bir sorgu ile çekilir — toplam istek
    sayısı sabit (7) ve her biri kısa timeout'lu, bu yüzden yavaş/kilitleyici değildir.
    Döner: {"son_fiyat", "son_tarih", "getiri_hafta", "getiri_1ay", "getiri_3ay",
    "getiri_6ay", "getiri_1yil", "getiri_yilbasi"} veya fon bulunamazsa None.
    """
    bugun = son_is_gunu()
    son = _tefas_nokta_fiyat(fon_kodu, bugun, pencere_gun=10, deneme=3)
    if son is None:
        return None
    son_fiyat = son

    sonuc = {"son_fiyat": son_fiyat, "son_tarih": str(bugun)}
    pencereler = {
        "getiri_hafta": 7, "getiri_1ay": 30, "getiri_3ay": 90,
        "getiri_6ay": 180, "getiri_1yil": 365,
    }
    for etiket, gun in pencereler.items():
        eski_fiyat = _tefas_nokta_fiyat(fon_kodu, bugun - timedelta(days=gun))
        sonuc[etiket] = round((son_fiyat / eski_fiyat - 1) * 100, 2) if eski_fiyat else None

    yilbasi_tarihi = date(bugun.year, 1, 1)
    eski_fiyat_yb = _tefas_nokta_fiyat(fon_kodu, yilbasi_tarihi)
    sonuc["getiri_yilbasi"] = round((son_fiyat / eski_fiyat_yb - 1) * 100, 2) if eski_fiyat_yb else None
    return sonuc


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
                _tefas_hiz_sinirla()
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
    Yahoo, art arda hızlı isteklerde bazı sembolleri sessizce reddedebiliyor (429/boş yanıt);
    bunu azaltmak için istekler arasına kısa bekleme koyuyoruz ve bir kez retry deniyoruz.
    Döner: (results, kaynak_etiketi, basarisiz_semboller)
    """
    if not semboller:
        return {}, "yok", []
    import requests as req
    results = {}
    basarisiz = []
    for i, sembol in enumerate(semboller):
        if i > 0:
            time.sleep(0.6)  # Yahoo'yu art arda hızlı istekle boğmayalım
        tur = (tur_map or {}).get(sembol, "")
        if tur == "BIST":
            yahoo_sembol = f"{sembol}.IS" if not sembol.endswith(".IS") else sembol
        elif tur == "ABD":
            yahoo_sembol = sembol
        else:
            # tur bilinmiyorsa eski mantık
            yahoo_sembol = f"{sembol}.IS" if not sembol.endswith(".IS") and len(sembol) <= 6 and sembol.isalpha() else sembol

        basarili_mi = False
        for deneme in range(2):  # 1 orijinal + 1 retry
            if deneme > 0:
                time.sleep(2)
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
                    for ts, c in zip(timestamps, closes):
                        if c is not None:
                            from datetime import datetime as _dt
                            tarih = _dt.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                            if sembol not in results:
                                results[sembol] = {}
                            results[sembol][tarih] = round(float(c), 4)
                    basarili_mi = True
                    break
            except Exception:
                pass
        if not basarili_mi:
            basarisiz.append(sembol)
    return results, ("Yahoo-Finance" if results else None), basarisiz

def fetch_hisse_detay_toplu(semboller):
    """Yahoo v7/finance/quote ile toplu anlık fiyat + günlük değişim (%) + şirket adı — tek istekte.
    Döndürür: {sembol: {"fiyat": float, "degisim": float|None, "isim": str|None}}
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
                isim = item.get("longName") or item.get("shortName") or item.get("displayName")
                if fiyat is not None and ys in sembol_map:
                    sonuc[sembol_map[ys]] = {
                        "fiyat": round(float(fiyat), 4),
                        "degisim": round(float(degisim), 2) if degisim is not None else None,
                        "isim": isim,
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
                    sonuc[kod] = {"fiyat": fiyat, "degisim": degisim, "isim": None}
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


def _yahoo_chart_fiyat(sembol):
    """v8 chart endpoint'inden (get_usd_try ile aynı, üretimde çalıştığı doğrulanmış yöntem)
    son fiyatı ve bir önceki kapanışa göre günlük değişim yüzdesini döndürür.
    Döner: (fiyat, degisim_yuzde) ya da (None, None)."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sembol}?interval=1d&range=5d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )
        if r.status_code == 200:
            result = r.json()["chart"]["result"][0]
            meta = result.get("meta", {})
            closes = [c for c in result.get("indicators", {}).get("quote", [{}])[0].get("close", []) if c is not None]
            son = meta.get("regularMarketPrice")
            if son is None:
                son = closes[-1] if closes else None
            if son is None:
                return None, None
            son = float(son)
            onceki = closes[-2] if len(closes) >= 2 else (meta.get("previousClose") or meta.get("chartPreviousClose"))
            degisim = round((son / onceki - 1) * 100, 2) if onceki else None
            return round(son, 4), degisim
    except Exception:
        pass
    return None, None


def fetch_milliyet_altin():
    """uzmanpara.milliyet.com.tr/altin-fiyatlari sayfasından Gram Altın, Ons Altın (USD),
    gümüş verilerini VE sayfanın üst kısmındaki BIST100/Dolar/Euro/Petrol ticker çubuğunu
    çeker (tek istekte, ekstra HTTP çağrısı yapmadan).
    Döner: {"GRAM_ALTIN","ONS_ALTIN","GUMUS_GRAM_TL","GUMUS_ONS_USD": {"alis","satis","degisim"},
            "BIST100","USDTRY","EURTRY","BRENT": {"deger","degisim"}}
    """
    import re
    from bs4 import BeautifulSoup

    sonuc = {}
    try:
        r = requests.get(
            "https://uzmanpara.milliyet.com.tr/altin-fiyatlari/",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
            timeout=10
        )
        if r.status_code != 200:
            return sonuc
        r.encoding = r.apparent_encoding or "utf-8"  # sayfa header'ı yanlış encoding bildiriyor, aksi halde Türkçe karakterler bozuluyor
        soup = BeautifulSoup(r.text, "html.parser")
        sayi_re = re.compile(r"^-?[\d\.]+,\d+$")
        etiket_map = {
            "Gram Altın": "GRAM_ALTIN",
            "Ons Altın": "ONS_ALTIN",
            "Gümüş Gram (TL)": "GUMUS_GRAM_TL",
            "Gümüş Ons (Dolar)": "GUMUS_ONS_USD",
        }
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            # tds[0] simge/ikon hücresi (boş), gerçek etiket tds[1]'de; değerler tds[2:]'de
            etiket = tds[1].get_text(strip=True)
            anahtar = etiket_map.get(etiket)
            if not anahtar or anahtar in sonuc:
                continue
            sayilar, yuzde = [], None
            for td in tds[2:]:
                v = td.get_text(strip=True)
                v_temiz = v.replace("$", "").replace("TL", "").replace("₺", "").strip()
                if sayi_re.match(v_temiz):
                    sayilar.append(_tl_sayi(v_temiz))
                elif "%" in v:
                    m = re.search(r"-?[\d\.]+,\d+|-?\d+", v.replace("%", ""))
                    if m:
                        yuzde = _tl_sayi(m.group()) if "," in m.group() else float(m.group())
            if len(sayilar) >= 2:
                sonuc[anahtar] = {"alis": sayilar[0], "satis": sayilar[1], "degisim": yuzde}

        # Üst ticker çubuğu: "BIST100 14.827 0,00%", "DOLAR 46,4473 -0,01%" gibi <a> etiketleri
        ticker_map = {"BIST100": "BIST100", "DOLAR": "USDTRY", "EURO": "EURTRY", "PETROL": "BRENT"}
        ticker_re = re.compile(r"^\S+\s+([\d\.]+(?:,\d+)?)\s+(-?[\d\.]+(?:,\d+)?)%")
        for a in soup.find_all("a"):
            metin = a.get_text(" ", strip=True)
            if not metin:
                continue
            ilk_kelime = metin.split(" ", 1)[0].upper()
            anahtar = ticker_map.get(ilk_kelime)
            if not anahtar or anahtar in sonuc:
                continue
            m = ticker_re.match(metin)
            if m:
                sonuc[anahtar] = {"deger": _tl_sayi(m.group(1)), "degisim": _tl_sayi(m.group(2))}
    except Exception:
        pass
    return sonuc


def fetch_altin_s1_milliyet():
    """ALTIN.S1 sertifikasını Milliyet'in BIST 'Tüm Hisseler' (harf bazlı) sayfasından çeker.
    Sayfa TİCKER koduna değil ŞİRKET ADINA göre alfabetik sıralı ('Darphane Altın Sertifikası'
    → 'D' harfi), bu yüzden ticker'ın kendi ilk harfi olan 'A' değil önce 'D' denenir.
    Döner: {"fiyat","degisim"} ya da None.
    """
    import re
    from bs4 import BeautifulSoup

    sayi_re = re.compile(r"^-?[\d\.]+,\d+$")
    for harf in ("D", "A"):
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
                if not a or a.get_text(strip=True).upper() != "ALTINS1":
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
                if fiyat is not None:
                    return {"fiyat": fiyat, "degisim": degisim}
        except Exception:
            continue
    return None


def fetch_altin_s1_doviz():
    """ALTIN.S1 sertifikasını doviz.com'dan çeker. Milliyet'in 'Tüm Hisseler' sayfası bu
    sertifikayı hiç listelemiyor olabilir (Emtia Pazarı enstrümanı), bu yüzden güvenilir
    bir yedek/öncelikli kaynak olarak doviz.com kullanılıyor. Sayfanın meta açıklamasından
    ('ALTINS1 hissesinin fiyatı 70,65 liradır. Önceki kapanış fiyatına göre %-0,20 düşmüştür.')
    fiyat ve günlük değişim çekilir. Döner: {"fiyat","degisim"} ya da None.
    """
    import re

    try:
        r = requests.get(
            "https://borsa.doviz.com/hisseler/altins1-darphane-altin-sertifikasi",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
            timeout=10
        )
        if r.status_code != 200:
            return None
        r.encoding = r.apparent_encoding or "utf-8"
        m = re.search(r"ALTINS1 hissesinin fiyat[ıi] ([\d\.,]+) liradır\.\s*Önceki kapanış fiyat[ıi]na göre %([\-\d,]+)", r.text)
        if not m:
            return None
        fiyat = _tl_sayi(m.group(1))
        degisim = _tl_sayi(m.group(2))
        if fiyat is None:
            return None
        return {"fiyat": fiyat, "degisim": degisim}
    except Exception:
        return None


def fetch_altin_s1():
    """ALTIN.S1 sertifikası: önce Milliyet BIST hisse sayfası, bulunamazsa doviz.com."""
    return fetch_altin_s1_milliyet() or fetch_altin_s1_doviz()


def _tv_scan_grup(screener, tickerlar):
    """TradingView'in herkese açık (resmi olmayan) scanner API'sinden birden fazla
    sembolü TEK istekte çeker (aynı screener grubu için). Bkz. scanner.tradingview.com —
    sitenin kendi hisse/döviz tarama widget'larının kullandığı, kimlik doğrulaması
    gerektirmeyen genel uç nokta. Döner: {ticker: (fiyat, degisim_yuzde)}.
    """
    sonuc = {}
    try:
        r = requests.post(
            f"https://scanner.tradingview.com/{screener}/scan",
            json={"symbols": {"tickers": tickerlar, "query": {"types": []}}, "columns": ["close", "change"]},
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
            timeout=8,
        )
        if r.status_code != 200:
            return sonuc
        for satir in (r.json().get("data") or []):
            d = satir.get("d") or []
            if len(d) < 2 or d[0] is None:
                continue
            close = float(d[0])
            degisim = round(float(d[1]), 2) if d[1] is not None else None
            sonuc[satir["s"]] = (round(close, 4), degisim)
    except Exception:
        pass
    return sonuc


# kod -> (screener, "EXCHANGE:SEMBOL", görünen ad)
_TV_KAYNAK = {
    "USD":       ("forex",   "FX_IDC:USDTRY",  "Dolar/TL"),
    "EUR":       ("forex",   "FX_IDC:EURTRY",  "Euro/TL"),
    "XAUSD":     ("cfd",     "OANDA:XAUUSD",   "Altın (Ons/USD)"),
    "XAGUSD":    ("cfd",     "OANDA:XAGUSD",   "Gümüş (Ons/USD)"),
    "GRAMALTIN": ("cfd",     "FX_IDC:XAUTRYG", "Gram Altın"),
    "IAU":       ("america", "AMEX:IAU",       "iShares Gold Trust (IAU)"),
    "ALTINS1":   ("turkey",  "BIST:ALTIN",     "Darphane Altın Sertifikası (ALTIN.S1)"),
    "BIST100":   ("turkey",  "BIST:XU100",     "BIST 100"),
    "PETROL":    ("futures", "ICEEUR:BRN1!",   "Brent Petrol (Varil/USD)"),
    # XAUSD/XAGUSD: ilk denemede "forex" ekranında FX_IDC:XAUUSD/XAGUSD boş dönmüştü.
    # Kullanıcının kendi TradingView ekranında bu sembollerin "OANDA" borsası ve
    # "Commodity · Cfd" kategorisinde olduğu görüldü — o yüzden "cfd" ekranında
    # OANDA: önekiyle tekrar denendi, çalıştı. GRAMALTIN (FX_IDC:XAUTRYG) için aynı
    # netlikte ekran doğrulaması yok — XAUUSD/XAGUSD ile aynı mantıkla "cfd" ekranında
    # deneniyor, ama Milliyet güvenilir fallback olarak kalıyor.
    # Not: scanner API'nin tam "teknik analiz" (Recommend.* vb.) hesaplaması endekslerde
    # çalışmıyor, ama burada sadece close/change çektiğimiz için XU100 sorunsuz geliyor.
}


def fetch_piyasa_verileri_tradingview():
    """Piyasalar kartlarını doğrudan TradingView'den çekmeye çalışır. Aynı screener
    grubundaki semboller (ör. tüm forex çiftleri) tek istekte toplanır — kod başına
    ayrı istek atılmaz. Eksik/başarısız kalemler sözlükte yer almaz; çağıran taraf
    (fetch_piyasa_verileri) bunlar için Yahoo/Milliyet'e düşer.
    Döner: {kod: {"fiyat","degisim","ad"}}.
    """
    gruplar = {}
    for kod, (screener, ticker, ad) in _TV_KAYNAK.items():
        gruplar.setdefault(screener, []).append((kod, ticker, ad))

    sonuc = {}
    for screener, kalemler in gruplar.items():
        veriler = _tv_scan_grup(screener, [t for _, t, _ in kalemler])
        for kod, ticker, ad in kalemler:
            if ticker in veriler:
                fiyat, degisim = veriler[ticker]
                sonuc[kod] = {"fiyat": fiyat, "degisim": degisim, "ad": ad}
    return sonuc


def fetch_piyasa_verileri():
    """'Piyasalar' sekmesi için altın/gümüş/döviz/endeks verilerini çeker.
    BIST100, ALTIN.S1, Dolar/TL, Euro/TL için TradingView (scanner API) birincil kaynak.
    Ons Altın/Gümüş ve Brent Petrol için Milliyet birincil — Yahoo'nun bu üçündeki
    sembolleri (GC=F/SI=F/BZ=F) VADELİ İŞLEM sözleşmesi, spot'tan yapısal olarak
    sapabiliyor (doğrulandı: TradingView/Fintables karşılaştırması), bu yüzden Yahoo
    artık sadece Milliyet başarısız olursa son çare. IAU (gerçek pay senedi, vade farkı
    yok) için Yahoo birincil. Her kalem kendi başarısız olduğu adımda bir sonraki
    kaynağa düşer.
    Döner: {anahtar: {"fiyat","degisim","ad", ...}} —
    XAUSD, IAU, GRAMALTIN, XAGUSD, ALTINS1, BIST100, USD, EUR, PETROL, MAKAS.
    """
    tv = fetch_piyasa_verileri_tradingview()
    milliyet = fetch_milliyet_altin()

    # Ons Altın, Gümüş Ons ve Brent Petrol: TradingView başarısız olursa Yahoo Finance,
    # o da olmazsa Milliyet'in altin-fiyatlari sayfası (en geç güncellenen, son çare).
    ham = {}
    fiyat, degisim = _yahoo_chart_fiyat("IAU")
    if fiyat is not None:
        ham["IAU"] = {"fiyat": fiyat, "degisim": degisim, "ad": "iShares Gold Trust (IAU)"}
    fiyat, degisim = _yahoo_chart_fiyat("GC=F")
    if fiyat is not None:
        ham["XAUUSD"] = {"fiyat": fiyat, "degisim": degisim, "ad": "Altın (Ons/USD, Vadeli)"}
    fiyat, degisim = _yahoo_chart_fiyat("SI=F")
    if fiyat is not None:
        ham["XAGUSD"] = {"fiyat": fiyat, "degisim": degisim, "ad": "Gümüş (Ons/USD, Vadeli)"}
    fiyat, degisim = _yahoo_chart_fiyat("BZ=F")
    if fiyat is not None:
        ham["BRENT"] = {"fiyat": fiyat, "degisim": degisim, "ad": "Brent Petrol (Varil/USD)"}

    piyasalar = {}

    # XAUSD: TradingView (OANDA:XAUUSD, cfd) ÖNCELİKLİ — Milliyet'in bu enstrümandaki
    # yüzde değişim alanı bazen fiyattan bağımsız/yanlış çıkabiliyor (bkz. Gümüş'te
    # görülen -0,09% vs gerçek +1,46% farkı). TradingView bulunamazsa Milliyet, o da
    # olmazsa Yahoo'nun "GC=F" (COMEX vadeli — spot'tan ~$30-40 sapabilir) son çare.
    if "XAUSD" in tv:
        piyasalar["XAUSD"] = tv["XAUSD"]
    elif "ONS_ALTIN" in milliyet:
        piyasalar["XAUSD"] = {"fiyat": milliyet["ONS_ALTIN"]["satis"], "degisim": milliyet["ONS_ALTIN"]["degisim"], "ad": "Altın (Ons/USD)"}
    elif "XAUUSD" in ham:
        piyasalar["XAUSD"] = {"fiyat": ham["XAUUSD"]["fiyat"], "degisim": ham["XAUUSD"]["degisim"], "ad": "Altın (Ons/USD)"}

    if "IAU" in ham:
        piyasalar["IAU"] = ham["IAU"]
    elif "IAU" in tv:
        piyasalar["IAU"] = tv["IAU"]

    # XAGUSD: TradingView (OANDA:XAGUSD, cfd) ÖNCELİKLİ — asıl sorunu tetikleyen kart.
    # Milliyet'in "Gümüş Ons (Dolar)" satırındaki yüzde değişim yanlış/bayat geliyordu
    # (fiyat doğruyken değişim tersti). Bulunamazsa Milliyet, o da olmazsa Yahoo (SI=F).
    if "XAGUSD" in tv:
        piyasalar["XAGUSD"] = tv["XAGUSD"]
    elif "GUMUS_ONS_USD" in milliyet:
        piyasalar["XAGUSD"] = {"fiyat": milliyet["GUMUS_ONS_USD"]["satis"], "degisim": milliyet["GUMUS_ONS_USD"]["degisim"], "ad": "Gümüş (Ons/USD)"}
    elif "XAGUSD" in ham:
        piyasalar["XAGUSD"] = {"fiyat": ham["XAGUSD"]["fiyat"], "degisim": ham["XAGUSD"]["degisim"], "ad": "Gümüş (Ons/USD)"}

    # Gram altın (TRY): TradingView (FX_IDC:XAUTRYG) ÖNCELİKLİ → Milliyet'in gerçek
    # piyasa fiyatı (satış) → Yahoo'dan hesapla (son çare).
    gram_fiyat = gram_degisim = None
    if "GRAMALTIN" in tv:
        gram_fiyat = tv["GRAMALTIN"]["fiyat"]
        gram_degisim = tv["GRAMALTIN"]["degisim"]
    elif "GRAM_ALTIN" in milliyet:
        gram_fiyat = milliyet["GRAM_ALTIN"]["satis"]
        gram_degisim = milliyet["GRAM_ALTIN"]["degisim"]
    elif "XAUUSD" in ham:
        usd_try, _ = _yahoo_chart_fiyat("USDTRY=X")
        if usd_try:
            gram_fiyat = round(ham["XAUUSD"]["fiyat"] * usd_try / 31.1034768, 2)
            gram_degisim = ham["XAUUSD"].get("degisim")

    if gram_fiyat is not None:
        piyasalar["GRAMALTIN"] = {"fiyat": gram_fiyat, "degisim": gram_degisim, "ad": "Gram Altın"}

    # ALTIN.S1 sertifikası: TradingView (BIST:ALTIN) TUTARLI ÇIKTI (BIST enstrümanları
    # anonim scanner'da gecikmesiz görünüyor) → Milliyet BIST hisse sayfası → doviz.com.
    # 1 lot = 0.01gr altın, dolayısıyla lot fiyatı x100 = gram karşılığı.
    if "ALTINS1" in tv:
        altin_s1 = {"fiyat": tv["ALTINS1"]["fiyat"], "degisim": tv["ALTINS1"]["degisim"]}
    else:
        altin_s1 = fetch_altin_s1()
    if altin_s1 and altin_s1.get("fiyat"):
        sertifika_gram = round(altin_s1["fiyat"] * 100, 2)
        piyasalar["ALTINS1"] = {
            "fiyat": altin_s1["fiyat"],
            "degisim": altin_s1.get("degisim"),
            "ad": "Darphane Altın Sertifikası (ALTIN.S1)",
            "fiyat_gram": sertifika_gram,
        }
        if gram_fiyat:
            makas = round((sertifika_gram / gram_fiyat - 1) * 100, 2)
            piyasalar["MAKAS"] = {
                "deger": makas,
                "sertifika_gram": sertifika_gram,
                "gram_altin": gram_fiyat,
            }

    # BIST100 endeksi: TradingView (BIST:XU100) — aynı sebeple burada da birincil kaynak.
    if "BIST100" in tv:
        piyasalar["BIST100"] = tv["BIST100"]
    elif "BIST100" in milliyet:
        piyasalar["BIST100"] = {"fiyat": milliyet["BIST100"]["deger"], "degisim": milliyet["BIST100"]["degisim"], "ad": "BIST 100"}

    # Dolar/Euro: Milliyet → TradingView
    if "USDTRY" in milliyet:
        piyasalar["USD"] = {"fiyat": milliyet["USDTRY"]["deger"], "degisim": milliyet["USDTRY"]["degisim"], "ad": "Dolar/TL"}
    elif "USD" in tv:
        piyasalar["USD"] = tv["USD"]
    if "EURTRY" in milliyet:
        piyasalar["EUR"] = {"fiyat": milliyet["EURTRY"]["deger"], "degisim": milliyet["EURTRY"]["degisim"], "ad": "Euro/TL"}
    elif "EUR" in tv:
        piyasalar["EUR"] = tv["EUR"]

    # Brent Petrol: TradingView (ICEEUR:BRN1! — ICE Brent Crude Futures, "Brent Petrol"
    # denince herkesin kastettiği asıl referans) öncelikli → Milliyet → Yahoo (BZ=F) son çare.
    if "PETROL" in tv:
        piyasalar["PETROL"] = tv["PETROL"]
    elif "BRENT" in milliyet:
        piyasalar["PETROL"] = {"fiyat": milliyet["BRENT"]["deger"], "degisim": milliyet["BRENT"]["degisim"], "ad": "Brent Petrol (Varil/USD)"}
    elif "BRENT" in ham:
        piyasalar["PETROL"] = {"fiyat": ham["BRENT"]["fiyat"], "degisim": ham["BRENT"]["degisim"], "ad": "Brent Petrol (Varil/USD)"}

    return piyasalar


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
        hisse_prices, hisse_method, hisse_basarisiz = fetch_hisse_fiyatlari(hisse_sembolleri, tur_map=tur_map)
        for s, gun_dict in hisse_prices.items():
            for tarih, fiyat in gun_dict.items():
                prices.append((s, tarih, fiyat))
        if hisse_prices:
            methods.append(f"Hisse:{hisse_method}")
        else:
            errors.append(f"Hisse alınamadı:{','.join(hisse_sembolleri)}")
        if hisse_basarisiz:
            errors.append(f"Bazı hisseler çekilemedi:{','.join(hisse_basarisiz)}")
    return {
        "prices": prices,
        "method": " | ".join(methods) if methods else "başarısız",
        "errors": "; ".join(errors)
    }
