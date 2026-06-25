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
        """)

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
    """Her sembol için portföy pozisyonunu hesapla."""
    with get_db() as conn:
        if hesap_filtre == "Hepsi":
            rows = conn.execute("""
                SELECT sembol, alissat, SUM(adet) as toplam_adet, SUM(tutar) as toplam_tutar
                FROM islemler WHERE user_id=?
                GROUP BY sembol, alissat
            """, (user_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT sembol, alissat, SUM(adet) as toplam_adet, SUM(tutar) as toplam_tutar
                FROM islemler WHERE user_id=? AND hesap=?
                GROUP BY sembol, alissat
            """, (user_id, hesap_filtre)).fetchall()

    # Sembol bazında topla
    pozisyonlar = {}
    for r in rows:
        s = r["sembol"]
        if s not in pozisyonlar:
            pozisyonlar[s] = {"alis_adet": 0, "alis_tutar": 0, "satis_adet": 0, "satis_tutar": 0}
        if r["alissat"] == "Alış":
            pozisyonlar[s]["alis_adet"] += r["toplam_adet"]
            pozisyonlar[s]["alis_tutar"] += r["toplam_tutar"]
        else:
            pozisyonlar[s]["satis_adet"] += r["toplam_adet"]
            pozisyonlar[s]["satis_tutar"] += r["toplam_tutar"]

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

        mevcut_deger = kalan_adet * son_fiyat
        maliyet = p["alis_tutar"] - p["satis_tutar"]
        kar_zarar = mevcut_deger - maliyet

        # Günlük getiri
        dun_fiyat = get_fiyat(sembol, dun_str)
        gunluk_tl = (son_fiyat - dun_fiyat) * kalan_adet if dun_fiyat else 0
        gunluk_yuzde = ((son_fiyat / dun_fiyat) - 1) * 100 if dun_fiyat else 0

        # Dönemsel getiriler
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
            "kalan_adet": kalan_adet,
            "alis_maliyet": maliyet,
            "son_fiyat": son_fiyat,
            "mevcut_deger": mevcut_deger,
            "kar_zarar": kar_zarar,
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
        hesaplar = [r["ad"] for r in conn.execute(
            "SELECT ad FROM hesaplar WHERE user_id=?", (user_id,)).fetchall()]

    portfoy = hesapla_portfoy(user_id, hesap_filtre)

    toplam_deger = sum(p["mevcut_deger"] for p in portfoy)
    toplam_gunluk_tl = sum(p["gunluk_tl"] for p in portfoy)
    toplam_gunluk_pct = (toplam_gunluk_tl / (toplam_deger - toplam_gunluk_tl) * 100) if (toplam_deger - toplam_gunluk_tl) else 0
    toplam_kar = sum(p["kar_zarar"] for p in portfoy)
    toplam_maliyet = sum(p["alis_maliyet"] for p in portfoy)
    toplam_getiri_pct = (toplam_kar / toplam_maliyet * 100) if toplam_maliyet else 0

    aylik = get_aylik_getiri(user_id)

    with get_db() as conn:
        son_fiyat_tarihi = conn.execute(
            "SELECT MAX(tarih) as t FROM fiyat_gecmisi").fetchone()["t"] or "-"
        son_log = conn.execute(
            "SELECT * FROM price_fetch_log ORDER BY id DESC LIMIT 1").fetchone()

    return render_template("dashboard.html",
        portfoy=portfoy,
        toplam_deger=toplam_deger,
        toplam_gunluk_tl=toplam_gunluk_tl,
        toplam_gunluk_pct=toplam_gunluk_pct,
        toplam_kar=toplam_kar,
        toplam_getiri_pct=toplam_getiri_pct,
        hesaplar=hesaplar,
        hesap_filtre=hesap_filtre,
        aylik=aylik,
        son_fiyat_tarihi=son_fiyat_tarihi,
        son_log=son_log,
    )

# ── İşlemler ────────────────────────────────────────────────────────────────

