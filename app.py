# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from functools import wraps
import sqlite3, os, hashlib, secrets
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from price_fetcher import fetch_all_prices

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB_PATH = os.environ.get("DB_PATH", "/data/yatirim.db")

def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            email TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS hesaplar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ad TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS aracilar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ad TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS islemler (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            sembol TEXT NOT NULL,
            tur TEXT NOT NULL,
            hesap TEXT,
            araciKurum TEXT,
            alissat TEXT NOT NULL,
            adet REAL NOT NULL,
            fiyat REAL NOT NULL,
            tutar REAL NOT NULL,
            tarih TEXT NOT NULL,
            para_birimi TEXT DEFAULT 'TRY',
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS fiyat_gecmisi (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sembol TEXT NOT NULL,
            tarih TEXT NOT NULL,
            fiyat REAL NOT NULL,
            UNIQUE(sembol, tarih)
        );
        CREATE TABLE IF NOT EXISTS price_fetch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tarih TEXT,
            sonuc TEXT,
            detay TEXT
        );
        CREATE TABLE IF NOT EXISTS semboller (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kod TEXT NOT NULL,
            ad TEXT,
            tur TEXT NOT NULL,
            piyasa TEXT NOT NULL,
            UNIQUE(kod, piyasa)
        );
        CREATE TABLE IF NOT EXISTS kiyaslama_global_tarih (
            user_id INTEGER PRIMARY KEY,
            ilk_tarih TEXT NOT NULL DEFAULT '',
            son_tarih TEXT NOT NULL DEFAULT '',
            toplam_para REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS nakit_bakiye (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            para_birimi TEXT NOT NULL,
            tutar REAL NOT NULL DEFAULT 0,
            UNIQUE(user_id, para_birimi)
        );
        CREATE TABLE IF NOT EXISTS kiyaslama_portfoy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ad TEXT NOT NULL,
            ilk_tarih TEXT NOT NULL,
            son_tarih TEXT NOT NULL,
            toplam_para REAL NOT NULL,
            sira INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS kiyaslama_kalem (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfoy_id INTEGER NOT NULL,
            sembol TEXT NOT NULL,
            agirlik REAL NOT NULL,
            ilk_fiyat REAL,
            son_fiyat REAL,
            vergi_orani REAL DEFAULT 0,
            FOREIGN KEY(portfoy_id) REFERENCES kiyaslama_portfoy(id)
        );
        """)
    # Migration: mevcut tablolara eksik kolonları ekle
    try:
        conn.execute("ALTER TABLE kiyaslama_kalem ADD COLUMN vergi_orani REAL DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE kiyaslama_global_tarih ADD COLUMN toplam_para REAL DEFAULT 0")
    except Exception:
        pass


def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def bugun():
    return datetime.now(ZoneInfo("Europe/Istanbul")).date()

# Vergi muaf fonlar (PHE altın fonu - stopaj yok)
VERGISIZ_FONLAR = {"PHE"}
VERGI_ORANI = 0.175  # %17.5

def net_kar(sembol, tur, kar_zarar):
    """Vergi sonrası net kar hesapla."""
    if tur == "BIST":
        return kar_zarar  # BIST'te stopaj yok
    if tur == "ABD":
        return None  # Değişken vergi, sonradan ödeniyor
    if tur != "FON":
        return kar_zarar
    if sembol in VERGISIZ_FONLAR:
        return kar_zarar  # Vergisiz
    if kar_zarar > 0:
        return kar_zarar * (1 - VERGI_ORANI)
    return kar_zarar

def get_usd_try():
    """Güncel USD/TRY kurunu Yahoo Finance'den çek."""
    try:
        import requests as req
        r = req.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/USDTRY=X?interval=1d&range=2d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        if r.status_code == 200:
            closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            for c in reversed(closes):
                if c:
                    return round(float(c), 4)
    except Exception:
        pass
    return None


    return datetime.now(ZoneInfo("Europe/Istanbul")).date()

# ── Getiri Hesaplama ──────────────────────────────────────────────────────────

def get_fiyat(sembol, tarih_str):
    """Verilen tarih veya öncesindeki en yakın fiyatı döndür."""
    with get_db() as conn:
        row = conn.execute("""
            SELECT fiyat FROM fiyat_gecmisi
            WHERE sembol=? AND tarih<=?
            ORDER BY tarih DESC LIMIT 1
        """, (sembol, tarih_str)).fetchone()
    return row["fiyat"] if row else None

def get_son_fiyat(sembol):
    with get_db() as conn:
        row = conn.execute("""
            SELECT fiyat FROM fiyat_gecmisi
            WHERE sembol=?
            ORDER BY tarih DESC LIMIT 1
        """, (sembol,)).fetchone()
    return row["fiyat"] if row else None

def hesapla_portfoy(user_id, hesap_filtre="Hepsi"):
    """Her sembol için portföy pozisyonunu hesapla.
    Sıfırlama mantığı: pozisyon 0'a düşünce maliyet sıfırlanır,
    sonraki alışlar yeni pozisyon olarak hesaplanır.
    """
    with get_db() as conn:
        if hesap_filtre == "Hepsi":
            rows = conn.execute("""
                SELECT sembol, tur, alissat, adet, tutar
                FROM islemler WHERE user_id=?
                ORDER BY sembol, tarih ASC
            """, (user_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT sembol, tur, alissat, adet, tutar
                FROM islemler WHERE user_id=? AND hesap=?
                ORDER BY sembol, tarih ASC
            """, (user_id, hesap_filtre)).fetchall()

    # Her sembol için işlemleri tarih sırasıyla işle, pozisyon sıfırlanınca resetle
    sembol_islemler = {}
    for r in rows:
        s = r["sembol"]
        if s not in sembol_islemler:
            sembol_islemler[s] = {"tur": r["tur"], "islemler": []}
        sembol_islemler[s]["islemler"].append(r)

    pozisyonlar = {}
    for sembol, data in sembol_islemler.items():
        kalan_adet = 0.0
        alis_adet = 0.0
        alis_tutar = 0.0
        satis_adet = 0.0
        satis_tutar = 0.0
        for r in data["islemler"]:
            if r["alissat"] == "Alış":
                kalan_adet += r["adet"]
                alis_adet += r["adet"]
                alis_tutar += r["tutar"]
            else:
                kalan_adet -= r["adet"]
                satis_adet += r["adet"]
                satis_tutar += r["tutar"]
            # Pozisyon sıfırlandıysa (veya negatife düştüyse) resetle
            if kalan_adet <= 0.0001:
                kalan_adet = 0.0
                alis_adet = 0.0
                alis_tutar = 0.0
                satis_adet = 0.0
                satis_tutar = 0.0
        pozisyonlar[sembol] = {
            "alis_adet": alis_adet,
            "alis_tutar": alis_tutar,
            "satis_adet": satis_adet,
            "satis_tutar": satis_tutar,
            "tur": data["tur"]
        }

    bugun_str = str(bugun())
    dun_str = str(bugun() - timedelta(days=1))
    hafta_str = str(bugun() - timedelta(days=7))
    ay_str = str(bugun() - timedelta(days=30))
    uc_ay_str = str(bugun() - timedelta(days=90))
    yilbasi_str = f"{bugun().year}-01-01"

    sonuclar = []
    for sembol, p in pozisyonlar.items():
        kalan_adet = p["alis_adet"] - p["satis_adet"]
        if kalan_adet <= 0:
            continue
        son_fiyat = get_son_fiyat(sembol)
        if not son_fiyat:
            continue

        tur = p["tur"]
        para_birimi = "USD" if tur == "ABD" else "TRY"
        mevcut_deger = kalan_adet * son_fiyat
        maliyet = p["alis_tutar"] - p["satis_tutar"]
        kar_zarar = mevcut_deger - maliyet

        dun_fiyat = get_fiyat(sembol, dun_str)
        # Yatırım fonlarında T+1 valör: alış tarihi = o günün kapanış fiyatı (maliyet).
        # Dolayısıyla ertesi gün (bugün) için günlük getiri henüz başlamamıştır → 0.
        # Kontrol: en son alış tarihi dün ise bugünkü günlük getiri = 0.
        if tur == "FON":
            with get_db() as _c:
                son_alis = _c.execute(
                    "SELECT MAX(tarih) FROM islemler WHERE user_id=? AND sembol=? AND alissat='Alış'",
                    (user_id, sembol)
                ).fetchone()[0]
            gunluk_sifir = (son_alis == bugun_str)
        else:
            gunluk_sifir = False
        if gunluk_sifir:
            gunluk_tl = 0
            gunluk_yuzde = 0
        else:
            gunluk_tl = (son_fiyat - dun_fiyat) * kalan_adet if dun_fiyat else 0
            gunluk_yuzde = ((son_fiyat / dun_fiyat) - 1) * 100 if dun_fiyat else 0

        def donemsel(ref_str):
            ref_fiyat = get_fiyat(sembol, ref_str)
            if not ref_fiyat or ref_fiyat == 0:
                return None, None
            tl = (son_fiyat - ref_fiyat) * kalan_adet
            yuzde = ((son_fiyat / ref_fiyat) - 1) * 100
            return tl, yuzde

        hafta_tl, hafta_pct = donemsel(hafta_str)
        ay_tl, ay_pct = donemsel(ay_str)
        uc_ay_tl, uc_ay_pct = donemsel(uc_ay_str)
        yb_tl, yb_pct = donemsel(yilbasi_str)

        sonuclar.append({
            "sembol": sembol,
            "tur": tur,
            "para_birimi": para_birimi,
            "kalan_adet": kalan_adet,
            "alis_maliyet": maliyet,
            "son_fiyat": son_fiyat,
            "mevcut_deger": mevcut_deger,
            "kar_zarar": kar_zarar,
            "net_kar": net_kar(sembol, tur, kar_zarar),
            "vergisiz": (tur == "BIST") or (sembol in VERGISIZ_FONLAR),
            "abd_vergi": tur == "ABD",
            "gunluk_tl": gunluk_tl,
            "gunluk_yuzde": gunluk_yuzde,
            "hafta_tl": hafta_tl, "hafta_pct": hafta_pct,
            "ay_tl": ay_tl, "ay_pct": ay_pct,
            "uc_ay_tl": uc_ay_tl, "uc_ay_pct": uc_ay_pct,
            "yb_tl": yb_tl, "yb_pct": yb_pct,
        })
    return sonuclar

def get_aylik_getiri(user_id):
    """Son 12 ay için aylık kazanç tablosu."""
    sonuclar = []
    bugun_d = bugun()
    for i in range(11, -1, -1):
        ay_basi = (bugun_d.replace(day=1) - timedelta(days=i*28)).replace(day=1)
        ay_sonu = (ay_basi.replace(month=ay_basi.month % 12 + 1, day=1) - timedelta(days=1)) if ay_basi.month < 12 \
            else ay_basi.replace(month=12, day=31)
        if ay_sonu > bugun_d:
            ay_sonu = bugun_d

        portfoy_basi = hesapla_portfoy_tarih(user_id, str(ay_basi - timedelta(days=1)))
        portfoy_sonu = hesapla_portfoy_tarih(user_id, str(ay_sonu))
        kazanc = portfoy_sonu - portfoy_basi
        sonuclar.append({
            "ay": ay_basi.strftime("%b %Y"),
            "kazanc": kazanc,
            "deger": portfoy_sonu
        })
    return sonuclar

def hesapla_portfoy_tarih(user_id, tarih_str):
    """Belirli bir tarihteki portföy değerini hesapla."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT sembol,
                SUM(CASE WHEN alissat='Alış' THEN adet ELSE -adet END) as net_adet
            FROM islemler
            WHERE user_id=? AND tarih<=?
            GROUP BY sembol
        """, (user_id, tarih_str)).fetchall()
    toplam = 0
    for r in rows:
        if r["net_adet"] > 0:
            f = get_fiyat(r["sembol"], tarih_str)
            if f:
                toplam += r["net_adet"] * f
    return toplam

# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        with get_db() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and user["password_hash"] == hash_pw(password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("dashboard"))
        flash("Kullanıcı adı veya şifre hatalı.", "error")
    return render_template("login.html")

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        email = request.form.get("email","").strip()
        if len(password) < 6:
            flash("Şifre en az 6 karakter olmalı.", "error")
            return render_template("register.html")
        try:
            with get_db() as conn:
                conn.execute("INSERT INTO users (username,password_hash,email) VALUES (?,?,?)",
                             (username, hash_pw(password), email))
                user_id = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
                # Varsayılan hesap ve aracı oluştur
                conn.execute("INSERT INTO hesaplar (user_id, ad) VALUES (?,?)", (user_id, username))
                conn.execute("INSERT INTO aracilar (user_id, ad) VALUES (?,?)", (user_id, "Midas"))
            session["user_id"] = user_id
            session["username"] = username
            return redirect(url_for("dashboard"))
        except sqlite3.IntegrityError:
            flash("Bu kullanıcı adı zaten alınmış.", "error")
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    hesap_filtre = request.args.get("hesap", "Hepsi")

    with get_db() as conn:
        hesaplar = [r["hesap"] for r in conn.execute("""
            SELECT DISTINCT hesap FROM islemler
            WHERE user_id=? AND hesap IS NOT NULL AND hesap != ''
            ORDER BY hesap
        """, (user_id,)).fetchall()]

    portfoy = hesapla_portfoy(user_id, hesap_filtre)

    fon_portfoy  = [p for p in portfoy if p["tur"] == "FON"]
    bist_portfoy = [p for p in portfoy if p["tur"] == "BIST"]
    abd_portfoy  = [p for p in portfoy if p["tur"] == "ABD"]

    usd_try = get_usd_try()

    for p in abd_portfoy:
        p["mevcut_deger_tl"] = p["mevcut_deger"] * usd_try if usd_try else None
        p["kar_zarar_tl"]    = p["kar_zarar"]    * usd_try if usd_try else None
        p["gunluk_tl_tl"]    = p["gunluk_tl"]    * usd_try if usd_try else None

    def s(lst, k): return sum(p[k] for p in lst if p.get(k) is not None)

    fon_deger       = s(fon_portfoy,  "mevcut_deger")
    bist_deger      = s(bist_portfoy, "mevcut_deger")
    abd_deger_usd   = s(abd_portfoy,  "mevcut_deger")
    abd_deger_tl    = abd_deger_usd * usd_try if usd_try else 0
    genel_toplam_tl = fon_deger + bist_deger + abd_deger_tl

    fon_gunluk      = s(fon_portfoy,  "gunluk_tl")
    bist_gunluk     = s(bist_portfoy, "gunluk_tl")
    abd_gunluk_usd  = s(abd_portfoy,  "gunluk_tl")
    abd_gunluk_tl   = abd_gunluk_usd * usd_try if usd_try else 0
    genel_gunluk_tl = fon_gunluk + bist_gunluk + abd_gunluk_tl

    fon_kar         = s(fon_portfoy,  "kar_zarar")
    bist_kar        = s(bist_portfoy, "kar_zarar")
    abd_kar_usd     = s(abd_portfoy,  "kar_zarar")
    abd_kar_tl      = abd_kar_usd * usd_try if usd_try else 0
    genel_kar_tl    = fon_kar + bist_kar + abd_kar_tl

    aylik = get_aylik_getiri(user_id)

    with get_db() as conn:
        son_fiyat_tarihi = conn.execute(
            "SELECT MAX(tarih) as t FROM fiyat_gecmisi").fetchone()["t"] or "-"
        son_log = conn.execute(
            "SELECT * FROM price_fetch_log ORDER BY id DESC LIMIT 1").fetchone()
        nakit_rows = conn.execute(
            "SELECT para_birimi, tutar FROM nakit_bakiye WHERE user_id=?", (user_id,)).fetchall()
    nakit = {r["para_birimi"]: r["tutar"] for r in nakit_rows}
    nakit_usd = nakit.get("USD", 0.0)
    nakit_try = nakit.get("TRY", 0.0)
    nakit_usd_tl = nakit_usd * usd_try if usd_try else 0
    genel_toplam_tl = fon_deger + bist_deger + abd_deger_tl + nakit_usd_tl + nakit_try

    return render_template("dashboard.html",
        fon_portfoy=fon_portfoy, bist_portfoy=bist_portfoy, abd_portfoy=abd_portfoy,
        usd_try=usd_try,
        fon_deger=fon_deger, bist_deger=bist_deger,
        abd_deger_usd=abd_deger_usd, abd_deger_tl=abd_deger_tl,
        genel_toplam_tl=genel_toplam_tl, genel_gunluk_tl=genel_gunluk_tl,
        genel_kar_tl=genel_kar_tl,
        fon_kar=fon_kar, bist_kar=bist_kar,
        abd_kar_usd=abd_kar_usd, abd_kar_tl=abd_kar_tl,
        nakit_usd=nakit_usd, nakit_try=nakit_try, nakit_usd_tl=nakit_usd_tl,
        abd_gunluk_tl=abd_gunluk_tl,
        hesaplar=hesaplar, hesap_filtre=hesap_filtre,
        aylik=aylik, son_fiyat_tarihi=son_fiyat_tarihi, son_log=son_log,
    )

# ── İşlemler ────────────────────────────────────────────────────────────────

@app.route("/islemler")
@login_required
def islemler():
    user_id = session["user_id"]
    filtre = request.args.get("sembol", "")
    tur_filtre = request.args.get("tur", "")
    hesap_filtre = request.args.get("hesap", "")
    with get_db() as conn:
        hesaplar = [r["ad"] for r in conn.execute("SELECT ad FROM hesaplar WHERE user_id=?", (user_id,)).fetchall()]
        aracilar = [r["ad"] for r in conn.execute("SELECT ad FROM aracilar WHERE user_id=?", (user_id,)).fetchall()]

        conditions = ["user_id=?"]
        params = [user_id]
        if filtre:
            conditions.append("sembol=?")
            params.append(filtre.upper())
        if tur_filtre:
            conditions.append("tur=?")
            params.append(tur_filtre)
        if hesap_filtre:
            conditions.append("hesap=?")
            params.append(hesap_filtre)

        where = " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT * FROM islemler WHERE {where} ORDER BY tarih DESC", params
        ).fetchall()

        tum = conn.execute(
            "SELECT DISTINCT sembol, tur FROM islemler WHERE user_id=? ORDER BY tur, sembol",
            (user_id,)
        ).fetchall()

    fon_semboller  = [r["sembol"] for r in tum if r["tur"] == "FON"]
    bist_semboller = [r["sembol"] for r in tum if r["tur"] == "BIST"]
    abd_semboller  = [r["sembol"] for r in tum if r["tur"] == "ABD"]

    return render_template("islemler.html",
        islemler=rows, hesaplar=hesaplar, aracilar=aracilar,
        filtre=filtre, tur_filtre=tur_filtre, hesap_filtre=hesap_filtre,
        fon_semboller=fon_semboller,
        bist_semboller=bist_semboller,
        abd_semboller=abd_semboller)

@app.route("/islem-ekle", methods=["POST"])
@login_required
def islem_ekle():
    user_id = session["user_id"]
    sembol = request.form["sembol"].strip().upper()
    tur = request.form["tur"]  # FON veya HISSE
    hesap = request.form["hesap"]
    araciKurum = request.form["araciKurum"]
    alissat = request.form["alissat"]
    adet = float(request.form["adet"].replace(",","."))
    fiyat = float(request.form["fiyat"].replace(",","."))
    tarih = request.form["tarih"]
    tutar = adet * fiyat

    with get_db() as conn:
        conn.execute("""
            INSERT INTO islemler (user_id,sembol,tur,hesap,araciKurum,alissat,adet,fiyat,tutar,tarih)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (user_id, sembol, tur, hesap, araciKurum, alissat, adet, fiyat, tutar, tarih))

        # Fiyat geçmişine de ekle (bu tarih için fiyat yoksa)
        conn.execute("""
            INSERT OR IGNORE INTO fiyat_gecmisi (sembol, tarih, fiyat)
            VALUES (?,?,?)
        """, (sembol, tarih, fiyat))

    flash(f"{sembol} {alissat} işlemi eklendi.", "success")
    return redirect(url_for("islemler"))

@app.route("/islem-sil/<int:islem_id>")
@login_required
def islem_sil(islem_id):
    user_id = session["user_id"]
    with get_db() as conn:
        conn.execute("DELETE FROM islemler WHERE id=? AND user_id=?", (islem_id, user_id))
    flash("İşlem silindi.", "success")
    return redirect(url_for("islemler"))

@app.route("/islem-duzenle", methods=["POST"])
@login_required
def islem_duzenle():
    user_id = session["user_id"]
    islem_id = request.form["islem_id"]
    sembol = request.form["sembol"].strip().upper()
    tur = request.form["tur"]
    hesap = request.form["hesap"]
    araciKurum = request.form["araciKurum"]
    alissat = request.form["alissat"]
    adet = float(request.form["adet"].replace(",","."))
    fiyat = float(request.form["fiyat"].replace(",","."))
    tarih = request.form["tarih"]
    tutar = adet * fiyat
    with get_db() as conn:
        conn.execute("""
            UPDATE islemler SET sembol=?,tur=?,hesap=?,araciKurum=?,alissat=?,
            adet=?,fiyat=?,tutar=?,tarih=? WHERE id=? AND user_id=?
        """, (sembol, tur, hesap, araciKurum, alissat, adet, fiyat, tutar, tarih, islem_id, user_id))
        conn.execute("INSERT OR IGNORE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                     (sembol, tarih, fiyat))
    flash(f"{sembol} işlemi güncellendi.", "success")
    return redirect(url_for("islemler"))


@login_required
def islem_sil(islem_id):
    user_id = session["user_id"]
    with get_db() as conn:
        conn.execute("DELETE FROM islemler WHERE id=? AND user_id=?", (islem_id, user_id))
    flash("İşlem silindi.", "success")
    return redirect(url_for("islemler"))

# ── Fiyat Geçmişi ────────────────────────────────────────────────────────────

@app.route("/fiyatlar")
@login_required
def fiyatlar():
    user_id = session["user_id"]

    with get_db() as conn:
        # Tüm sembolleri tur bilgisiyle al
        islem_rows = conn.execute(
            "SELECT DISTINCT sembol, tur FROM islemler WHERE user_id=?", (user_id,)).fetchall()
        logs = conn.execute(
            "SELECT * FROM price_fetch_log ORDER BY id DESC LIMIT 4").fetchall()

    # Türe göre grupla
    tur_map = {r["sembol"]: r["tur"] for r in islem_rows}
    fon_sembolleri  = sorted([s for s,t in tur_map.items() if t == "FON"])
    bist_sembolleri = sorted([s for s,t in tur_map.items() if t == "BIST"])
    abd_sembolleri  = sorted([s for s,t in tur_map.items() if t == "ABD"])

    def pivot_yap(semboller):
        if not semboller:
            return [], []
        with get_db() as conn:
            ph = ",".join("?"*len(semboller))
            raw = conn.execute(f"""
                SELECT tarih, sembol, fiyat FROM fiyat_gecmisi
                WHERE sembol IN ({ph}) ORDER BY tarih DESC
            """, semboller).fetchall()
        pivot = {}
        for r in raw:
            pivot.setdefault(r["tarih"], {})[r["sembol"]] = r["fiyat"]
        tarihler = sorted(pivot.keys(), reverse=True)
        tablo = [{"tarih": t, **{s: pivot[t].get(s) for s in semboller}} for t in tarihler]
        return semboller, tablo

    fon_semboller, fon_tablo   = pivot_yap(fon_sembolleri)
    bist_semboller, bist_tablo = pivot_yap(bist_sembolleri)
    abd_semboller, abd_tablo   = pivot_yap(abd_sembolleri)
    tum_semboller = fon_sembolleri + bist_sembolleri + abd_sembolleri

    return render_template("fiyatlar.html",
        fon_semboller=fon_semboller, fon_tablo=fon_tablo,
        bist_semboller=bist_semboller, bist_tablo=bist_tablo,
        abd_semboller=abd_semboller, abd_tablo=abd_tablo,
        tum_semboller=tum_semboller, logs=logs)

@app.route("/fiyat-ekle", methods=["POST"])
@login_required
def fiyat_ekle():
    sembol = request.form["sembol"].strip().upper()
    tarih = request.form["tarih"]
    fiyat = float(request.form["fiyat"].replace(",","."))
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO fiyat_gecmisi (sembol, tarih, fiyat)
            VALUES (?,?,?)
        """, (sembol, tarih, fiyat))
    flash(f"{sembol} fiyatı güncellendi.", "success")
    return redirect(url_for("fiyatlar"))

@app.route("/fiyat-guncelle", methods=["POST"])
@login_required
def fiyat_guncelle():
    """FON, BIST ve ABD fiyatlarını güncelle."""
    import requests as req
    from price_fetcher import fetch_fon_fiyatlari

    with get_db() as conn:
        tum = conn.execute("SELECT DISTINCT sembol, tur FROM islemler").fetchall()

    fon_sembolleri  = [r["sembol"] for r in tum if r["tur"] == "FON"]
    bist_sembolleri = [r["sembol"] for r in tum if r["tur"] == "BIST"]
    abd_sembolleri  = [r["sembol"] for r in tum if r["tur"] == "ABD"]

    bugun_str = str(bugun())
    simdi = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    basarili = 0
    hatalar = []

    # FON → TEFAS
    if fon_sembolleri:
        fon_prices, _ = fetch_fon_fiyatlari(fon_sembolleri)
        for sembol, fiyat in fon_prices.items():
            with get_db() as conn:
                conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                             (sembol, bugun_str, fiyat))
            basarili += 1
        eksik = [s for s in fon_sembolleri if s not in fon_prices]
        if eksik:
            hatalar.append(f"FON alınamadı: {','.join(eksik)}")

    # BIST + ABD → Yahoo Finance
    def yahoo_fiyat(sembol, tur):
        yahoo_s = f"{sembol}.IS" if tur == "BIST" else sembol
        try:
            r = req.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_s}?interval=1d&range=2d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10
            )
            if r.status_code == 200:
                closes = r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                for c in reversed(closes):
                    if c is not None:
                        return round(float(c), 4)
        except Exception:
            pass
        return None

    for sembol in bist_sembolleri + abd_sembolleri:
        tur = "BIST" if sembol in bist_sembolleri else "ABD"
        fiyat = yahoo_fiyat(sembol, tur)
        if fiyat:
            with get_db() as conn:
                conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                             (sembol, bugun_str, fiyat))
            basarili += 1
        else:
            hatalar.append(sembol)

    with get_db() as conn:
        conn.execute("INSERT INTO price_fetch_log (tarih,sonuc,detay) VALUES (?,?,?)",
                     (simdi, "Güncelleme",
                      f"{basarili} fiyat güncellendi. " + (f"Hata: {','.join(hatalar)}" if hatalar else "")))

    flash(f"✅ {basarili} fiyat güncellendi.", "success")
    return redirect(url_for("fiyatlar"))

# ── Ayarlar ──────────────────────────────────────────────────────────────────

@app.route("/ayarlar", methods=["GET","POST"])
@login_required
def ayarlar():
    user_id = session["user_id"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "hesap_ekle":
            ad = request.form["hesap_ad"].strip()
            if ad:
                with get_db() as conn:
                    conn.execute("INSERT INTO hesaplar (user_id,ad) VALUES (?,?)", (user_id, ad))
        elif action == "hesap_sil":
            hid = request.form["hesap_id"]
            with get_db() as conn:
                conn.execute("DELETE FROM hesaplar WHERE id=? AND user_id=?", (hid, user_id))
        elif action == "aracilar_ekle":
            ad = request.form["aracilar_ad"].strip()
            if ad:
                with get_db() as conn:
                    conn.execute("INSERT INTO aracilar (user_id,ad) VALUES (?,?)", (user_id, ad))
        elif action == "aracilar_sil":
            aid = request.form["aracilar_id"]
            with get_db() as conn:
                conn.execute("DELETE FROM aracilar WHERE id=? AND user_id=?", (aid, user_id))
        elif action == "nakit_guncelle":
            for pb in ["USD", "TRY"]:
                tutar_str = request.form.get(f"nakit_{pb}", "0").replace(",", ".").strip()
                try:
                    tutar = float(tutar_str)
                except ValueError:
                    tutar = 0.0
                with get_db() as conn:
                    conn.execute("""
                        INSERT INTO nakit_bakiye (user_id, para_birimi, tutar)
                        VALUES (?,?,?)
                        ON CONFLICT(user_id, para_birimi) DO UPDATE SET tutar=excluded.tutar
                    """, (user_id, pb, tutar))
            flash("Nakit bakiyeler güncellendi.", "success")
        elif action == "sifre":
            eski = request.form.get("eski_sifre","")
            yeni = request.form.get("yeni_sifre","")
            if not eski or not yeni:
                flash("Şifre alanları boş bırakılamaz.", "error")
            elif len(yeni) < 6:
                flash("Yeni şifre en az 6 karakter olmalı.", "error")
            else:
                with get_db() as conn:
                    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                if user["password_hash"] == hash_pw(eski):
                    with get_db() as conn:
                        conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                                     (hash_pw(yeni), user_id))
                    flash("Şifre güncellendi.", "success")
                else:
                    flash("Eski şifre hatalı.", "error")
        return redirect(url_for("ayarlar"))

    with get_db() as conn:
        # Tekrarlı kayıtları temizle, sadece unique olanları tut
        conn.execute("""
            DELETE FROM hesaplar WHERE id NOT IN (
                SELECT MIN(id) FROM hesaplar WHERE user_id=? GROUP BY ad
            ) AND user_id=?
        """, (user_id, user_id))
        conn.execute("""
            DELETE FROM aracilar WHERE id NOT IN (
                SELECT MIN(id) FROM aracilar WHERE user_id=? GROUP BY ad
            ) AND user_id=?
        """, (user_id, user_id))
        hesaplar = conn.execute("SELECT * FROM hesaplar WHERE user_id=? ORDER BY ad", (user_id,)).fetchall()
        aracilar = conn.execute("SELECT * FROM aracilar WHERE user_id=? ORDER BY ad", (user_id,)).fetchall()

    with get_db() as conn:
        nakit_rows = conn.execute("SELECT para_birimi, tutar FROM nakit_bakiye WHERE user_id=?", (user_id,)).fetchall()
    nakit = {r["para_birimi"]: r["tutar"] for r in nakit_rows}
    return render_template("ayarlar.html", hesaplar=hesaplar, aracilar=aracilar, nakit=nakit)

# ── API ──────────────────────────────────────────────────────────────────────

@app.route("/api/bulk-import", methods=["POST"])
@login_required
def bulk_import():
    """Excel'den toplu işlem yükleme — JSON body bekler."""
    import os
    key = request.json.get("key","")
    if key != os.environ.get("IMPORT_KEY",""):
        return jsonify({"error": "yetkisiz"}), 403

    user_id = request.json.get("user_id", session["user_id"])
    islemler = request.json.get("islemler", [])

    # Önce mevcut excel işlemlerini temizle (manuel girilen ZPX30 vs kalır)
    with get_db() as conn:
        conn.execute("DELETE FROM islemler WHERE user_id=? AND tur='FON'", (user_id,))

    eklenen = 0
    with get_db() as conn:
        for i in islemler:
            tutar = i["adet"] * i["fiyat"]
            conn.execute("""
                INSERT INTO islemler
                (user_id, sembol, tur, hesap, araciKurum, alissat, adet, fiyat, tutar, tarih)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (user_id, i["sembol"], "FON", i["hesap"], i["araciKurum"],
                  i["alissat"], i["adet"], i["fiyat"], tutar, i["tarih"]))
            # Fiyat geçmişine de ekle
            conn.execute("""
                INSERT OR IGNORE INTO fiyat_gecmisi (sembol, tarih, fiyat)
                VALUES (?,?,?)
            """, (i["sembol"], i["tarih"], i["fiyat"]))
            # Hesap ve aracı otomatik oluştur
            conn.execute("INSERT OR IGNORE INTO hesaplar (user_id, ad) VALUES (?,?)",
                         (user_id, i["hesap"]))
            conn.execute("INSERT OR IGNORE INTO aracilar (user_id, ad) VALUES (?,?)",
                         (user_id, i["araciKurum"]))
            eklenen += 1

    return jsonify({"ok": True, "eklenen": eklenen})

@app.route("/import-excel", methods=["GET","POST"])
@login_required
def import_excel():
    """Tek tıkla Excel işlemlerini yükle."""
    import os
    IMPORT_KEY = os.environ.get("IMPORT_KEY","")

    excel_islemler = [
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Alış","adet":230972.0,"fiyat":2.16834,"tarih":"2025-10-30"},
        {"sembol":"PRY","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Alış","adet":225687.0,"fiyat":2.171141,"tarih":"2025-10-31"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Alış","adet":287867.0,"fiyat":2.171141,"tarih":"2025-10-31"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":2012766.0,"fiyat":2.171141,"tarih":"2025-10-31"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Alış","adet":458221.0,"fiyat":2.182357,"tarih":"2025-11-04"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Alış","adet":306472.0,"fiyat":2.185182,"tarih":"2025-11-05"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":2012766.0,"fiyat":2.204968,"tarih":"2025-11-12"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Alış","adet":662037.0,"fiyat":1.508628,"tarih":"2025-11-13"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":1605417.0,"fiyat":1.510512,"tarih":"2025-11-14"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Alış","adet":662.0,"fiyat":1.510512,"tarih":"2025-11-14"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":35016.0,"fiyat":1.713539,"tarih":"2026-02-17"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":738829.0,"fiyat":1.827211,"tarih":"2026-04-09"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Satış","adet":662699.0,"fiyat":1.839223,"tarih":"2026-04-14"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Midas","alissat":"Satış","adet":1283532.0,"fiyat":2.684218,"tarih":"2026-04-14"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":814264.0,"fiyat":1.842151,"tarih":"2026-04-15"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":1524885.0,"fiyat":2.688726,"tarih":"2026-04-15"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":54283.0,"fiyat":1.842151,"tarih":"2026-04-15"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":184243.0,"fiyat":2.713819,"tarih":"2026-04-22"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":268940.0,"fiyat":1.859155,"tarih":"2026-04-22"},
        {"sembol":"PRY","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Satış","adet":36849.0,"fiyat":2.713819,"tarih":"2026-04-22"},
        {"sembol":"TLY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":98.0,"fiyat":5216.385597,"tarih":"2026-04-28"},
        {"sembol":"PHE","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":175068.0,"fiyat":2.856028,"tarih":"2026-04-28"},
        {"sembol":"TLY","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Alış","adet":9.0,"fiyat":5216.385597,"tarih":"2026-04-28"},
        {"sembol":"PHE","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Alış","adet":17506.0,"fiyat":2.856028,"tarih":"2026-04-28"},
        {"sembol":"TP2","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":253020.0,"fiyat":1.976129,"tarih":"2026-06-09"},
        {"sembol":"PRY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":173395.0,"fiyat":2.883597,"tarih":"2026-06-09"},
        {"sembol":"PRY","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Satış","adet":34679.0,"fiyat":2.883597,"tarih":"2026-06-09"},
        {"sembol":"PHE","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Alış","adet":30367.0,"fiyat":3.292996,"tarih":"2026-06-11"},
        {"sembol":"PHE","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":182204.0,"fiyat":3.292996,"tarih":"2026-06-11"},
        {"sembol":"TLY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Alış","adet":57.0,"fiyat":6063.543833,"tarih":"2026-06-11"},
        {"sembol":"TLY","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":155.0,"fiyat":6523.1441,"tarih":"2026-06-16"},
        {"sembol":"PHE","hesap":"Murat","araciKurum":"Murat Denizbank","alissat":"Satış","adet":357272.0,"fiyat":3.49162,"tarih":"2026-06-16"},
        {"sembol":"PHE","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Satış","adet":47873.0,"fiyat":3.49162,"tarih":"2026-06-16"},
        {"sembol":"TLY","hesap":"Berrin","araciKurum":"Berrin Denizbank","alissat":"Satış","adet":9.0,"fiyat":6523.1441,"tarih":"2026-06-17"},
    ]

    if request.method == "POST":
        key = request.form.get("key","")
        if not IMPORT_KEY or key != IMPORT_KEY:
            flash("Import key hatalı.", "error")
            return redirect(url_for("import_excel"))

        user_id = session["user_id"]
        with get_db() as conn:
            conn.execute("DELETE FROM islemler WHERE user_id=? AND tur='FON'", (user_id,))

        eklenen = 0
        with get_db() as conn:
            for i in excel_islemler:
                tutar = i["adet"] * i["fiyat"]
                conn.execute("""
                    INSERT INTO islemler
                    (user_id,sembol,tur,hesap,araciKurum,alissat,adet,fiyat,tutar,tarih)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (user_id, i["sembol"], "FON", i["hesap"], i["araciKurum"],
                      i["alissat"], i["adet"], i["fiyat"], tutar, i["tarih"]))
                conn.execute("INSERT OR IGNORE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                             (i["sembol"], i["tarih"], i["fiyat"]))
                conn.execute("INSERT OR IGNORE INTO hesaplar (user_id,ad) VALUES (?,?)",
                             (user_id, i["hesap"]))
                conn.execute("INSERT OR IGNORE INTO aracilar (user_id,ad) VALUES (?,?)",
                             (user_id, i["araciKurum"]))
                eklenen += 1

        flash(f"✅ {eklenen} işlem başarıyla yüklendi!", "success")
        return redirect(url_for("islemler"))

    return render_template("import_excel.html", count=len(excel_islemler))


@app.route("/fiyat-backfill-debug")
@login_required
def fiyat_backfill_debug():
    """Backfill'i senkron çalıştır, sonucu ekranda göster."""
    from price_fetcher import fetch_tefas_fon
    from datetime import date, timedelta
    import time as time_mod

    user_id = session["user_id"]
    with get_db() as conn:
        fon_sembolleri = [r["sembol"] for r in conn.execute(
            "SELECT DISTINCT sembol FROM islemler WHERE user_id=? AND tur='FON'",
            (user_id,)).fetchall()]

    bugun_d = date.today()
    baslangic = bugun_d - timedelta(days=10)  # Sadece son 10 gün test

    with get_db() as conn:
        mevcut = set()
        for r in conn.execute(
            "SELECT sembol, tarih FROM fiyat_gecmisi WHERE sembol IN ({})".format(
                ",".join("?"*len(fon_sembolleri))), fon_sembolleri).fetchall():
            mevcut.add((r["sembol"], r["tarih"]))

    eksikler = {}
    gun = baslangic
    while gun <= bugun_d:
        if gun.weekday() < 5:
            tarih_str = str(gun)
            for s in fon_sembolleri:
                if (s, tarih_str) not in mevcut:
                    eksikler.setdefault(tarih_str, []).append(s)
        gun += timedelta(days=1)

    log = [f"Fonlar: {fon_sembolleri}"]
    log.append(f"Eksik kombinasyon: {sum(len(v) for v in eksikler.values())} ({len(eksikler)} gün)")

    for tarih_str, semboller in sorted(eksikler.items()):
        hedef = date.fromisoformat(tarih_str)
        for sembol in semboller:
            try:
                fiyat = fetch_tefas_fon(sembol, hedef)
                if fiyat:
                    with get_db() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                            (sembol, tarih_str, fiyat))
                    log.append(f"✅ {sembol} {tarih_str}: {fiyat}")
                else:
                    log.append(f"⚠️ {sembol} {tarih_str}: veri yok (tatil?)")
            except Exception as e:
                log.append(f"❌ {sembol} {tarih_str}: HATA: {e}")
            time_mod.sleep(1)

    return "<pre style='background:#111;color:#eee;padding:1rem'>" + "\n".join(log) + "</pre>"


@app.route("/fiyat-backfill", methods=["POST"])
@login_required
def fiyat_backfill():
    """Kullanıcının seçtiği tarih aralığındaki fiyatları toplu çek."""
    from price_fetcher import fetch_fon_aralik
    from datetime import date

    user_id = session["user_id"]
    with get_db() as conn:
        fon_sembolleri = list(set(r["sembol"] for r in conn.execute(
            "SELECT DISTINCT sembol FROM islemler WHERE user_id=? AND tur=\'FON\'",
            (user_id,)).fetchall()))

    if not fon_sembolleri:
        flash("Hiç fon işlemi bulunamadı.", "error")
        return redirect(url_for("fiyatlar"))

    # Tarih aralığı
    baslangic_str = request.form.get("baslangic","")
    bitis_str = request.form.get("bitis","")
    try:
        baslangic = date.fromisoformat(baslangic_str)
        bitis = date.fromisoformat(bitis_str)
    except Exception:
        from datetime import timedelta
        bitis = date.today()
        baslangic = bitis - timedelta(days=30)

    if baslangic > bitis:
        baslangic, bitis = bitis, baslangic

    gun_sayisi = (bitis - baslangic).days + 1

    # Toplu çek
    tum_veriler = fetch_fon_aralik(fon_sembolleri, baslangic, bitis)

    eklenen = 0
    for (sembol, tarih_str), fiyat in tum_veriler.items():
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                (sembol, tarih_str, fiyat))
        eklenen += 1

    simdi = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("INSERT INTO price_fetch_log (tarih,sonuc,detay) VALUES (?,?,?)",
                     (simdi, "Backfill-TEFAS",
                      f"{eklenen} fiyat ({baslangic_str} → {bitis_str}, {len(fon_sembolleri)} fon)"))

    flash(f"✅ {eklenen} fiyat güncellendi ({baslangic_str} → {bitis_str}).", "success")
    return redirect(url_for("fiyatlar"))


    bugun_d = date.today()
    baslangic = bugun_d - timedelta(days=60)

    flash(f"⏳ {len(fon_sembolleri)} fon için 60 günlük veri çekiliyor... Lütfen bekleyin.", "success")

    # Toplu çek
    tum_veriler = fetch_fon_aralik(fon_sembolleri, baslangic, bugun_d)

    eklenen = 0
    for (sembol, tarih_str), fiyat in tum_veriler.items():
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                (sembol, tarih_str, fiyat))
        eklenen += 1

    simdi = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("INSERT INTO price_fetch_log (tarih,sonuc,detay) VALUES (?,?,?)",
                     (simdi, "Backfill-TEFAS",
                      f"{eklenen} fiyat dolduruldu (60 gun, {len(fon_sembolleri)} fon)"))

    flash(f"✅ {eklenen} fiyat güncellendi.", "success")
    return redirect(url_for("fiyatlar"))


@app.route("/import-fiyatlar", methods=["GET","POST"])
@login_required
def import_fiyatlar():
    """Excel'den fiyat geçmişini yükle."""
    import os
    IMPORT_KEY = os.environ.get("IMPORT_KEY","")

    excel_fiyatlar = [
        {"sembol":"PHE","tarih":"2025-11-10","fiyat":1.286225},
        {"sembol":"PRY","tarih":"2025-11-10","fiyat":2.199214},
        {"sembol":"TLY","tarih":"2025-11-10","fiyat":2494.924695},
        {"sembol":"TP2","tarih":"2025-11-10","fiyat":1.502571},
        {"sembol":"PHE","tarih":"2025-11-11","fiyat":1.277979},
        {"sembol":"PRY","tarih":"2025-11-11","fiyat":2.202102},
        {"sembol":"TLY","tarih":"2025-11-11","fiyat":2584.982327},
        {"sembol":"TP2","tarih":"2025-11-11","fiyat":1.504759},
        {"sembol":"PHE","tarih":"2025-11-12","fiyat":1.253117},
        {"sembol":"PRY","tarih":"2025-11-12","fiyat":2.204968},
        {"sembol":"TLY","tarih":"2025-11-12","fiyat":2588.952371},
        {"sembol":"TP2","tarih":"2025-11-12","fiyat":1.50669},
        {"sembol":"PHE","tarih":"2025-11-13","fiyat":1.27107},
        {"sembol":"PRY","tarih":"2025-11-13","fiyat":2.207714},
        {"sembol":"TLY","tarih":"2025-11-13","fiyat":2607.620273},
        {"sembol":"TP2","tarih":"2025-11-13","fiyat":1.508628},
        {"sembol":"PHE","tarih":"2025-11-14","fiyat":1.28554},
        {"sembol":"PRY","tarih":"2025-11-14","fiyat":2.210515},
        {"sembol":"TLY","tarih":"2025-11-14","fiyat":2604.424081},
        {"sembol":"TP2","tarih":"2025-11-14","fiyat":1.510512},
        {"sembol":"PHE","tarih":"2025-11-17","fiyat":1.316465},
        {"sembol":"PRY","tarih":"2025-11-17","fiyat":2.219069},
        {"sembol":"TLY","tarih":"2025-11-17","fiyat":2622.992679},
        {"sembol":"TP2","tarih":"2025-11-17","fiyat":1.516315},
        {"sembol":"PHE","tarih":"2025-11-18","fiyat":1.32859},
        {"sembol":"PRY","tarih":"2025-11-18","fiyat":2.222019},
        {"sembol":"TLY","tarih":"2025-11-18","fiyat":2616.579127},
        {"sembol":"TP2","tarih":"2025-11-18","fiyat":1.518429},
        {"sembol":"PHE","tarih":"2025-11-19","fiyat":1.334587},
        {"sembol":"PRY","tarih":"2025-11-19","fiyat":2.224891},
        {"sembol":"TLY","tarih":"2025-11-19","fiyat":2646.246782},
        {"sembol":"TP2","tarih":"2025-11-19","fiyat":1.520401},
        {"sembol":"PHE","tarih":"2025-11-20","fiyat":1.343687},
        {"sembol":"PRY","tarih":"2025-11-20","fiyat":2.227676},
        {"sembol":"TLY","tarih":"2025-11-20","fiyat":2649.287334},
        {"sembol":"TP2","tarih":"2025-11-20","fiyat":1.522408},
        {"sembol":"PHE","tarih":"2025-11-21","fiyat":1.357948},
        {"sembol":"PRY","tarih":"2025-11-21","fiyat":2.230589},
        {"sembol":"TLY","tarih":"2025-11-21","fiyat":2681.58796},
        {"sembol":"TP2","tarih":"2025-11-21","fiyat":1.524529},
        {"sembol":"PHE","tarih":"2025-11-24","fiyat":1.373012},
        {"sembol":"PRY","tarih":"2025-11-24","fiyat":2.239292},
        {"sembol":"TLY","tarih":"2025-11-24","fiyat":2698.902726},
        {"sembol":"TP2","tarih":"2025-11-24","fiyat":1.53067},
        {"sembol":"PHE","tarih":"2025-11-25","fiyat":1.381065},
        {"sembol":"PRY","tarih":"2025-11-25","fiyat":2.242458},
        {"sembol":"TLY","tarih":"2025-11-25","fiyat":2745.91243},
        {"sembol":"TP2","tarih":"2025-11-25","fiyat":1.532888},
        {"sembol":"PHE","tarih":"2025-11-26","fiyat":1.379177},
        {"sembol":"PRY","tarih":"2025-11-26","fiyat":2.2455},
        {"sembol":"TLY","tarih":"2025-11-26","fiyat":2724.238934},
        {"sembol":"TP2","tarih":"2025-11-26","fiyat":1.535071},
        {"sembol":"PHE","tarih":"2025-11-27","fiyat":1.381856},
        {"sembol":"PRY","tarih":"2025-11-27","fiyat":2.24869},
        {"sembol":"TLY","tarih":"2025-11-27","fiyat":2721.188397},
        {"sembol":"TP2","tarih":"2025-11-27","fiyat":1.537281},
        {"sembol":"PHE","tarih":"2025-11-28","fiyat":1.38698},
        {"sembol":"PRY","tarih":"2025-11-28","fiyat":2.251757},
        {"sembol":"TLY","tarih":"2025-11-28","fiyat":2667.471199},
        {"sembol":"TP2","tarih":"2025-11-28","fiyat":1.539354},
        {"sembol":"PHE","tarih":"2025-12-01","fiyat":1.376069},
        {"sembol":"PRY","tarih":"2025-12-01","fiyat":2.260721},
        {"sembol":"TLY","tarih":"2025-12-01","fiyat":2674.595394},
        {"sembol":"TP2","tarih":"2025-12-01","fiyat":1.545958},
        {"sembol":"PHE","tarih":"2025-12-02","fiyat":1.397244},
        {"sembol":"PRY","tarih":"2025-12-02","fiyat":2.263819},
        {"sembol":"TLY","tarih":"2025-12-02","fiyat":2716.348887},
        {"sembol":"TP2","tarih":"2025-12-02","fiyat":1.548131},
        {"sembol":"PHE","tarih":"2025-12-03","fiyat":1.399202},
        {"sembol":"PRY","tarih":"2025-12-03","fiyat":2.266846},
        {"sembol":"TLY","tarih":"2025-12-03","fiyat":2716.935065},
        {"sembol":"TP2","tarih":"2025-12-03","fiyat":1.550258},
        {"sembol":"PHE","tarih":"2025-12-04","fiyat":1.396477},
        {"sembol":"PRY","tarih":"2025-12-04","fiyat":2.269731},
        {"sembol":"TLY","tarih":"2025-12-04","fiyat":2752.971496},
        {"sembol":"TP2","tarih":"2025-12-04","fiyat":1.552052},
        {"sembol":"PHE","tarih":"2025-12-05","fiyat":1.377409},
        {"sembol":"PRY","tarih":"2025-12-05","fiyat":2.27267},
        {"sembol":"TLY","tarih":"2025-12-05","fiyat":2759.514015},
        {"sembol":"TP2","tarih":"2025-12-05","fiyat":1.554256},
        {"sembol":"PHE","tarih":"2025-12-08","fiyat":1.406442},
        {"sembol":"PRY","tarih":"2025-12-08","fiyat":2.281725},
        {"sembol":"TLY","tarih":"2025-12-08","fiyat":2760.709445},
        {"sembol":"TP2","tarih":"2025-12-08","fiyat":1.560518},
        {"sembol":"PHE","tarih":"2025-12-09","fiyat":1.432988},
        {"sembol":"PRY","tarih":"2025-12-09","fiyat":2.28476},
        {"sembol":"TLY","tarih":"2025-12-09","fiyat":2777.378172},
        {"sembol":"TP2","tarih":"2025-12-09","fiyat":1.562598},
        {"sembol":"PHE","tarih":"2025-12-10","fiyat":1.44185},
        {"sembol":"PRY","tarih":"2025-12-10","fiyat":2.287783},
        {"sembol":"TLY","tarih":"2025-12-10","fiyat":2801.994486},
        {"sembol":"TP2","tarih":"2025-12-10","fiyat":1.564779},
        {"sembol":"PHE","tarih":"2025-12-11","fiyat":1.433017},
        {"sembol":"PRY","tarih":"2025-12-11","fiyat":2.290756},
        {"sembol":"TLY","tarih":"2025-12-11","fiyat":2773.644534},
        {"sembol":"TP2","tarih":"2025-12-11","fiyat":1.566863},
        {"sembol":"PHE","tarih":"2025-12-12","fiyat":1.446598},
        {"sembol":"PRY","tarih":"2025-12-12","fiyat":2.293907},
        {"sembol":"TLY","tarih":"2025-12-12","fiyat":2773.278951},
        {"sembol":"TP2","tarih":"2025-12-12","fiyat":1.569025},
        {"sembol":"PHE","tarih":"2025-12-15","fiyat":1.462894},
        {"sembol":"PRY","tarih":"2025-12-15","fiyat":2.302916},
        {"sembol":"TLY","tarih":"2025-12-15","fiyat":2811.673661},
        {"sembol":"TP2","tarih":"2025-12-15","fiyat":1.5753},
        {"sembol":"PHE","tarih":"2025-12-16","fiyat":1.475163},
        {"sembol":"PRY","tarih":"2025-12-16","fiyat":2.305922},
        {"sembol":"TLY","tarih":"2025-12-16","fiyat":2817.779829},
        {"sembol":"TP2","tarih":"2025-12-16","fiyat":1.577841},
        {"sembol":"PHE","tarih":"2025-12-17","fiyat":1.464597},
        {"sembol":"PRY","tarih":"2025-12-17","fiyat":2.308931},
        {"sembol":"TLY","tarih":"2025-12-17","fiyat":2839.84733},
        {"sembol":"TP2","tarih":"2025-12-17","fiyat":1.579933},
        {"sembol":"PHE","tarih":"2025-12-18","fiyat":1.461166},
        {"sembol":"PRY","tarih":"2025-12-18","fiyat":2.311797},
        {"sembol":"TLY","tarih":"2025-12-18","fiyat":2852.085092},
        {"sembol":"TP2","tarih":"2025-12-18","fiyat":1.581974},
        {"sembol":"PHE","tarih":"2025-12-19","fiyat":1.470761},
        {"sembol":"PRY","tarih":"2025-12-19","fiyat":2.314906},
        {"sembol":"TLY","tarih":"2025-12-19","fiyat":2842.124172},
        {"sembol":"TP2","tarih":"2025-12-19","fiyat":1.584046},
        {"sembol":"PHE","tarih":"2025-12-22","fiyat":1.475148},
        {"sembol":"PRY","tarih":"2025-12-22","fiyat":2.323934},
        {"sembol":"TLY","tarih":"2025-12-22","fiyat":2851.974412},
        {"sembol":"TP2","tarih":"2025-12-22","fiyat":1.590441},
        {"sembol":"PHE","tarih":"2025-12-23","fiyat":1.47063},
        {"sembol":"PRY","tarih":"2025-12-23","fiyat":2.327103},
        {"sembol":"TLY","tarih":"2025-12-23","fiyat":2844.442835},
        {"sembol":"TP2","tarih":"2025-12-23","fiyat":1.592686},
        {"sembol":"PHE","tarih":"2025-12-24","fiyat":1.461765},
        {"sembol":"PRY","tarih":"2025-12-24","fiyat":2.330094},
        {"sembol":"TLY","tarih":"2025-12-24","fiyat":2845.590983},
        {"sembol":"TP2","tarih":"2025-12-24","fiyat":1.594676},
        {"sembol":"PHE","tarih":"2025-12-25","fiyat":1.46582},
        {"sembol":"PRY","tarih":"2025-12-25","fiyat":2.333208},
        {"sembol":"TLY","tarih":"2025-12-25","fiyat":2865.033961},
        {"sembol":"TP2","tarih":"2025-12-25","fiyat":1.596834},
        {"sembol":"PHE","tarih":"2025-12-26","fiyat":1.463633},
        {"sembol":"PRY","tarih":"2025-12-26","fiyat":2.336214},
        {"sembol":"TLY","tarih":"2025-12-26","fiyat":2876.158717},
        {"sembol":"TP2","tarih":"2025-12-26","fiyat":1.598924},
        {"sembol":"PHE","tarih":"2025-12-29","fiyat":1.454851},
        {"sembol":"PRY","tarih":"2025-12-29","fiyat":2.345376},
        {"sembol":"TLY","tarih":"2025-12-29","fiyat":2852.616099},
        {"sembol":"TP2","tarih":"2025-12-29","fiyat":1.605328},
        {"sembol":"PHE","tarih":"2025-12-30","fiyat":1.440791},
        {"sembol":"PRY","tarih":"2025-12-30","fiyat":2.348388},
        {"sembol":"TLY","tarih":"2025-12-30","fiyat":2805.383101},
        {"sembol":"TP2","tarih":"2025-12-30","fiyat":1.607273},
        {"sembol":"PHE","tarih":"2025-12-31","fiyat":1.441649},
        {"sembol":"PRY","tarih":"2025-12-31","fiyat":2.351456},
        {"sembol":"TLY","tarih":"2025-12-31","fiyat":2930.287064},
        {"sembol":"TP2","tarih":"2025-12-31","fiyat":1.609639},
        {"sembol":"PHE","tarih":"2026-01-02","fiyat":1.446508},
        {"sembol":"PRY","tarih":"2026-01-02","fiyat":2.357554},
        {"sembol":"TLY","tarih":"2026-01-02","fiyat":2965.681554},
        {"sembol":"TP2","tarih":"2026-01-02","fiyat":1.613895},
        {"sembol":"PHE","tarih":"2026-01-05","fiyat":1.47852},
        {"sembol":"PRY","tarih":"2026-01-05","fiyat":2.366882},
        {"sembol":"TLY","tarih":"2026-01-05","fiyat":2988.817499},
        {"sembol":"TP2","tarih":"2026-01-05","fiyat":1.620432},
        {"sembol":"PHE","tarih":"2026-01-06","fiyat":1.506734},
        {"sembol":"PRY","tarih":"2026-01-06","fiyat":2.369991},
        {"sembol":"TLY","tarih":"2026-01-06","fiyat":3017.86315},
        {"sembol":"TP2","tarih":"2026-01-06","fiyat":1.62243},
        {"sembol":"PHE","tarih":"2026-01-07","fiyat":1.548783},
        {"sembol":"PRY","tarih":"2026-01-07","fiyat":2.373182},
        {"sembol":"TLY","tarih":"2026-01-07","fiyat":3038.116769},
        {"sembol":"TP2","tarih":"2026-01-07","fiyat":1.624648},
        {"sembol":"PHE","tarih":"2026-01-08","fiyat":1.548363},
        {"sembol":"PRY","tarih":"2026-01-08","fiyat":2.376224},
        {"sembol":"TLY","tarih":"2026-01-08","fiyat":3064.113045},
        {"sembol":"TP2","tarih":"2026-01-08","fiyat":1.626685},
        {"sembol":"PHE","tarih":"2026-01-09","fiyat":1.560691},
        {"sembol":"PRY","tarih":"2026-01-09","fiyat":2.379254},
        {"sembol":"TLY","tarih":"2026-01-09","fiyat":3073.05351},
        {"sembol":"TP2","tarih":"2026-01-09","fiyat":1.62886},
        {"sembol":"PHE","tarih":"2026-01-12","fiyat":1.561153},
        {"sembol":"PRY","tarih":"2026-01-12","fiyat":2.388452},
        {"sembol":"TLY","tarih":"2026-01-12","fiyat":3107.839228},
        {"sembol":"TP2","tarih":"2026-01-12","fiyat":1.635163},
        {"sembol":"PHE","tarih":"2026-01-13","fiyat":1.57177},
        {"sembol":"PRY","tarih":"2026-01-13","fiyat":2.391534},
        {"sembol":"TLY","tarih":"2026-01-13","fiyat":3120.380466},
        {"sembol":"TP2","tarih":"2026-01-13","fiyat":1.637326},
        {"sembol":"PHE","tarih":"2026-01-14","fiyat":1.582967},
        {"sembol":"PRY","tarih":"2026-01-14","fiyat":2.394638},
        {"sembol":"TLY","tarih":"2026-01-14","fiyat":3124.833334},
        {"sembol":"TP2","tarih":"2026-01-14","fiyat":1.639423},
        {"sembol":"PHE","tarih":"2026-01-15","fiyat":1.583653},
        {"sembol":"PRY","tarih":"2026-01-15","fiyat":2.397673},
        {"sembol":"TLY","tarih":"2026-01-15","fiyat":3124.869087},
        {"sembol":"TP2","tarih":"2026-01-15","fiyat":1.641483},
        {"sembol":"PHE","tarih":"2026-01-16","fiyat":1.595676},
        {"sembol":"PRY","tarih":"2026-01-16","fiyat":2.400752},
        {"sembol":"TLY","tarih":"2026-01-16","fiyat":3158.094673},
        {"sembol":"TP2","tarih":"2026-01-16","fiyat":1.643628},
        {"sembol":"PHE","tarih":"2026-01-19","fiyat":1.624664},
        {"sembol":"PRY","tarih":"2026-01-19","fiyat":2.410231},
        {"sembol":"TLY","tarih":"2026-01-19","fiyat":3199.220448},
        {"sembol":"TP2","tarih":"2026-01-19","fiyat":1.650142},
        {"sembol":"PHE","tarih":"2026-01-20","fiyat":1.633544},
        {"sembol":"PRY","tarih":"2026-01-20","fiyat":2.413451},
        {"sembol":"TLY","tarih":"2026-01-20","fiyat":3220.543663},
        {"sembol":"TP2","tarih":"2026-01-20","fiyat":1.652313},
        {"sembol":"PHE","tarih":"2026-01-21","fiyat":1.624118},
        {"sembol":"PRY","tarih":"2026-01-21","fiyat":2.416587},
        {"sembol":"TLY","tarih":"2026-01-21","fiyat":3232.995435},
        {"sembol":"TP2","tarih":"2026-01-21","fiyat":1.654463},
        {"sembol":"PHE","tarih":"2026-01-22","fiyat":1.637386},
        {"sembol":"PRY","tarih":"2026-01-22","fiyat":2.419745},
        {"sembol":"TLY","tarih":"2026-01-22","fiyat":3266.193226},
        {"sembol":"TP2","tarih":"2026-01-22","fiyat":1.656656},
        {"sembol":"PHE","tarih":"2026-01-23","fiyat":1.704261},
        {"sembol":"PRY","tarih":"2026-01-23","fiyat":2.423116},
        {"sembol":"TLY","tarih":"2026-01-23","fiyat":3251.927966},
        {"sembol":"TP2","tarih":"2026-01-23","fiyat":1.658838},
        {"sembol":"PHE","tarih":"2026-01-26","fiyat":1.726028},
        {"sembol":"PRY","tarih":"2026-01-26","fiyat":2.4328},
        {"sembol":"TLY","tarih":"2026-01-26","fiyat":3242.260249},
        {"sembol":"TP2","tarih":"2026-01-26","fiyat":1.66548},
        {"sembol":"PHE","tarih":"2026-01-27","fiyat":1.745297},
        {"sembol":"PRY","tarih":"2026-01-27","fiyat":2.43659},
        {"sembol":"TLY","tarih":"2026-01-27","fiyat":3272.629818},
        {"sembol":"TP2","tarih":"2026-01-27","fiyat":1.668325},
        {"sembol":"PHE","tarih":"2026-01-28","fiyat":1.764306},
        {"sembol":"PRY","tarih":"2026-01-28","fiyat":2.440103},
        {"sembol":"TLY","tarih":"2026-01-28","fiyat":3287.619631},
        {"sembol":"TP2","tarih":"2026-01-28","fiyat":1.670943},
        {"sembol":"PHE","tarih":"2026-01-29","fiyat":1.782468},
        {"sembol":"PRY","tarih":"2026-01-29","fiyat":2.443399},
        {"sembol":"TLY","tarih":"2026-01-29","fiyat":3353.821296},
        {"sembol":"TP2","tarih":"2026-01-29","fiyat":1.673339},
        {"sembol":"PHE","tarih":"2026-01-30","fiyat":1.862308},
        {"sembol":"PRY","tarih":"2026-01-30","fiyat":2.446556},
        {"sembol":"TLY","tarih":"2026-01-30","fiyat":3393.579372},
        {"sembol":"TP2","tarih":"2026-01-30","fiyat":1.675544},
        {"sembol":"PHE","tarih":"2026-02-02","fiyat":1.870723},
        {"sembol":"PRY","tarih":"2026-02-02","fiyat":2.455378},
        {"sembol":"TLY","tarih":"2026-02-02","fiyat":3385.757573},
        {"sembol":"TP2","tarih":"2026-02-02","fiyat":1.681765},
        {"sembol":"PHE","tarih":"2026-02-03","fiyat":1.86655},
        {"sembol":"PRY","tarih":"2026-02-03","fiyat":2.458039},
        {"sembol":"TLY","tarih":"2026-02-03","fiyat":3395.303583},
        {"sembol":"TP2","tarih":"2026-02-03","fiyat":1.683595},
        {"sembol":"PHE","tarih":"2026-02-04","fiyat":1.895986},
        {"sembol":"PRY","tarih":"2026-02-04","fiyat":2.461277},
        {"sembol":"TLY","tarih":"2026-02-04","fiyat":3404.067693},
        {"sembol":"TP2","tarih":"2026-02-04","fiyat":1.68602},
        {"sembol":"PHE","tarih":"2026-02-05","fiyat":1.904818},
        {"sembol":"PRY","tarih":"2026-02-05","fiyat":2.464314},
        {"sembol":"TLY","tarih":"2026-02-05","fiyat":3426.457106},
        {"sembol":"TP2","tarih":"2026-02-05","fiyat":1.688046},
        {"sembol":"PHE","tarih":"2026-02-06","fiyat":1.922399},
        {"sembol":"PRY","tarih":"2026-02-06","fiyat":2.467412},
        {"sembol":"TLY","tarih":"2026-02-06","fiyat":3407.720471},
        {"sembol":"TP2","tarih":"2026-02-06","fiyat":1.690161},
        {"sembol":"PHE","tarih":"2026-02-09","fiyat":1.931635},
        {"sembol":"PRY","tarih":"2026-02-09","fiyat":2.476529},
        {"sembol":"TLY","tarih":"2026-02-09","fiyat":3415.896103},
        {"sembol":"TP2","tarih":"2026-02-09","fiyat":1.696589},
        {"sembol":"PHE","tarih":"2026-02-10","fiyat":1.970458},
        {"sembol":"PRY","tarih":"2026-02-10","fiyat":2.479634},
        {"sembol":"TLY","tarih":"2026-02-10","fiyat":3454.746186},
        {"sembol":"TP2","tarih":"2026-02-10","fiyat":1.698543},
        {"sembol":"PHE","tarih":"2026-02-11","fiyat":2.002924},
        {"sembol":"PRY","tarih":"2026-02-11","fiyat":2.482524},
        {"sembol":"TLY","tarih":"2026-02-11","fiyat":3466.229933},
        {"sembol":"TP2","tarih":"2026-02-11","fiyat":1.700712},
        {"sembol":"PHE","tarih":"2026-02-12","fiyat":2.034847},
        {"sembol":"PRY","tarih":"2026-02-12","fiyat":2.485425},
        {"sembol":"TLY","tarih":"2026-02-12","fiyat":3485.659398},
        {"sembol":"TP2","tarih":"2026-02-12","fiyat":1.70267},
        {"sembol":"PHE","tarih":"2026-02-13","fiyat":2.061393},
        {"sembol":"PRY","tarih":"2026-02-13","fiyat":2.488662},
        {"sembol":"TLY","tarih":"2026-02-13","fiyat":3553.47545},
        {"sembol":"TP2","tarih":"2026-02-13","fiyat":1.704931},
        {"sembol":"PHE","tarih":"2026-02-16","fiyat":2.090189},
        {"sembol":"PRY","tarih":"2026-02-16","fiyat":2.49786},
        {"sembol":"TLY","tarih":"2026-02-16","fiyat":3571.210026},
        {"sembol":"TP2","tarih":"2026-02-16","fiyat":1.711345},
        {"sembol":"PHE","tarih":"2026-02-17","fiyat":2.103045},
        {"sembol":"PRY","tarih":"2026-02-17","fiyat":2.501093},
        {"sembol":"TLY","tarih":"2026-02-17","fiyat":3606.279823},
        {"sembol":"TP2","tarih":"2026-02-17","fiyat":1.713539},
        {"sembol":"PHE","tarih":"2026-02-18","fiyat":2.1259},
        {"sembol":"PRY","tarih":"2026-02-18","fiyat":2.504511},
        {"sembol":"TLY","tarih":"2026-02-18","fiyat":3626.341408},
        {"sembol":"TP2","tarih":"2026-02-18","fiyat":1.715898},
        {"sembol":"PHE","tarih":"2026-02-19","fiyat":2.12509},
        {"sembol":"PRY","tarih":"2026-02-19","fiyat":2.507683},
        {"sembol":"TLY","tarih":"2026-02-19","fiyat":3667.611871},
        {"sembol":"TP2","tarih":"2026-02-19","fiyat":1.718105},
        {"sembol":"PHE","tarih":"2026-02-20","fiyat":2.112341},
        {"sembol":"PRY","tarih":"2026-02-20","fiyat":2.510336},
        {"sembol":"TLY","tarih":"2026-02-20","fiyat":3616.118086},
        {"sembol":"TP2","tarih":"2026-02-20","fiyat":1.719975},
        {"sembol":"PHE","tarih":"2026-02-23","fiyat":2.181667},
        {"sembol":"PRY","tarih":"2026-02-23","fiyat":2.519371},
        {"sembol":"TLY","tarih":"2026-02-23","fiyat":3648.369268},
        {"sembol":"TP2","tarih":"2026-02-23","fiyat":1.726114},
        {"sembol":"PHE","tarih":"2026-02-24","fiyat":2.221405},
        {"sembol":"PRY","tarih":"2026-02-24","fiyat":2.522634},
        {"sembol":"TLY","tarih":"2026-02-24","fiyat":3816.506496},
        {"sembol":"TP2","tarih":"2026-02-24","fiyat":1.728408},
        {"sembol":"PHE","tarih":"2026-02-25","fiyat":2.226113},
        {"sembol":"PRY","tarih":"2026-02-25","fiyat":2.525875},
        {"sembol":"TLY","tarih":"2026-02-25","fiyat":3886.618443},
        {"sembol":"TP2","tarih":"2026-02-25","fiyat":1.730626},
        {"sembol":"PHE","tarih":"2026-02-26","fiyat":2.25081},
        {"sembol":"PRY","tarih":"2026-02-26","fiyat":2.52909},
        {"sembol":"TLY","tarih":"2026-02-26","fiyat":3821.607079},
        {"sembol":"TP2","tarih":"2026-02-26","fiyat":1.732768},
        {"sembol":"PHE","tarih":"2026-02-27","fiyat":2.271802},
        {"sembol":"PRY","tarih":"2026-02-27","fiyat":2.53228},
        {"sembol":"TLY","tarih":"2026-02-27","fiyat":3899.452408},
        {"sembol":"TP2","tarih":"2026-02-27","fiyat":1.734893},
        {"sembol":"PHE","tarih":"2026-03-02","fiyat":2.272566},
        {"sembol":"PRY","tarih":"2026-03-02","fiyat":2.541917},
        {"sembol":"TLY","tarih":"2026-03-02","fiyat":3934.234318},
        {"sembol":"TP2","tarih":"2026-03-02","fiyat":1.741497},
        {"sembol":"PHE","tarih":"2026-03-03","fiyat":2.247888},
        {"sembol":"PRY","tarih":"2026-03-03","fiyat":2.544448},
        {"sembol":"TLY","tarih":"2026-03-03","fiyat":3904.525114},
        {"sembol":"TP2","tarih":"2026-03-03","fiyat":1.743539},
        {"sembol":"PHE","tarih":"2026-03-04","fiyat":2.274028},
        {"sembol":"PRY","tarih":"2026-03-04","fiyat":2.547517},
        {"sembol":"TLY","tarih":"2026-03-04","fiyat":3926.182939},
        {"sembol":"TP2","tarih":"2026-03-04","fiyat":1.745496},
        {"sembol":"PHE","tarih":"2026-03-05","fiyat":2.302507},
        {"sembol":"PRY","tarih":"2026-03-05","fiyat":2.549653},
        {"sembol":"TLY","tarih":"2026-03-05","fiyat":3992.687834},
        {"sembol":"TP2","tarih":"2026-03-05","fiyat":1.74723},
        {"sembol":"PHE","tarih":"2026-03-06","fiyat":2.34874},
        {"sembol":"PRY","tarih":"2026-03-06","fiyat":2.552958},
        {"sembol":"TLY","tarih":"2026-03-06","fiyat":4139.27297},
        {"sembol":"TP2","tarih":"2026-03-06","fiyat":1.749075},
        {"sembol":"PHE","tarih":"2026-03-09","fiyat":2.330562},
        {"sembol":"PRY","tarih":"2026-03-09","fiyat":2.562793},
        {"sembol":"TLY","tarih":"2026-03-09","fiyat":4157.909345},
        {"sembol":"TP2","tarih":"2026-03-09","fiyat":1.755752},
        {"sembol":"PHE","tarih":"2026-03-10","fiyat":2.351917},
        {"sembol":"PRY","tarih":"2026-03-10","fiyat":2.564088},
        {"sembol":"TLY","tarih":"2026-03-10","fiyat":4141.572008},
        {"sembol":"TP2","tarih":"2026-03-10","fiyat":1.756878},
        {"sembol":"PHE","tarih":"2026-03-11","fiyat":2.398264},
        {"sembol":"PRY","tarih":"2026-03-11","fiyat":2.568076},
        {"sembol":"TLY","tarih":"2026-03-11","fiyat":4269.92972},
        {"sembol":"TP2","tarih":"2026-03-11","fiyat":1.759662},
        {"sembol":"PHE","tarih":"2026-03-12","fiyat":2.397065},
        {"sembol":"PRY","tarih":"2026-03-12","fiyat":2.571041},
        {"sembol":"TLY","tarih":"2026-03-12","fiyat":4308.710024},
        {"sembol":"TP2","tarih":"2026-03-12","fiyat":1.761846},
        {"sembol":"PHE","tarih":"2026-03-13","fiyat":2.413382},
        {"sembol":"PRY","tarih":"2026-03-13","fiyat":2.573755},
        {"sembol":"TLY","tarih":"2026-03-13","fiyat":4321.293141},
        {"sembol":"TP2","tarih":"2026-03-13","fiyat":1.763986},
        {"sembol":"PHE","tarih":"2026-03-16","fiyat":2.393792},
        {"sembol":"PRY","tarih":"2026-03-16","fiyat":2.583297},
        {"sembol":"TLY","tarih":"2026-03-16","fiyat":4296.755184},
        {"sembol":"TP2","tarih":"2026-03-16","fiyat":1.769968},
        {"sembol":"PHE","tarih":"2026-03-17","fiyat":2.398859},
        {"sembol":"PRY","tarih":"2026-03-17","fiyat":2.586469},
        {"sembol":"TLY","tarih":"2026-03-17","fiyat":4288.244306},
        {"sembol":"TP2","tarih":"2026-03-17","fiyat":1.77213},
        {"sembol":"PHE","tarih":"2026-03-18","fiyat":2.426963},
        {"sembol":"PRY","tarih":"2026-03-18","fiyat":2.589958},
        {"sembol":"TLY","tarih":"2026-03-18","fiyat":4372.563125},
        {"sembol":"TP2","tarih":"2026-03-18","fiyat":1.774494},
        {"sembol":"PHE","tarih":"2026-03-19","fiyat":2.431588},
        {"sembol":"PRY","tarih":"2026-03-19","fiyat":2.594431},
        {"sembol":"TLY","tarih":"2026-03-19","fiyat":4430.303237},
        {"sembol":"TP2","tarih":"2026-03-19","fiyat":1.778038},
        {"sembol":"PHE","tarih":"2026-03-23","fiyat":2.40375},
        {"sembol":"PRY","tarih":"2026-03-23","fiyat":2.607994},
        {"sembol":"TLY","tarih":"2026-03-23","fiyat":4443.164878},
        {"sembol":"TP2","tarih":"2026-03-23","fiyat":1.787262},
        {"sembol":"PHE","tarih":"2026-03-24","fiyat":2.461937},
        {"sembol":"PRY","tarih":"2026-03-24","fiyat":2.610862},
        {"sembol":"TLY","tarih":"2026-03-24","fiyat":4490.202602},
        {"sembol":"TP2","tarih":"2026-03-24","fiyat":1.789307},
        {"sembol":"PHE","tarih":"2026-03-25","fiyat":2.498236},
        {"sembol":"PRY","tarih":"2026-03-25","fiyat":2.614361},
        {"sembol":"TLY","tarih":"2026-03-25","fiyat":4533.997619},
        {"sembol":"TP2","tarih":"2026-03-25","fiyat":1.791535},
        {"sembol":"PHE","tarih":"2026-03-26","fiyat":2.546488},
        {"sembol":"PRY","tarih":"2026-03-26","fiyat":2.617881},
        {"sembol":"TLY","tarih":"2026-03-26","fiyat":4581.357473},
        {"sembol":"TP2","tarih":"2026-03-26","fiyat":1.793869},
        {"sembol":"PHE","tarih":"2026-03-27","fiyat":2.562147},
        {"sembol":"PRY","tarih":"2026-03-27","fiyat":2.621012},
        {"sembol":"TLY","tarih":"2026-03-27","fiyat":4605.144851},
        {"sembol":"TP2","tarih":"2026-03-27","fiyat":1.795948},
        {"sembol":"PHE","tarih":"2026-03-30","fiyat":2.572269},
        {"sembol":"PRY","tarih":"2026-03-30","fiyat":2.630968},
        {"sembol":"TLY","tarih":"2026-03-30","fiyat":4630.814629},
        {"sembol":"TP2","tarih":"2026-03-30","fiyat":1.80295},
        {"sembol":"PHE","tarih":"2026-03-31","fiyat":2.580167},
        {"sembol":"PRY","tarih":"2026-03-31","fiyat":2.633699},
        {"sembol":"TLY","tarih":"2026-03-31","fiyat":4638.074312},
        {"sembol":"TP2","tarih":"2026-03-31","fiyat":1.804633},
        {"sembol":"PHE","tarih":"2026-04-01","fiyat":2.606992},
        {"sembol":"PRY","tarih":"2026-04-01","fiyat":2.637275},
        {"sembol":"TLY","tarih":"2026-04-01","fiyat":4686.836903},
        {"sembol":"TP2","tarih":"2026-04-01","fiyat":1.806812},
        {"sembol":"PHE","tarih":"2026-04-02","fiyat":2.622766},
        {"sembol":"PRY","tarih":"2026-04-02","fiyat":2.641179},
        {"sembol":"TLY","tarih":"2026-04-02","fiyat":4718.012403},
        {"sembol":"TP2","tarih":"2026-04-02","fiyat":1.809584},
        {"sembol":"PHE","tarih":"2026-04-03","fiyat":2.638986},
        {"sembol":"PRY","tarih":"2026-04-03","fiyat":2.644617},
        {"sembol":"TLY","tarih":"2026-04-03","fiyat":4737.764674},
        {"sembol":"TP2","tarih":"2026-04-03","fiyat":1.811925},
        {"sembol":"PHE","tarih":"2026-04-06","fiyat":2.631541},
        {"sembol":"PRY","tarih":"2026-04-06","fiyat":2.65499},
        {"sembol":"TLY","tarih":"2026-04-06","fiyat":4750.269563},
        {"sembol":"TP2","tarih":"2026-04-06","fiyat":1.818912},
        {"sembol":"PHE","tarih":"2026-04-07","fiyat":2.648981},
        {"sembol":"PRY","tarih":"2026-04-07","fiyat":2.658404},
        {"sembol":"TLY","tarih":"2026-04-07","fiyat":4817.795883},
        {"sembol":"TP2","tarih":"2026-04-07","fiyat":1.821398},
        {"sembol":"PHE","tarih":"2026-04-08","fiyat":2.638286},
        {"sembol":"PRY","tarih":"2026-04-08","fiyat":2.661722},
        {"sembol":"TLY","tarih":"2026-04-08","fiyat":4817.261106},
        {"sembol":"TP2","tarih":"2026-04-08","fiyat":1.823738},
        {"sembol":"PHE","tarih":"2026-04-09","fiyat":2.725876},
        {"sembol":"PRY","tarih":"2026-04-09","fiyat":2.666445},
        {"sembol":"TLY","tarih":"2026-04-09","fiyat":4891.196475},
        {"sembol":"TP2","tarih":"2026-04-09","fiyat":1.827211},
        {"sembol":"PHE","tarih":"2026-04-10","fiyat":2.722281},
        {"sembol":"PRY","tarih":"2026-04-10","fiyat":2.670574},
        {"sembol":"TLY","tarih":"2026-04-10","fiyat":4929.565236},
        {"sembol":"TP2","tarih":"2026-04-10","fiyat":1.829682},
        {"sembol":"PHE","tarih":"2026-04-13","fiyat":2.768049},
        {"sembol":"PRY","tarih":"2026-04-13","fiyat":2.681204},
        {"sembol":"TLY","tarih":"2026-04-13","fiyat":4986.560092},
        {"sembol":"TP2","tarih":"2026-04-13","fiyat":1.836915},
        {"sembol":"PHE","tarih":"2026-04-14","fiyat":2.770983},
        {"sembol":"PRY","tarih":"2026-04-14","fiyat":2.684218},
        {"sembol":"TLY","tarih":"2026-04-14","fiyat":4965.152719},
        {"sembol":"TP2","tarih":"2026-04-14","fiyat":1.839223},
        {"sembol":"PHE","tarih":"2026-04-15","fiyat":2.765117},
        {"sembol":"PRY","tarih":"2026-04-15","fiyat":2.688726},
        {"sembol":"TLY","tarih":"2026-04-15","fiyat":5016.999844},
        {"sembol":"TP2","tarih":"2026-04-15","fiyat":1.842151},
        {"sembol":"PHE","tarih":"2026-04-16","fiyat":2.773872},
        {"sembol":"PRY","tarih":"2026-04-16","fiyat":2.693099},
        {"sembol":"TLY","tarih":"2026-04-16","fiyat":5030.556871},
        {"sembol":"TP2","tarih":"2026-04-16","fiyat":1.84518},
        {"sembol":"PHE","tarih":"2026-04-17","fiyat":2.767785},
        {"sembol":"PRY","tarih":"2026-04-17","fiyat":2.696862},
        {"sembol":"TLY","tarih":"2026-04-17","fiyat":5057.675737},
        {"sembol":"TP2","tarih":"2026-04-17","fiyat":1.847653},
        {"sembol":"PHE","tarih":"2026-04-20","fiyat":2.822538},
        {"sembol":"PRY","tarih":"2026-04-20","fiyat":2.706947},
        {"sembol":"TLY","tarih":"2026-04-20","fiyat":5142.418363},
        {"sembol":"TP2","tarih":"2026-04-20","fiyat":1.854466},
        {"sembol":"PHE","tarih":"2026-04-21","fiyat":2.828791},
        {"sembol":"PRY","tarih":"2026-04-21","fiyat":2.710433},
        {"sembol":"TLY","tarih":"2026-04-21","fiyat":5087.722908},
        {"sembol":"TP2","tarih":"2026-04-21","fiyat":1.856793},
        {"sembol":"PHE","tarih":"2026-04-22","fiyat":2.830321},
        {"sembol":"PRY","tarih":"2026-04-22","fiyat":2.713819},
        {"sembol":"TLY","tarih":"2026-04-22","fiyat":5099.616212},
        {"sembol":"TP2","tarih":"2026-04-22","fiyat":1.859155},
        {"sembol":"PHE","tarih":"2026-04-24","fiyat":2.82182},
        {"sembol":"PRY","tarih":"2026-04-24","fiyat":2.720292},
        {"sembol":"TLY","tarih":"2026-04-24","fiyat":5097.623979},
        {"sembol":"TP2","tarih":"2026-04-24","fiyat":1.863613},
        {"sembol":"PHE","tarih":"2026-04-27","fiyat":2.828884},
        {"sembol":"PRY","tarih":"2026-04-27","fiyat":2.730206},
        {"sembol":"TLY","tarih":"2026-04-27","fiyat":5118.660651},
        {"sembol":"TP2","tarih":"2026-04-27","fiyat":1.870506},
        {"sembol":"PHE","tarih":"2026-04-28","fiyat":2.856028},
        {"sembol":"PRY","tarih":"2026-04-28","fiyat":2.733715},
        {"sembol":"TLY","tarih":"2026-04-28","fiyat":5216.385597},
        {"sembol":"TP2","tarih":"2026-04-28","fiyat":1.872743},
        {"sembol":"PHE","tarih":"2026-04-29","fiyat":2.817793},
        {"sembol":"PRY","tarih":"2026-04-29","fiyat":2.736998},
        {"sembol":"TLY","tarih":"2026-04-29","fiyat":5213.487578},
        {"sembol":"TP2","tarih":"2026-04-29","fiyat":1.87505},
        {"sembol":"PHE","tarih":"2026-04-30","fiyat":2.816142},
        {"sembol":"PRY","tarih":"2026-04-30","fiyat":2.740376},
        {"sembol":"TLY","tarih":"2026-04-30","fiyat":5223.812438},
        {"sembol":"TP2","tarih":"2026-04-30","fiyat":1.877419},
        {"sembol":"PHE","tarih":"2026-05-04","fiyat":2.858748},
        {"sembol":"PRY","tarih":"2026-05-04","fiyat":2.753907},
        {"sembol":"TLY","tarih":"2026-05-04","fiyat":5265.226726},
        {"sembol":"TP2","tarih":"2026-05-04","fiyat":1.886536},
        {"sembol":"PHE","tarih":"2026-05-05","fiyat":2.855704},
        {"sembol":"PRY","tarih":"2026-05-05","fiyat":2.757151},
        {"sembol":"TLY","tarih":"2026-05-05","fiyat":5270.988891},
        {"sembol":"TP2","tarih":"2026-05-05","fiyat":1.888844},
        {"sembol":"PHE","tarih":"2026-05-06","fiyat":2.871541},
        {"sembol":"PRY","tarih":"2026-05-06","fiyat":2.760498},
        {"sembol":"TLY","tarih":"2026-05-06","fiyat":5319.937078},
        {"sembol":"TP2","tarih":"2026-05-06","fiyat":1.89125},
        {"sembol":"PHE","tarih":"2026-05-07","fiyat":2.893337},
        {"sembol":"PRY","tarih":"2026-05-07","fiyat":2.764651},
        {"sembol":"TLY","tarih":"2026-05-07","fiyat":5381.651019},
        {"sembol":"TP2","tarih":"2026-05-07","fiyat":1.89437},
        {"sembol":"PHE","tarih":"2026-05-08","fiyat":2.919987},
        {"sembol":"PRY","tarih":"2026-05-08","fiyat":2.769201},
        {"sembol":"TLY","tarih":"2026-05-08","fiyat":5431.088422},
        {"sembol":"TP2","tarih":"2026-05-08","fiyat":1.897681},
        {"sembol":"PHE","tarih":"2026-05-11","fiyat":2.912932},
        {"sembol":"PRY","tarih":"2026-05-11","fiyat":2.779245},
        {"sembol":"TLY","tarih":"2026-05-11","fiyat":5429.254939},
        {"sembol":"TP2","tarih":"2026-05-11","fiyat":1.904709},
        {"sembol":"PHE","tarih":"2026-05-12","fiyat":2.92482},
        {"sembol":"PRY","tarih":"2026-05-12","fiyat":2.782349},
        {"sembol":"TLY","tarih":"2026-05-12","fiyat":5487.503657},
        {"sembol":"TP2","tarih":"2026-05-12","fiyat":1.907},
        {"sembol":"PHE","tarih":"2026-05-13","fiyat":2.932508},
        {"sembol":"PRY","tarih":"2026-05-13","fiyat":2.785663},
        {"sembol":"TLY","tarih":"2026-05-13","fiyat":5508.739481},
        {"sembol":"TP2","tarih":"2026-05-13","fiyat":1.90897},
        {"sembol":"PHE","tarih":"2026-05-14","fiyat":2.940961},
        {"sembol":"PRY","tarih":"2026-05-14","fiyat":2.789579},
        {"sembol":"TLY","tarih":"2026-05-14","fiyat":5489.550424},
        {"sembol":"TP2","tarih":"2026-05-14","fiyat":1.91169},
        {"sembol":"PHE","tarih":"2026-05-15","fiyat":2.947942},
        {"sembol":"PRY","tarih":"2026-05-15","fiyat":2.793582},
        {"sembol":"TLY","tarih":"2026-05-15","fiyat":5407.951202},
        {"sembol":"TP2","tarih":"2026-05-15","fiyat":1.914269},
        {"sembol":"PHE","tarih":"2026-05-18","fiyat":2.88907},
        {"sembol":"PRY","tarih":"2026-05-18","fiyat":2.804436},
        {"sembol":"TLY","tarih":"2026-05-18","fiyat":5488.8942},
        {"sembol":"TP2","tarih":"2026-05-18","fiyat":1.921661},
        {"sembol":"PHE","tarih":"2026-05-20","fiyat":2.891955},
        {"sembol":"PRY","tarih":"2026-05-20","fiyat":2.811198},
        {"sembol":"TLY","tarih":"2026-05-20","fiyat":5516.308655},
        {"sembol":"TP2","tarih":"2026-05-20","fiyat":1.926364},
        {"sembol":"PHE","tarih":"2026-05-21","fiyat":2.905221},
        {"sembol":"PRY","tarih":"2026-05-21","fiyat":2.814588},
        {"sembol":"TLY","tarih":"2026-05-21","fiyat":5518.513245},
        {"sembol":"TP2","tarih":"2026-05-21","fiyat":1.928635},
        {"sembol":"PHE","tarih":"2026-05-22","fiyat":2.778949},
        {"sembol":"PRY","tarih":"2026-05-22","fiyat":2.818197},
        {"sembol":"TLY","tarih":"2026-05-22","fiyat":5285.613},
        {"sembol":"TP2","tarih":"2026-05-22","fiyat":1.931111},
        {"sembol":"PHE","tarih":"2026-05-25","fiyat":2.827022},
        {"sembol":"PRY","tarih":"2026-05-25","fiyat":2.828177},
        {"sembol":"TLY","tarih":"2026-05-25","fiyat":5479.810701},
        {"sembol":"TP2","tarih":"2026-05-25","fiyat":1.938056},
        {"sembol":"PHE","tarih":"2026-05-26","fiyat":2.84544},
        {"sembol":"PRY","tarih":"2026-05-26","fiyat":2.831813},
        {"sembol":"TLY","tarih":"2026-05-26","fiyat":5552.06271},
        {"sembol":"TP2","tarih":"2026-05-26","fiyat":1.940285},
        {"sembol":"PHE","tarih":"2026-06-01","fiyat":2.86433},
        {"sembol":"PRY","tarih":"2026-06-01","fiyat":2.854022},
        {"sembol":"TLY","tarih":"2026-06-01","fiyat":5670.943028},
        {"sembol":"TP2","tarih":"2026-06-01","fiyat":1.955275},
        {"sembol":"PHE","tarih":"2026-06-02","fiyat":2.899997},
        {"sembol":"PRY","tarih":"2026-06-02","fiyat":2.857961},
        {"sembol":"TLY","tarih":"2026-06-02","fiyat":5616.323305},
        {"sembol":"TP2","tarih":"2026-06-02","fiyat":1.958375},
        {"sembol":"PHE","tarih":"2026-06-03","fiyat":3.008753},
        {"sembol":"PRY","tarih":"2026-06-03","fiyat":2.861448},
        {"sembol":"TLY","tarih":"2026-06-03","fiyat":5694.968942},
        {"sembol":"TP2","tarih":"2026-06-03","fiyat":1.961022},
        {"sembol":"PHE","tarih":"2026-06-04","fiyat":3.048807},
        {"sembol":"PRY","tarih":"2026-06-04","fiyat":2.865226},
        {"sembol":"TLY","tarih":"2026-06-04","fiyat":5781.730326},
        {"sembol":"TP2","tarih":"2026-06-04","fiyat":1.96339},
        {"sembol":"PHE","tarih":"2026-06-05","fiyat":3.10418},
        {"sembol":"PRY","tarih":"2026-06-05","fiyat":2.868954},
        {"sembol":"TLY","tarih":"2026-06-05","fiyat":5811.277955},
        {"sembol":"TP2","tarih":"2026-06-05","fiyat":1.965988},
        {"sembol":"PHE","tarih":"2026-06-08","fiyat":3.143424},
        {"sembol":"PRY","tarih":"2026-06-08","fiyat":2.880051},
        {"sembol":"TLY","tarih":"2026-06-08","fiyat":5821.33936},
        {"sembol":"TP2","tarih":"2026-06-08","fiyat":1.973559},
        {"sembol":"PHE","tarih":"2026-06-09","fiyat":3.236871},
        {"sembol":"PRY","tarih":"2026-06-09","fiyat":2.883597},
        {"sembol":"TLY","tarih":"2026-06-09","fiyat":5908.973645},
        {"sembol":"TP2","tarih":"2026-06-09","fiyat":1.976129},
        {"sembol":"PHE","tarih":"2026-06-10","fiyat":3.269763},
        {"sembol":"PRY","tarih":"2026-06-10","fiyat":2.887302},
        {"sembol":"TLY","tarih":"2026-06-10","fiyat":6009.373064},
        {"sembol":"TP2","tarih":"2026-06-10","fiyat":1.978683},
        {"sembol":"PHE","tarih":"2026-06-11","fiyat":3.292996},
        {"sembol":"PRY","tarih":"2026-06-11","fiyat":2.891147},
        {"sembol":"TLY","tarih":"2026-06-11","fiyat":6063.543833},
        {"sembol":"TP2","tarih":"2026-06-11","fiyat":1.981275},
        {"sembol":"PHE","tarih":"2026-06-12","fiyat":3.329608},
        {"sembol":"PRY","tarih":"2026-06-12","fiyat":2.894925},
        {"sembol":"TLY","tarih":"2026-06-12","fiyat":6186.818441},
        {"sembol":"TP2","tarih":"2026-06-12","fiyat":1.983827},
        {"sembol":"PHE","tarih":"2026-06-15","fiyat":3.405477},
        {"sembol":"PRY","tarih":"2026-06-15","fiyat":2.906405},
        {"sembol":"TLY","tarih":"2026-06-15","fiyat":6248.883328},
        {"sembol":"TP2","tarih":"2026-06-15","fiyat":1.991681},
        {"sembol":"PHE","tarih":"2026-06-16","fiyat":3.49162},
        {"sembol":"PRY","tarih":"2026-06-16","fiyat":2.910589},
        {"sembol":"TLY","tarih":"2026-06-16","fiyat":6449.186448},
        {"sembol":"TP2","tarih":"2026-06-16","fiyat":1.994665},
        {"sembol":"PHE","tarih":"2026-06-17","fiyat":3.52699},
        {"sembol":"PRY","tarih":"2026-06-17","fiyat":2.914402},
        {"sembol":"TLY","tarih":"2026-06-17","fiyat":6523.1441},
        {"sembol":"TP2","tarih":"2026-06-17","fiyat":1.997203},
        {"sembol":"PHE","tarih":"2026-06-18","fiyat":3.563461},
        {"sembol":"PRY","tarih":"2026-06-18","fiyat":2.918355},
        {"sembol":"TLY","tarih":"2026-06-18","fiyat":6493.191364},
        {"sembol":"TP2","tarih":"2026-06-18","fiyat":1.999455},
    ]

    if request.method == "POST":
        key = request.form.get("key","")
        if not IMPORT_KEY or key != IMPORT_KEY:
            flash("Import key hatalı.", "error")
            return redirect(url_for("import_fiyatlar"))

        eklenen = 0
        with get_db() as conn:
            for f in excel_fiyatlar:
                conn.execute("""
                    INSERT OR REPLACE INTO fiyat_gecmisi (sembol, tarih, fiyat)
                    VALUES (?,?,?)
                """, (f["sembol"], f["tarih"], f["fiyat"]))
                eklenen += 1

        flash(f"✅ {eklenen} fiyat kaydı yüklendi!", "success")
        return redirect(url_for("fiyatlar"))

    return render_template("import_fiyatlar.html", count=len(excel_fiyatlar))

@app.route("/api/portfoy")
@login_required
def api_portfoy():
    portfoy = hesapla_portfoy(session["user_id"])
    return jsonify(portfoy)

@app.route("/api/fiyat-guncelle-manuel", methods=["POST"])
@login_required
def fiyat_guncelle_manuel():
    """Dashboard butonu ile anlık fiyat güncelleme — oturum açmış kullanıcı için."""
    with get_db() as conn:
        tum = conn.execute("SELECT DISTINCT sembol, tur FROM islemler WHERE user_id=?",
                           (session["user_id"],)).fetchall()
    fon_sembolleri  = [r["sembol"] for r in tum if r["tur"] == "FON"]
    hisse_sembolleri = [r["sembol"] for r in tum if r["tur"] in ("ABD", "BIST", "HISSE")]
    tur_map = {r["sembol"]: r["tur"] for r in tum}

    sonuc = fetch_all_prices(fon_sembolleri, hisse_sembolleri, tur_map=tur_map)

    basarili = 0
    for sembol, tarih, fiyat in sonuc.get("prices", []):
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO fiyat_gecmisi (sembol, tarih, fiyat)
                VALUES (?,?,?)
            """, (sembol, tarih, fiyat))
        basarili += 1

    return jsonify({"ok": True, "guncellenen": basarili, "kaynak": sonuc.get("method","?")})


@app.route("/cron/guncelle")
def cron_guncelle():
    """
    Render Cron Job bu endpoint'i çağırır.
    CRON_KEY env var ile korunur.
    """
    import os
    key = request.args.get("key","")
    if key != os.environ.get("CRON_KEY",""):
        return "yetkisiz", 403

    with get_db() as conn:
        tum = conn.execute("SELECT DISTINCT sembol, tur FROM islemler").fetchall()

    fon_sembolleri  = [r["sembol"] for r in tum if r["tur"] == "FON"]
    hisse_sembolleri = [r["sembol"] for r in tum if r["tur"] in ("ABD", "BIST", "HISSE")]
    tur_map = {r["sembol"]: r["tur"] for r in tum}

    sonuc = fetch_all_prices(fon_sembolleri, hisse_sembolleri, tur_map=tur_map)

    basarili = 0
    for sembol, tarih, fiyat in sonuc.get("prices", []):
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO fiyat_gecmisi (sembol, tarih, fiyat)
                VALUES (?,?,?)
            """, (sembol, tarih, fiyat))
        basarili += 1

    simdi = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO price_fetch_log (tarih, sonuc, detay)
            VALUES (?,?,?)
        """, (simdi, sonuc.get("method","?"),
              f"{basarili} fiyat güncellendi (cron). {sonuc.get('errors','')}"))

    return f"OK: {basarili} fiyat güncellendi. {sonuc.get('method')}", 200


@app.route("/cron/backfill")
def cron_backfill():
    """Backfill — fon başına tek API çağrısıyla 60 günlük veri çeker."""
    import os, time as tm
    from price_fetcher import fetch_fon_aralik
    from datetime import date, timedelta

    key = request.args.get("key","")
    if key != os.environ.get("CRON_KEY",""):
        return "yetkisiz", 403

    with get_db() as conn:
        fon_sembolleri = list(set(r["sembol"] for r in conn.execute(
            "SELECT DISTINCT sembol FROM islemler WHERE tur=\'FON\'").fetchall()))

    if not fon_sembolleri:
        return "Fon yok", 200

    bugun_d = date.today()
    baslangic = bugun_d - timedelta(days=60)

    # Tek API çağrısıyla tüm aralığı çek (fon başına ~2-3 istek)
    tum_veriler = fetch_fon_aralik(fon_sembolleri, baslangic, bugun_d)

    eklenen = 0
    log_lines = [f"Fon: {fon_sembolleri}, {baslangic} - {bugun_d}"]

    for (sembol, tarih_str), fiyat in sorted(tum_veriler.items()):
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                (sembol, tarih_str, fiyat))
        eklenen += 1
        log_lines.append(f"OK {sembol} {tarih_str}: {fiyat}")

    simdi = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("INSERT INTO price_fetch_log (tarih,sonuc,detay) VALUES (?,?,?)",
                     (simdi, "Backfill-TEFAS", f"{eklenen} fiyat dolduruldu (60 gun)"))

    return "\n".join(log_lines), 200


if __name__ == "__main__":
    init_db()
    app.run(debug=True)

# ── Sembol Arama ──────────────────────────────────────────────────────────────

# ── Getiri Kıyaslama ─────────────────────────────────────────────────────────

@app.route("/kiyaslama")
@login_required
def kiyaslama():
    user_id = session["user_id"]
    with get_db() as conn:
        portfoyler = conn.execute(
            "SELECT * FROM kiyaslama_portfoy WHERE user_id=? ORDER BY sira", (user_id,)
        ).fetchall()
        kalemler = {}
        for p in portfoyler:
            kalemler[p["id"]] = conn.execute(
                "SELECT * FROM kiyaslama_kalem WHERE portfoy_id=?", (p["id"],)
            ).fetchall()
    bugun_str = str(bugun())
    with get_db() as conn:
        gt = conn.execute("SELECT ilk_tarih, son_tarih FROM kiyaslama_global_tarih WHERE user_id=?",
                          (user_id,)).fetchone()
    try:
        toplam_para_gt = gt["toplam_para"] if gt else 0
    except (IndexError, KeyError):
        toplam_para_gt = 0
    global_tarih = {"ilk": gt["ilk_tarih"] if gt else "", "son": gt["son_tarih"] if gt else bugun_str, "toplam_para": toplam_para_gt}
    return render_template("kiyaslama.html",
        portfoyler=portfoyler, kalemler=kalemler, bugun=bugun_str, global_tarih=global_tarih)


@app.route("/kiyaslama/tarih-guncelle", methods=["POST"])
@login_required
def kiyaslama_tarih_guncelle():
    """Global tarihi kaydet ve tüm portföylerin fiyatlarını güncelle."""
    from price_fetcher import tefas_aralik_cek, fetch_hisse_toplu, fetch_hisse_fiyatlari
    user_id = session["user_id"]
    ilk_tarih = request.form["ilk_tarih"]
    son_tarih = request.form["son_tarih"]
    toplam_para_str = request.form.get("toplam_para", "0").replace(".", "").replace(",", ".").strip()
    try:
        toplam_para = float(toplam_para_str) if toplam_para_str else 0
    except:
        toplam_para = 0

    # Global tarihi ve toplam parayı kaydet
    with get_db() as conn:
        conn.execute("""
            INSERT INTO kiyaslama_global_tarih (user_id, ilk_tarih, son_tarih, toplam_para)
            VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET ilk_tarih=excluded.ilk_tarih,
                son_tarih=excluded.son_tarih, toplam_para=excluded.toplam_para
        """, (user_id, ilk_tarih, son_tarih, toplam_para))
        # Tüm portföylerin tarihlerini ve toplam parasını güncelle
        conn.execute("""
            UPDATE kiyaslama_portfoy SET ilk_tarih=?, son_tarih=?, toplam_para=? WHERE user_id=?
        """, (ilk_tarih, son_tarih, toplam_para, user_id))
        portfoyler = conn.execute(
            "SELECT id FROM kiyaslama_portfoy WHERE user_id=?", (user_id,)
        ).fetchall()

    from datetime import date as _date
    ilk_date = _date.fromisoformat(ilk_tarih)
    son_date = _date.fromisoformat(son_tarih)
    bugun_str = str(bugun())

    def _piyasa(sembol):
        with get_db() as c:
            r = c.execute("SELECT piyasa FROM semboller WHERE kod=? LIMIT 1", (sembol,)).fetchone()
            if r: return r["piyasa"]
            r2 = c.execute("SELECT tur FROM islemler WHERE sembol=? LIMIT 1", (sembol,)).fetchone()
            return r2["tur"] if r2 else "BIST"

    def _tam_fiyat(sembol, tarih):
        with get_db() as c:
            r = c.execute("SELECT fiyat FROM fiyat_gecmisi WHERE sembol=? AND tarih=?",
                          (sembol, tarih)).fetchone()
            return r["fiyat"] if r else None

    for p in portfoyler:
        with get_db() as conn:
            kalemler = conn.execute(
                "SELECT * FROM kiyaslama_kalem WHERE portfoy_id=?", (p["id"],)
            ).fetchall()

        fonlar = [k for k in kalemler if _piyasa(k["sembol"]) == "FON"]
        hisseler = [k for k in kalemler if _piyasa(k["sembol"]) != "FON"]

        # FON fiyatları TEFAS'tan
        for k in fonlar:
            s = k["sembol"]
            ilk_f = _tam_fiyat(s, ilk_tarih)
            son_f = _tam_fiyat(s, son_tarih)
            tarihler = set()
            if not ilk_f: tarihler.add(ilk_date)
            if not son_f: tarihler.add(son_date)
            if tarihler:
                aralik = tefas_aralik_cek(s, min(tarihler), max(tarihler))
                if not ilk_f and ilk_date in aralik:
                    ilk_f = aralik[ilk_date]
                    with get_db() as conn:
                        conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                     (s, ilk_tarih, ilk_f))
                if not son_f:
                    # Sadece tam tarih eşleşmesinde yaz, farklı tarihin fiyatını uydurma
                    if son_date in aralik:
                        son_f = aralik[son_date]
                        with get_db() as conn:
                            conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                         (s, son_tarih, son_f))
            if ilk_f:
                with get_db() as conn:
                    conn.execute("UPDATE kiyaslama_kalem SET ilk_fiyat=? WHERE id=?", (ilk_f, k["id"]))
            if son_f:
                with get_db() as conn:
                    conn.execute("UPDATE kiyaslama_kalem SET son_fiyat=? WHERE id=?", (son_f, k["id"]))

        # Hisse fiyatları toplu Yahoo
        if hisseler:
            tur_map = {k["sembol"]: _piyasa(k["sembol"]) for k in hisseler}
            prices = fetch_hisse_toplu([k["sembol"] for k in hisseler], tur_map=tur_map)
            for k in hisseler:
                s = k["sembol"]
                ilk_f = _tam_fiyat(s, ilk_tarih)
                son_f = prices.get(s)
                if son_f:
                    with get_db() as conn:
                        conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                     (s, son_tarih, son_f))
                        conn.execute("UPDATE kiyaslama_kalem SET son_fiyat=? WHERE id=?", (son_f, k["id"]))
                if ilk_f:
                    with get_db() as conn:
                        conn.execute("UPDATE kiyaslama_kalem SET ilk_fiyat=? WHERE id=?", (ilk_f, k["id"]))

    flash("Tüm portföyler güncellendi.", "success")
    return redirect(url_for("kiyaslama"))


@app.route("/kiyaslama/portfoy-ekle", methods=["POST"])
@login_required
def kiyaslama_portfoy_ekle():
    user_id = session["user_id"]
    with get_db() as conn:
        sayi = conn.execute(
            "SELECT COUNT(*) as c FROM kiyaslama_portfoy WHERE user_id=?", (user_id,)
        ).fetchone()["c"]
        if sayi >= 4:
            flash("En fazla 4 portföy oluşturabilirsiniz.", "error")
            return redirect(url_for("kiyaslama"))
        ad = request.form["ad"].strip()
        with get_db() as c2:
            gt = c2.execute("SELECT ilk_tarih, son_tarih, toplam_para FROM kiyaslama_global_tarih WHERE user_id=?",
                           (user_id,)).fetchone()
        ilk_tarih = gt["ilk_tarih"] if gt else ""
        son_tarih = gt["son_tarih"] if gt else ""
        toplam_para = gt["toplam_para"] if gt else 0
        conn.execute(
            "INSERT INTO kiyaslama_portfoy (user_id,ad,ilk_tarih,son_tarih,toplam_para,sira) VALUES (?,?,?,?,?,?)",
            (user_id, ad, ilk_tarih, son_tarih, toplam_para, sayi)
        )
    return redirect(url_for("kiyaslama"))


@app.route("/kiyaslama/portfoy-duzenle", methods=["POST"])
@login_required
def kiyaslama_portfoy_duzenle():
    from price_fetcher import fetch_fon_fiyatlari as _tefas, fetch_hisse_fiyatlari as _yahoo

    def _piyasa_bul(sembol):
        with get_db() as c:
            r = c.execute("SELECT piyasa FROM semboller WHERE kod=? LIMIT 1", (sembol,)).fetchone()
            if r: return r["piyasa"]
            r2 = c.execute("SELECT tur FROM islemler WHERE sembol=? LIMIT 1", (sembol,)).fetchone()
            return r2["tur"] if r2 else "BIST"

    def _fiyat_cek(sembol, tarih):
        f = get_fiyat(sembol, tarih)
        if f: return f
        piyasa = _piyasa_bul(sembol)
        if piyasa == "FON":
            prices, _ = _tefas([sembol])
            return prices.get(sembol)
        else:
            tur_map = {sembol: piyasa}
            prices, _ = _yahoo([sembol], tur_map=tur_map)
            gun_dict = prices.get(sembol, {})
            return gun_dict.get(tarih) or (list(gun_dict.values())[-1] if gun_dict else None)

    user_id = session["user_id"]
    pid = int(request.form["portfoy_id"])
    ad = request.form["ad"].strip()
    toplam_para = float(request.form["toplam_para"].replace(".", "").replace(",", "."))

    with get_db() as conn:
        conn.execute("""
            UPDATE kiyaslama_portfoy SET ad=?, toplam_para=?
            WHERE id=? AND user_id=?
        """, (ad, toplam_para, pid, user_id))
        # Fiyatları sıfırla — Güncelle butonu ile çekilecek
        conn.execute("UPDATE kiyaslama_kalem SET ilk_fiyat=NULL, son_fiyat=NULL WHERE portfoy_id=?", (pid,))

    flash("Portföy güncellendi. Fiyatları almak için Güncelle butonuna tıklayın.", "success")
    return redirect(url_for("kiyaslama"))


@app.route("/kiyaslama/portfoy-sil/<int:pid>")
@login_required
def kiyaslama_portfoy_sil(pid):
    user_id = session["user_id"]
    with get_db() as conn:
        conn.execute("DELETE FROM kiyaslama_kalem WHERE portfoy_id=?", (pid,))
        conn.execute("DELETE FROM kiyaslama_portfoy WHERE id=? AND user_id=?", (pid, user_id))
    return redirect(url_for("kiyaslama"))


@app.route("/kiyaslama/kalem-ekle", methods=["POST"])
@login_required
def kiyaslama_kalem_ekle():
    user_id = session["user_id"]
    portfoy_id = int(request.form["portfoy_id"])
    sembol = request.form["sembol"].strip().upper()
    agirlik = float(request.form["agirlik"].replace(",", "."))
    ilk_fiyat_str = request.form.get("ilk_fiyat", "").replace(",", ".").strip()
    son_fiyat_str = request.form.get("son_fiyat", "").replace(",", ".").strip()

    # Portföy tarihlerini al
    with get_db() as conn:
        p = conn.execute("SELECT * FROM kiyaslama_portfoy WHERE id=? AND user_id=?",
                         (portfoy_id, user_id)).fetchone()
        if not p:
            return redirect(url_for("kiyaslama"))
        kalem_sayi = conn.execute(
            "SELECT COUNT(*) as c FROM kiyaslama_kalem WHERE portfoy_id=?", (portfoy_id,)
        ).fetchone()["c"]
        if kalem_sayi >= 50:
            flash("Portföye en fazla 10 kalem eklenebilir.", "error")
            return redirect(url_for("kiyaslama"))

    from price_fetcher import fetch_fon_fiyatlari as _tefas, fetch_hisse_fiyatlari as _yahoo

    def _piyasa_bul(sembol):
        with get_db() as c:
            r = c.execute("SELECT piyasa FROM semboller WHERE kod=? LIMIT 1", (sembol,)).fetchone()
            if r: return r["piyasa"]
            r2 = c.execute("SELECT tur FROM islemler WHERE sembol=? LIMIT 1", (sembol,)).fetchone()
            return r2["tur"] if r2 else "BIST"

    def _fiyat_cek(sembol, tarih):
        """DB → TEFAS/Yahoo fallback ile fiyat çek."""
        f = get_fiyat(sembol, tarih)
        if f: return f
        piyasa = _piyasa_bul(sembol)
        if piyasa == "FON":
            prices, _ = _tefas([sembol])
            return prices.get(sembol)
        else:
            tur_map = {sembol: piyasa}
            prices, _ = _yahoo([sembol], tur_map=tur_map)
            gun_dict = prices.get(sembol, {})
            return gun_dict.get(tarih) or (list(gun_dict.values())[-1] if gun_dict else None)

    # İlk fiyat: DB'den dene, yoksa TEFAS/Yahoo'dan çek, yoksa manuel
    ilk_fiyat = None
    if not ilk_fiyat_str:
        ilk_fiyat = _fiyat_cek(sembol, p["ilk_tarih"])
    else:
        try:
            ilk_fiyat = float(ilk_fiyat_str)
        except:
            pass

    # Son fiyat: DB'den dene, yoksa TEFAS/Yahoo'dan çek, yoksa manuel
    bugun_str = str(bugun())
    son_fiyat = None
    if not son_fiyat_str:
        son_fiyat = _fiyat_cek(sembol, p["son_tarih"])
    if not son_fiyat and son_fiyat_str:
        try:
            son_fiyat = float(son_fiyat_str)
        except:
            pass

    vergi_str = request.form.get("vergi_orani", "0").replace(",", ".").strip()
    try:
        vergi_orani = float(vergi_str) if vergi_str else 0
    except:
        vergi_orani = 0
    with get_db() as conn:
        conn.execute(
            "INSERT INTO kiyaslama_kalem (portfoy_id,sembol,agirlik,ilk_fiyat,son_fiyat,vergi_orani) VALUES (?,?,?,?,?,?)",
            (portfoy_id, sembol, agirlik, ilk_fiyat, son_fiyat, vergi_orani)
        )
    return redirect(url_for("kiyaslama"))


@app.route("/kiyaslama/kalem-duzenle", methods=["POST"])
@login_required
def kiyaslama_kalem_duzenle():
    user_id = session["user_id"]
    kid = int(request.form["kalem_id"])
    sembol = request.form["sembol"].strip().upper()
    agirlik = float(request.form["agirlik"].replace(",", "."))
    ilk_fiyat_str = request.form.get("ilk_fiyat", "").replace(",", ".").strip()
    son_fiyat_str = request.form.get("son_fiyat", "").replace(",", ".").strip()
    vergi_str = request.form.get("vergi_orani", "0").replace(",", ".").strip()
    try:
        ilk_fiyat = float(ilk_fiyat_str) if ilk_fiyat_str else None
    except:
        ilk_fiyat = None
    try:
        son_fiyat = float(son_fiyat_str) if son_fiyat_str else None
    except:
        son_fiyat = None
    try:
        vergi_orani = float(vergi_str) if vergi_str else 0
    except:
        vergi_orani = 0
    with get_db() as conn:
        k = conn.execute("SELECT portfoy_id FROM kiyaslama_kalem WHERE id=?", (kid,)).fetchone()
        if k:
            p = conn.execute("SELECT user_id FROM kiyaslama_portfoy WHERE id=?", (k["portfoy_id"],)).fetchone()
            if p and p["user_id"] == user_id:
                conn.execute("""
                    UPDATE kiyaslama_kalem SET sembol=?, agirlik=?, ilk_fiyat=?, son_fiyat=?, vergi_orani=?
                    WHERE id=?
                """, (sembol, agirlik, ilk_fiyat, son_fiyat, vergi_orani, kid))
    return redirect(url_for("kiyaslama"))


@app.route("/kiyaslama/kalem-sil/<int:kid>")
@login_required
def kiyaslama_kalem_sil(kid):
    user_id = session["user_id"]
    with get_db() as conn:
        k = conn.execute("SELECT portfoy_id FROM kiyaslama_kalem WHERE id=?", (kid,)).fetchone()
        if k:
            p = conn.execute("SELECT user_id FROM kiyaslama_portfoy WHERE id=?",
                             (k["portfoy_id"],)).fetchone()
            if p and p["user_id"] == user_id:
                conn.execute("DELETE FROM kiyaslama_kalem WHERE id=?", (kid,))
    return redirect(url_for("kiyaslama"))


@app.route("/kiyaslama/fiyat-guncelle/<int:pid>")
@login_required
def kiyaslama_fiyat_guncelle(pid):
    """İlk ve son fiyatları toplu çek."""
    from price_fetcher import fetch_fon_fiyatlari, fetch_hisse_fiyatlari, fetch_hisse_toplu
    user_id = session["user_id"]
    bugun_str = str(bugun())
    with get_db() as conn:
        p = conn.execute("SELECT * FROM kiyaslama_portfoy WHERE id=? AND user_id=?",
                         (pid, user_id)).fetchone()
        if not p:
            return redirect(url_for("kiyaslama"))
        kalemler = conn.execute(
            "SELECT * FROM kiyaslama_kalem WHERE portfoy_id=?", (pid,)
        ).fetchall()

    ilk_tarih = p["ilk_tarih"]
    son_tarih = p["son_tarih"]

    from price_fetcher import tefas_aralik_cek, fetch_hisse_toplu, fetch_hisse_fiyatlari
    from datetime import date as _date
    ilk_date = _date.fromisoformat(ilk_tarih)
    son_date = _date.fromisoformat(son_tarih)
    bugun_date = _date.fromisoformat(bugun_str)

    # Piyasa bilgisini belirle
    def _piyasa(sembol):
        with get_db() as c:
            r = c.execute("SELECT piyasa FROM semboller WHERE kod=? LIMIT 1", (sembol,)).fetchone()
            if r: return r["piyasa"]
            r2 = c.execute("SELECT tur FROM islemler WHERE sembol=? LIMIT 1", (sembol,)).fetchone()
            return r2["tur"] if r2 else "BIST"

    piyasa_map = {k["sembol"]: _piyasa(k["sembol"]) for k in kalemler}
    fonlar = [k for k in kalemler if piyasa_map[k["sembol"]] == "FON"]
    hisseler = [k for k in kalemler if piyasa_map[k["sembol"]] != "FON"]

    # Tüm kalemleri güncelle
    for k in kalemler:
        s = k["sembol"]
        pm = piyasa_map[s]

        def _tam_fiyat(sembol, tarih):
            """Tam tarih eşleşmesi — <= değil."""
            with get_db() as c:
                r = c.execute("SELECT fiyat FROM fiyat_gecmisi WHERE sembol=? AND tarih=?",
                              (sembol, tarih)).fetchone()
                return r["fiyat"] if r else None

        ilk_f = _tam_fiyat(s, ilk_tarih)
        son_f = _tam_fiyat(s, son_tarih)

        if pm == "FON":
            # TEFAS'tan belirli tarih aralığı için çek
            tarihler = set()
            if not ilk_f: tarihler.add(ilk_date)
            if not son_f: tarihler.add(son_date)
            if tarihler:
                bas = min(tarihler)
                bit = max(tarihler)
                aralik = tefas_aralik_cek(s, bas, bit)
                if not ilk_f and ilk_date in aralik:
                    ilk_f = aralik[ilk_date]
                    with get_db() as conn:
                        conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                     (s, ilk_tarih, ilk_f))
                if not son_f:
                    # Sadece tam tarih eşleşmesinde yaz, farklı tarihin fiyatını uydurma
                    if son_date in aralik:
                        son_f = aralik[son_date]
                        with get_db() as conn:
                            conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                         (s, son_tarih, son_f))
        else:
            # Hisse: toplu Yahoo
            if not son_f:
                tur_map = {s: pm}
                prices = fetch_hisse_toplu([s], tur_map=tur_map)
                son_f = prices.get(s)
                if son_f:
                    with get_db() as conn:
                        conn.execute("INSERT OR REPLACE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                     (s, son_tarih, son_f))

        if ilk_f:
            with get_db() as conn:
                conn.execute("UPDATE kiyaslama_kalem SET ilk_fiyat=? WHERE id=?", (ilk_f, k["id"]))
        if son_f:
            with get_db() as conn:
                conn.execute("UPDATE kiyaslama_kalem SET son_fiyat=? WHERE id=?", (son_f, k["id"]))




    flash("Son fiyatlar güncellendi.", "success")
    return redirect(url_for("kiyaslama"))


@app.route("/api/son-fiyat")
@login_required
def api_son_fiyat():
    sembol = request.args.get("sembol","").strip().upper()
    tur = request.args.get("tur","")
    if not sembol:
        return jsonify({})

    # 1) DB'de var mı? (sadece FON için cache kullan)
    if tur == "FON" or not tur:
        with get_db() as conn:
            row = conn.execute(
                "SELECT fiyat, tarih FROM fiyat_gecmisi WHERE sembol=? ORDER BY tarih DESC LIMIT 1",
                (sembol,)).fetchone()
        if row:
            return jsonify({"fiyat": row["fiyat"], "tarih": row["tarih"], "kaynak": "db"})

    # Eğer tur gelmemişse DB'den bul
    if not tur:
        with get_db() as conn:
            s = conn.execute("SELECT piyasa FROM semboller WHERE kod=? LIMIT 1", (sembol,)).fetchone()
            if s:
                tur = s["piyasa"]  # FON, BIST, ABD
    if tur == "FON":
        try:
            from price_fetcher import fetch_tefas_fon, son_is_gunu
            fiyat = fetch_tefas_fon(sembol, son_is_gunu())
            if fiyat:
                tarih = str(bugun())
                with get_db() as conn:
                    conn.execute("INSERT OR IGNORE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                 (sembol, tarih, fiyat))
                return jsonify({"fiyat": round(fiyat,6), "tarih": tarih, "kaynak": "TEFAS"})
        except Exception as e:
            return jsonify({"hata": f"TEFAS: {str(e)[:60]}", "tur": tur})

    # 3) BIST ve ABD → Yahoo Finance (direkt requests)
    if tur in ("BIST", "ABD"):
        import requests as req
        yahoo_sembol = f"{sembol}.IS" if tur == "BIST" else sembol
        try:
            r = req.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sembol}"
                f"?interval=1d&range=5d",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                },
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                meta = data["chart"]["result"][0]["meta"]
                closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                timestamps = data["chart"]["result"][0]["timestamp"]
                fiyat = None
                tarih = None
                saat = None
                # regularMarketTime = son işlem saati
                market_time = meta.get("regularMarketTime")
                if market_time:
                    from datetime import datetime as dt
                    saat = dt.fromtimestamp(market_time, tz=ZoneInfo("Europe/Istanbul")).strftime("%H:%M")
                for i in range(len(closes)-1, -1, -1):
                    if closes[i] is not None:
                        fiyat = round(float(closes[i]), 4)
                        from datetime import datetime as dt
                        tarih = str(dt.fromtimestamp(timestamps[i]).date())
                        break
                if fiyat:
                    with get_db() as conn:
                        conn.execute("INSERT OR IGNORE INTO fiyat_gecmisi (sembol,tarih,fiyat) VALUES (?,?,?)",
                                     (sembol, tarih, fiyat))
                    return jsonify({"fiyat": fiyat, "tarih": tarih, "saat": saat, "kaynak": "Yahoo"})
        except Exception as e:
            return jsonify({"hata": f"Yahoo: {str(e)[:60]}", "tur": tur})

    return jsonify({"hata": f"{sembol} fiyatı alınamadı — manuel gir", "tur": tur})

@app.route("/api/sembol-ara")
@login_required
def sembol_ara():
    q = request.args.get("q","").strip().upper()
    piyasa = request.args.get("piyasa","")
    if len(q) < 1:
        return jsonify([])

    with get_db() as conn:
        if piyasa:
            rows = conn.execute("""
                SELECT kod, ad, piyasa FROM semboller
                WHERE (kod LIKE ? OR ad LIKE ?) AND piyasa=?
                ORDER BY kod LIMIT 20
            """, (f"{q}%", f"%{q}%", piyasa)).fetchall()
        else:
            rows = conn.execute("""
                SELECT kod, ad, piyasa FROM semboller
                WHERE kod LIKE ? OR ad LIKE ?
                ORDER BY kod LIMIT 20
            """, (f"{q}%", f"%{q}%")).fetchall()

        sonuclar = [{"kod": r["kod"], "ad": r["ad"] or "", "piyasa": r["piyasa"]} for r in rows]

        if not sonuclar:
            mevcut = conn.execute("""
                SELECT DISTINCT sembol, tur FROM islemler
                WHERE user_id=? AND sembol LIKE ?
                ORDER BY sembol LIMIT 10
            """, (session["user_id"], f"{q}%")).fetchall()
            sonuclar = [{"kod": r["sembol"], "ad": "", "piyasa": r["tur"]} for r in mevcut]

    return jsonify(sonuclar)

@app.route("/admin/sembol-guncelle", methods=["GET","POST"])
@login_required
def sembol_guncelle():
    """Sembol listelerini güncelle — admin only."""
    import threading
    from sembol_cek import sembolleri_guncelle, init_sembol_tablosu

    init_sembol_tablosu(DB_PATH)

    def _guncelle():
        with app.app_context():
            n = sembolleri_guncelle(DB_PATH)
            simdi = datetime.now(ZoneInfo("Europe/Istanbul")).strftime("%Y-%m-%d %H:%M:%S")
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO price_fetch_log (tarih,sonuc,detay) VALUES (?,?,?)",
                    (simdi, "Sembol-Güncelle", f"{n} sembol güncellendi"))

    t = threading.Thread(target=_guncelle, daemon=True)
    t.start()
    flash("⏳ Sembol listesi arka planda güncelleniyor (1-2 dakika).", "success")
    return redirect(url_for("ayarlar"))
