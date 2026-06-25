# -*- coding: utf-8 -*-
"""
Sembol listelerini çeker ve DB'ye kaydeder.
TEFAS fonları + BIST hisseleri + ABD hisseleri (S&P500 + NASDAQ100)
"""
import requests, sqlite3, os, time

DB_PATH = os.environ.get("DB_PATH", "/data/yatirim.db")

def init_sembol_tablosu(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS semboller (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kod TEXT NOT NULL,
            ad TEXT,
            tur TEXT NOT NULL,
            piyasa TEXT NOT NULL,
            UNIQUE(kod, piyasa)
        )
    """)
    conn.commit()
    conn.close()

def cek_tefas_fonlari():
    """TEFAS'tan tüm YAT tipi fonları çeker."""
    url = "https://www.tefas.gov.tr/api/funds/fonGnlBlgSiraliGetir"
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.tefas.gov.tr",
        "Referer": "https://www.tefas.gov.tr/tr/fon-verileri",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36",
    }
    body = {
        "fonTipi": "YAT", "fonKodu": "", "aramaMetni": "",
        "fonTurKod": None, "fonGrubu": None, "sfonTurKod": None,
        "fonTurAciklama": None, "kurucuKod": None,
        "basTarih": None, "bitTarih": None,
        "basSira": 1, "bitSira": 99999,
        "dil": "TR", "sFonTurKod": "", "fonKod": "", "fonGrup": "", "fonUnvanTip": "",
    }
    try:
        r = requests.post(url, json=body, headers=headers, timeout=30)
        if r.status_code == 200:
            fonlar = []
            for row in r.json().get("resultList", []):
                kod = row.get("fonKodu") or row.get("kod")
                ad = row.get("fonUnvani") or row.get("ad") or ""
                if kod:
                    fonlar.append((kod.strip(), ad.strip(), "FON", "TEFAS"))
            return fonlar
    except Exception as e:
        print(f"TEFAS hata: {e}")
    return []

def cek_bist_hisseleri():
    """İş Yatırım API'sinden BIST hisselerini çeker."""
    try:
        r = requests.get(
            "https://www.isyatirim.com.tr/api/data/symbol?market=BIST",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20
        )
        if r.status_code == 200:
            data = r.json()
            hisseler = []
            for item in data:
                kod = item.get("symbol") or item.get("kod")
                ad = item.get("description") or item.get("ad") or ""
                if kod:
                    hisseler.append((kod.strip(), ad.strip(), "HISSE", "BIST"))
            return hisseler
    except Exception:
        pass

    # Fallback: Yahoo Finance BIST listesi
    try:
        # En bilinen BIST hisseleri
        bist_kodlar = [
            ("THYAO","Türk Hava Yolları"), ("GARAN","Garanti BBVA"), ("AKBNK","Akbank"),
            ("YKBNK","Yapı Kredi"), ("ISCTR","İş Bankası C"), ("HALKB","Halkbank"),
            ("VAKBN","Vakıfbank"), ("SAHOL","Sabancı Holding"), ("KCHOL","Koç Holding"),
            ("SISE","Şişecam"), ("EREGL","Ereğli Demir Çelik"), ("BIMAS","BİM"),
            ("MIGROS","Migros"), ("TCELL","Turkcell"), ("TUPRS","Tüpraş"),
            ("TOASO","Tofaş Oto"), ("FROTO","Ford Otosan"), ("ASELS","Aselsan"),
            ("PGSUS","Pegasus"), ("TAVHL","TAV Havalimanları"), ("SASA","SASA"),
            ("EKGYO","Emlak Konut"), ("TTKOM","Türk Telekom"), ("ARCLK","Arçelik"),
            ("VESTL","Vestel"), ("OTKAR","Otokar"), ("KOZAL","Koza Altın"),
            ("KRDMD","Kardemir D"), ("PETKM","Petkim"), ("DOHOL","Doğan Holding"),
        ]
        return [(k, a, "HISSE", "BIST") for k, a in bist_kodlar]
    except Exception:
        return []

def cek_abd_hisseleri():
    """S&P 500 + NASDAQ 100 sembollerini Wikipedia'dan çeker."""
    semboller = []
    try:
        # S&P 500
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20
        )
        if r.status_code == 200:
            import re
            tickers = re.findall(r'<td><a[^>]+>([A-Z]{1,5})</a>', r.text)
            # Wikipedia'dan şirket adları
            names = re.findall(r'<td><a[^>]+href="/wiki/[^"]*"[^>]*>([^<]+)</a></td>', r.text)
            for i, ticker in enumerate(tickers[:505]):
                ad = names[i] if i < len(names) else ""
                semboller.append((ticker, ad, "HISSE", "ABD"))
    except Exception as e:
        print(f"SP500 hata: {e}")

    try:
        # NASDAQ 100 ekle
        nasdaq100 = [
            ("AAPL","Apple"), ("MSFT","Microsoft"), ("NVDA","NVIDIA"), ("AMZN","Amazon"),
            ("META","Meta"), ("GOOGL","Alphabet A"), ("GOOG","Alphabet C"), ("TSLA","Tesla"),
            ("AVGO","Broadcom"), ("COST","Costco"), ("NFLX","Netflix"), ("AMD","AMD"),
            ("ADBE","Adobe"), ("QCOM","Qualcomm"), ("INTC","Intel"), ("INTU","Intuit"),
            ("AMAT","Applied Materials"), ("AMGN","Amgen"), ("MU","Micron"), ("ISRG","Intuitive Surgical"),
            ("LRCX","Lam Research"), ("KLAC","KLA Corp"), ("PANW","Palo Alto"), ("SNPS","Synopsys"),
            ("CDNS","Cadence"), ("REGN","Regeneron"), ("MELI","MercadoLibre"), ("CRWD","CrowdStrike"),
            ("PYPL","PayPal"), ("ABNB","Airbnb"), ("ORLY","O'Reilly Auto"), ("MNST","Monster Beverage"),
        ]
        existing = {s[0] for s in semboller}
        for kod, ad in nasdaq100:
            if kod not in existing:
                semboller.append((kod, ad, "HISSE", "ABD"))
    except Exception:
        pass

    return semboller