@app.route("/islemler")
@login_required
def islemler():
    user_id = session["user_id"]
    filtre = request.args.get("sembol","")
    with get_db() as conn:
        hesaplar = [r["ad"] for r in conn.execute("SELECT ad FROM hesaplar WHERE user_id=?", (user_id,)).fetchall()]
        aracilar = [r["ad"] for r in conn.execute("SELECT ad FROM aracilar WHERE user_id=?", (user_id,)).fetchall()]
        if filtre:
            rows = conn.execute("""
                SELECT * FROM islemler WHERE user_id=? AND sembol=?
                ORDER BY tarih DESC
            """, (user_id, filtre.upper())).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM islemler WHERE user_id=?
                ORDER BY tarih DESC
            """, (user_id,)).fetchall()
    semboller = sorted(set(r["sembol"] for r in rows))
    return render_template("islemler.html", islemler=rows, hesaplar=hesaplar,
                           aracilar=aracilar, filtre=filtre, semboller=semboller)

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
        # Kullanıcının sembollerini bul
        semboller = [r["sembol"] for r in conn.execute("""
            SELECT DISTINCT sembol FROM islemler WHERE user_id=?
        """, (user_id,)).fetchall()]

        # Son 30 günün fiyatları
        rows = conn.execute("""
            SELECT * FROM fiyat_gecmisi
            WHERE sembol IN ({})
            ORDER BY tarih DESC, sembol
            LIMIT 200
        """.format(",".join("?"*len(semboller)) if semboller else "''"
                   ), semboller).fetchall() if semboller else []

        logs = conn.execute("""
            SELECT * FROM price_fetch_log ORDER BY id DESC LIMIT 10
        """).fetchall()

    return render_template("fiyatlar.html", semboller=semboller, rows=rows, logs=logs)

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
    """Tüm aktif sembollerin fiyatlarını otomatik çek."""
    user_id = session["user_id"]
    with get_db() as conn:
        semboller = [r["sembol"] for r in conn.execute("""
            SELECT DISTINCT sembol, tur FROM islemler WHERE user_id=?
        """, (user_id,)).fetchall()]
        tum_semboller = list(conn.execute(
            "SELECT DISTINCT sembol, tur FROM islemler").fetchall())

    # Tüm kullanıcıların sembollerini güncelle (fiyat geçmişi ortak)
    fon_sembolleri = [r["sembol"] for r in tum_semboller if r["tur"] == "FON"]
    hisse_sembolleri = [r["sembol"] for r in tum_semboller if r["tur"] == "HISSE"]

    sonuc = fetch_all_prices(fon_sembolleri, hisse_sembolleri)

    basarili = 0
    for sembol, tarih, fiyat in sonuc.get("prices", []):
        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO fiyat_gecmisi (sembol, tarih, fiyat)
                VALUES (?,?,?)
            """, (sembol, tarih, fiyat))
        basarili += 1

    with get_db() as conn:
        conn.execute("""
            INSERT INTO price_fetch_log (tarih, sonuc, detay)
            VALUES (?,?,?)
        """, (str(bugun()), sonuc.get("method","?"),
              f"{basarili} fiyat güncellendi. {sonuc.get('errors','')}"))

    flash(f"{basarili} fiyat güncellendi. Kaynak: {sonuc.get('method','?')}", "success")
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
        elif action == "sifre":
            eski = request.form["eski_sifre"]
            yeni = request.form["yeni_sifre"]
            with get_db() as conn:
                user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user["password_hash"] == hash_pw(eski) and len(yeni) >= 6:
                with get_db() as conn:
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                                 (hash_pw(yeni), user_id))
                flash("Şifre güncellendi.", "success")
            else:
                flash("Eski şifre hatalı veya yeni şifre çok kısa.", "error")
        return redirect(url_for("ayarlar"))

    with get_db() as conn:
        hesaplar = conn.execute("SELECT * FROM hesaplar WHERE user_id=?", (user_id,)).fetchall()
        aracilar = conn.execute("SELECT * FROM aracilar WHERE user_id=?", (user_id,)).fetchall()

    return render_template("ayarlar.html", hesaplar=hesaplar, aracilar=aracilar)

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

@app.route("/api/portfoy")
@login_required
def api_portfoy():
    portfoy = hesapla_portfoy(session["user_id"])
    return jsonify(portfoy)

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