def cek_abd_etfleri():
    """Popüler ABD ETF'leri."""
    etfler = [
        # Sektör ETF'leri
        ("SMH","VanEck Semiconductor ETF"), ("SOXX","iShares Semiconductor ETF"),
        ("XLK","Technology Select Sector SPDR"), ("QQQ","Invesco NASDAQ 100 ETF"),
        ("SPY","SPDR S&P 500 ETF"), ("IVV","iShares Core S&P 500 ETF"),
        ("VOO","Vanguard S&P 500 ETF"), ("VTI","Vanguard Total Stock Market ETF"),
        ("ARKK","ARK Innovation ETF"), ("ARKG","ARK Genomic Revolution ETF"),
        ("ARKW","ARK Next Generation Internet ETF"), ("ARKF","ARK Fintech Innovation ETF"),
        ("XLF","Financial Select Sector SPDR"), ("XLE","Energy Select Sector SPDR"),
        ("XLV","Health Care Select Sector SPDR"), ("XLI","Industrial Select Sector SPDR"),
        ("XLP","Consumer Staples Select Sector SPDR"), ("XLY","Consumer Discretionary SPDR"),
        ("XLU","Utilities Select Sector SPDR"), ("XLRE","Real Estate Select Sector SPDR"),
        ("XLB","Materials Select Sector SPDR"), ("XLC","Communication Services SPDR"),
        # Altın & Emtia
        ("GLD","SPDR Gold Shares"), ("IAU","iShares Gold Trust"),
        ("SLV","iShares Silver Trust"), ("USO","United States Oil Fund"),
        # Tahvil
        ("TLT","iShares 20+ Year Treasury Bond ETF"), ("IEF","iShares 7-10 Year Treasury ETF"),
        ("HYG","iShares iBoxx High Yield Corporate Bond ETF"),
        # Kaldıraçlı
        ("TQQQ","ProShares UltraPro QQQ"), ("SQQQ","ProShares UltraPro Short QQQ"),
        ("SPXL","Direxion Daily S&P 500 Bull 3X"), ("SPXS","Direxion Daily S&P 500 Bear 3X"),
        ("SOXL","Direxion Daily Semiconductor Bull 3X"), ("SOXS","Direxion Daily Semiconductor Bear 3X"),
        # Diğer popüler
        ("VNQ","Vanguard Real Estate ETF"), ("EEM","iShares MSCI Emerging Markets ETF"),
        ("EFA","iShares MSCI EAFE ETF"), ("AGG","iShares Core U.S. Aggregate Bond ETF"),
        ("IBIT","iShares Bitcoin Trust"), ("FBTC","Fidelity Wise Origin Bitcoin Fund"),
        ("BITO","ProShares Bitcoin ETF"), ("MCHI","iShares MSCI China ETF"),
        ("KWEB","KraneShares CSI China Internet ETF"), ("FXI","iShares China Large-Cap ETF"),
    ]
    return [(k, a, "HISSE", "ABD") for k, a in etfler]

    if db_path is None:
        db_path = DB_PATH
    
    init_sembol_tablosu(db_path)
    conn = sqlite3.connect(db_path)
    
    toplam = 0
    
    print("TEFAS fonları çekiliyor...")
    fonlar = cek_tefas_fonlari()
    print(f"  {len(fonlar)} fon bulundu")
    for item in fonlar:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()
    
    time.sleep(2)
    
    print("BIST hisseleri çekiliyor...")
    bist = cek_bist_hisseleri()
    print(f"  {len(bist)} hisse bulundu")
    for item in bist:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()
    
    print("ABD ETF'leri ekleniyor...")
    etfler = cek_abd_etfleri()
    for item in etfler:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()

    print("ABD hisseleri çekiliyor...")
    abd = cek_abd_hisseleri()
    print(f"  {len(abd)} hisse bulundu")
    for item in abd:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()
    conn.close()
    
    return toplam

if __name__ == "__main__":
    n = sembolleri_guncelle("/data/yatirim.db")
    print(f"Toplam {n} sembol güncellendi.")

def sembolleri_guncelle(db_path=None):
    if db_path is None:
        db_path = DB_PATH

    init_sembol_tablosu(db_path)
    conn = sqlite3.connect(db_path)
    toplam = 0

    print("ABD ETF'leri ekleniyor...")
    etfler = cek_abd_etfleri()
    for item in etfler:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()

    print("TEFAS fonları çekiliyor...")
    fonlar = cek_tefas_fonlari()
    print(f"  {len(fonlar)} fon bulundu")
    for item in fonlar:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()
    time.sleep(2)

    print("BIST hisseleri çekiliyor...")
    bist = cek_bist_hisseleri()
    print(f"  {len(bist)} hisse bulundu")
    for item in bist:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()

    print("ABD hisseleri çekiliyor...")
    abd = cek_abd_hisseleri()
    print(f"  {len(abd)} hisse bulundu")
    for item in abd:
        conn.execute("INSERT OR REPLACE INTO semboller (kod,ad,tur,piyasa) VALUES (?,?,?,?)", item)
        toplam += 1
    conn.commit()
    conn.close()

    return toplam

if __name__ == "__main__":
    n = sembolleri_guncelle("/data/yatirim.db")
    print(f"Toplam {n} sembol güncellendi.")
