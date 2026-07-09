# -*- coding: utf-8 -*-
"""
KAP (Kamuyu Aydınlatma Platformu) entegrasyonu.
Fon kodundan otomatik olarak en son "Portföy Dağılım Raporu"nu bulur, PDF'ini indirir
ve hisse bazlı ağırlıkları (GRUP % — fonun toplam portföy değerine göre) çıkarır.

Uç noktalar (kap.org.tr'nin kendi frontend'inin kullandığı, dokümante edilmemiş ama
herkese açık iç API'si):
  1. GET  /tr/api/member/filter/{kod}                      -> mkkMemberOid bul
  2. POST /tr/api/disclosure/members/byCriteria             -> bildirim listesi
  3. GET  /tr/api/notification/attachment-detail/{index}    -> PDF eki objId
  4. GET  /tr/api/file/download/{objId}                     -> PDF bytes
     (bazı bildirimlerde PDF, Java'nın serialized byte[] formatına sarılı gelir)
"""
import re
import struct
import time
from datetime import date, timedelta

import requests

KAP_BASE = "https://www.kap.org.tr"
KAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "Accept": "application/json",
}


_SUBJECT_OID_CACHE = {}


def kap_portfoy_dagilim_subject_oid_bul():
    """'Portföy Dağılım Raporu' bildirim konusunun KAP subjectOid'sini bulur (cache'ler).
    disclosureClass='DG' altında, üye tipine göre farklı listeler dönebiliyor; birkaç
    üye tipini dener.
    """
    if "oid" in _SUBJECT_OID_CACHE:
        return _SUBJECT_OID_CACHE["oid"], None
    debug = []
    for uye_tipi in ["IGS", "YF", "FON", "YAT", "PFON", "HT"]:
        try:
            r = requests.get(
                f"{KAP_BASE}/tr/api/disclosure/subjects/DG/{uye_tipi}",
                headers=KAP_HEADERS, timeout=10
            )
            debug.append(f"DG/{uye_tipi} -> {r.status_code}")
            if r.status_code != 200:
                continue
            data = r.json()
            if not isinstance(data, list):
                continue
            for item in data:
                if "portföy dağılım raporu" in (item.get("subject") or "").lower():
                    _SUBJECT_OID_CACHE["oid"] = item.get("subjectOid")
                    return item.get("subjectOid"), None
        except Exception as e:
            debug.append(f"DG/{uye_tipi} istisna: {e}")
    return None, " | ".join(debug)


def kap_fon_kodu_ile_rapor_bul(fon_kodu, toplam_gun=45, pencere_gun=3):
    """OID/subject aramaya gerek kalmadan: tarihi küçük pencerelere bölüp (varsayılan
    3 gün) her pencerede TÜM bildirimleri tarar (mkkMemberOidList ve subjectList boş).
    2000 kayıt limitine yaklaşmadan geniş bir tarih aralığını güvenle tarayabilmek için
    bu şekilde parçalıyoruz (subjectOid tahminine dayanmıyor, KAP'ın şemasını olduğu
    gibi kullanıyor). En yeni pencereden başlar, eşleşme bulunca hemen durur.
    Döner: (disclosureIndex, publishDate, debug_str) veya (None, None, debug_str)
    """
    kod = fon_kodu.strip().upper()
    bugun = date.today()
    debug_satirlari = []
    toplam_tarama = 0
    son_ornekler = []
    pencere_sayisi = max(1, toplam_gun // pencere_gun)

    for i in range(pencere_sayisi):
        bitis = bugun - timedelta(days=i * pencere_gun)
        baslangic = bitis - timedelta(days=pencere_gun)
        body = {
            "fromDate": baslangic.isoformat(),
            "toDate": bitis.isoformat(),
            "mkkMemberOidList": [],
            "subjectList": [],
        }
        try:
            r = requests.post(
                f"{KAP_BASE}/tr/api/disclosure/members/byCriteria",
                json=body, headers=KAP_HEADERS, timeout=20
            )
            if r.status_code != 200:
                debug_satirlari.append(f"[{baslangic}-{bitis}] -> {r.status_code}")
                continue
            sonuclar = r.json()
            if not isinstance(sonuclar, list):
                debug_satirlari.append(f"[{baslangic}-{bitis}] beklenmeyen tip")
                continue
            toplam_tarama += len(sonuclar)

            eslesen = [
                d for d in sonuclar
                if kod in [s.strip().upper() for s in (d.get("stockCodes") or "").split(",") if s.strip()]
                and "portföy dağılım raporu" in (d.get("subject") or "").lower()
            ]
            if not eslesen:
                eslesen = [
                    d for d in sonuclar
                    if (d.get("fundCode") or "").strip().upper() == kod
                    and "portföy dağılım raporu" in (d.get("subject") or "").lower()
                ]
            if not eslesen:
                eslesen = [
                    d for d in sonuclar
                    if (d.get("relatedStocks") or "").strip().upper() == kod
                    and "portföy dağılım raporu" in (d.get("subject") or "").lower()
                ]
            if eslesen:
                eslesen.sort(key=lambda d: d.get("publishDate", ""), reverse=True)
                en_son = eslesen[0]
                return en_son.get("disclosureIndex"), en_son.get("publishDate"), None

            # Hicbir eslesme yoksa, ornek toplamaya devam et (en fazla 4 ornek):
            # 'portfoy' gecen HERHANGI bir subject VEYA fundCode alani dolu olan kayitlar
            if len(son_ornekler) < 2:
                for d in sonuclar:
                    subj = d.get("subject") or ""
                    fc = d.get("fundCode")
                    if "dağılım raporu" in subj.lower() or "portföy" in subj.lower() or fc:
                        import json as _json
                        son_ornekler.append(_json.dumps(d, ensure_ascii=False))
                        if len(son_ornekler) >= 2:
                            break

            if len(sonuclar) >= 1990:
                debug_satirlari.append(f"[{baslangic}-{bitis}] {len(sonuclar)} kayit (limite yakin!)")
        except Exception as e:
            debug_satirlari.append(f"[{baslangic}-{bitis}] istisna: {e}")
        time.sleep(0.5)

    debug = f"{pencere_sayisi} pencere ({toplam_gun} gün), toplam {toplam_tarama} bildirim tarandı, '{kod}' eşleşmesi yok."
    if son_ornekler:
        debug += " ÖRNEK KAYITLAR: " + " ;; ".join(son_ornekler)
    if debug_satirlari:
        debug += " " + " | ".join(debug_satirlari)
    return None, None, debug


def kap_fon_oid_bul(fon_kodu):
    """Fon koduna (örn. 'PBR') karşılık gelen KAP mkkMemberOid'sini bulur.
    Fonlar KAP'ta şirketlerden farklı bir üyelik tipinde olabileceği için birkaç
    olası uç noktayı sırayla dener. Bulamazsa, hata ayıklama için denenen her
    uç noktanın durum kodunu/gövdesini de döndürür.
    """
    kod = fon_kodu.strip().upper()
    denemeler = [
        f"{KAP_BASE}/tr/api/member/filter/{kod}",
        f"{KAP_BASE}/tr/api/fund/filter/{kod}",
        f"{KAP_BASE}/tr/api/fon/filter/{kod}",
        f"{KAP_BASE}/tr/api/member-fund/filter/{kod}",
    ]
    debug = []
    for url in denemeler:
        try:
            r = requests.get(url, headers=KAP_HEADERS, timeout=10)
            debug.append(f"{url} -> {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                continue
            data = r.json()
            if isinstance(data, list):
                if not data:
                    continue
                data = data[0]
            if not isinstance(data, dict):
                continue
            oid = data.get("mkkMemberOid") or data.get("kapMemberOid")
            unvan = data.get("title") or data.get("kapMemberTitle")
            if oid:
                return oid, unvan, None
        except Exception as e:
            debug.append(f"{url} -> İSTİSNA: {e}")
    return None, None, " | ".join(debug)


def kap_son_portfoy_raporu_bul(mkk_member_oid, gun_araligi=75):
    """Verilen fon OID'si için son 'Portföy Dağılım Raporu' bildirimini bulur.
    Döner: (disclosureIndex, publishDate) veya (None, None)
    """
    bugun = date.today()
    baslangic = bugun - timedelta(days=gun_araligi)
    body = {
        "fromDate": baslangic.isoformat(),
        "toDate": bugun.isoformat(),
        "mkkMemberOidList": [mkk_member_oid],
        "subjectList": [],
    }
    try:
        r = requests.post(
            f"{KAP_BASE}/tr/api/disclosure/members/byCriteria",
            json=body, headers=KAP_HEADERS, timeout=15
        )
        if r.status_code != 200:
            return None, None
        sonuclar = r.json()
        raporlar = [
            d for d in sonuclar
            if "portföy dağılım raporu" in (d.get("subject") or "").lower()
        ]
        if not raporlar:
            return None, None
        raporlar.sort(key=lambda d: d.get("publishDate", ""), reverse=True)
        en_son = raporlar[0]
        return en_son.get("disclosureIndex"), en_son.get("publishDate")
    except Exception:
        return None, None


def kap_pdf_obj_id_bul(disclosure_index):
    """Bildirim index'inden PDF ekinin objId'sini bulur."""
    try:
        headers = dict(KAP_HEADERS)
        headers["Referer"] = f"{KAP_BASE}/tr/Bildirim/{disclosure_index}"
        r = requests.get(
            f"{KAP_BASE}/tr/api/notification/attachment-detail/{disclosure_index}",
            headers=headers, timeout=10
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, list) and data:
            ekler = data[0].get("attachments", [])
            for ek in ekler:
                if (ek.get("fileExtension") or "").lower() == "pdf":
                    return ek.get("objId")
            if ekler:
                return ekler[0].get("objId")
    except Exception:
        pass
    return None


def _java_byte_array_ayikla(raw):
    """KAP'ın file/download uç noktası bazı bildirimlerde PDF'i Java'nın serialized
    byte[] formatına sarıyor (AC ED 00 05 ... TC_ENDBLOCKDATA 78 70 <4 byte uzunluk> <PDF>).
    Bu sarmalayıcıyı tespit edip PDF'i çıkarır; sarmalayıcı yoksa veriyi olduğu gibi döner.
    """
    if len(raw) > 30 and raw[:4] == b"\xac\xed\x00\x05":
        try:
            idx = raw.index(b"\x78\x70", 10)
            arr_len = struct.unpack(">I", raw[idx + 2:idx + 6])[0]
            return raw[idx + 6:idx + 6 + arr_len]
        except Exception:
            pass
    return raw


def kap_pdf_indir(obj_id, disclosure_index=None):
    """Verilen objId için PDF byte'larını indirir (Java sarmalayıcıyı otomatik çözer)."""
    headers = dict(KAP_HEADERS)
    if disclosure_index:
        headers["Referer"] = f"{KAP_BASE}/tr/Bildirim/{disclosure_index}"
    r = requests.get(
        f"{KAP_BASE}/tr/api/file/download/{obj_id}",
        headers=headers, timeout=25
    )
    r.raise_for_status()
    return _java_byte_array_ayikla(r.content)


_SAYI_RE = re.compile(r"^-?[\d.]+,\d{2}$")


def _tr_sayi(s):
    try:
        return float(s.replace(".", "").replace(",", "."))
    except Exception:
        return None


_SAYI_TOKEN_RE = re.compile(r"^-?\d[\d.]*,\d{2}$")
_KOD_RE = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")


def pdf_hisse_dagilimi_ayikla(pdf_bytes):
    """Portföy Dağılım Raporu PDF'inden hisse bazlı ağırlıkları (GRUP %) çıkarır.
    pdfplumber'ın metin-bazlı tablo tespitini kullanır (bu raporlarda çizgi/kenarlık
    olmadığı için 'lines' stratejisi çalışmıyor). Her hisse satırının ilk hücresi
    hisse kodu, son hücrelerinden biri de 'TOPLAM(FPD) GRUP% TOPLAM(FTD)' üçlüsünü
    (bazen tek hücrede boşlukla ayrık, bazen ayrı hücrelerde) içerir — ortadaki değer
    GRUP%'tır (fonun toplam portföy değerine göre ağırlık).
    Döner: (hisseler: [(kod, agirlik), ...] aggregated, kap_grup_toplami: float|None)
    """
    import pdfplumber
    import io

    agirliklar = {}
    kap_toplam = None
    bolum_bulundu = False
    bolum_bitti = False

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tam_metin = page.extract_text() or ""
            tam_metin_bosluksuz = re.sub(r"\s+", "", tam_metin)
            if not bolum_bulundu and "HİSSESENETLERİ" not in tam_metin_bosluksuz:
                continue
            if bolum_bitti:
                break

            tablolar = page.extract_tables(
                table_settings={"vertical_strategy": "text", "horizontal_strategy": "text"}
            )
            if not tablolar:
                continue

            for satir in tablolar[0]:
                birlesik = " ".join(c for c in satir if c)
                birlesik_bosluksuz = re.sub(r"\s+", "", birlesik)
                if "HİSSESENETLERİ" in birlesik_bosluksuz:
                    bolum_bulundu = True
                    continue
                if not bolum_bulundu:
                    continue
                if "GRUPTOPLAMI" in birlesik_bosluksuz:
                    sayilar = [t for t in re.findall(r"-?\d[\d.]*,\d{2}", birlesik)]
                    if len(sayilar) >= 2:
                        try:
                            kap_toplam = _tr_sayi(sayilar[-2])
                        except Exception:
                            pass
                    bolum_bitti = True
                    break
                if "TÜREV" in birlesik_bosluksuz or "BORÇLANMASENETLERİ" in birlesik_bosluksuz:
                    bolum_bitti = True
                    break

                ilk = (satir[0] or "").strip()
                if not _KOD_RE.match(ilk):
                    continue
                # Bu satırdaki tüm hücrelerden ondalıklı sayı token'larını çıkar
                tum_hucreler = [c for c in satir if c]
                sayi_tokenlari = []
                for hucre in tum_hucreler:
                    for parca in hucre.split():
                        if _SAYI_TOKEN_RE.match(parca):
                            sayi_tokenlari.append(parca)
                if len(sayi_tokenlari) < 3:
                    continue
                # Son 3 ondalıklı sayı: TOPLAM(FPD GÖRE), GRUP(%), TOPLAM(FTD GÖRE)
                grup_pct = _tr_sayi(sayi_tokenlari[-2])
                if grup_pct is None:
                    continue
                agirliklar[ilk] = agirliklar.get(ilk, 0.0) + grup_pct

    hisseler = sorted(agirliklar.items(), key=lambda x: -x[1])
    return hisseler, kap_toplam


def kap_fon_kompozisyon_getir(fon_kodu):
    """Tam pipeline: fon kodu -> KAP'tan en son Portföy Dağılım Raporu -> ayrıştırılmış
    hisse listesi. Önce 'fundCode' ile doğrudan filtreleme dener (OID gerektirmez);
    olmazsa OID bulup fon-bazlı arama yapar.
    Döner: dict {basarili, hata, fon_adi, donem, hisseler, kap_toplam, hesaplanan_toplam}
    """
    disclosure_index, publish_date, debug1 = kap_fon_kodu_ile_rapor_bul(fon_kodu)
    fon_adi = None

    if not disclosure_index:
        oid, fon_adi, debug2 = kap_fon_oid_bul(fon_kodu)
        if oid:
            disclosure_index, publish_date = kap_son_portfoy_raporu_bul(oid)
        if not disclosure_index:
            hata = f"'{fon_kodu}' için son günlerde Portföy Dağılım Raporu bulunamadı."
            detaylar = " || ".join(d for d in [debug1, debug2 if oid is None else None] if d)
            if detaylar:
                hata += f" [DEBUG: {detaylar}]"
            return {"basarili": False, "hata": hata}

    time.sleep(0.3)
    obj_id = kap_pdf_obj_id_bul(disclosure_index)
    if not obj_id:
        return {"basarili": False, "hata": f"Bildirimde (id={disclosure_index}) PDF eki bulunamadı."}

    time.sleep(0.3)
    try:
        pdf_bytes = kap_pdf_indir(obj_id, disclosure_index)
    except Exception as e:
        return {"basarili": False, "hata": f"PDF indirilemedi: {e}"}

    try:
        hisseler, kap_toplam = pdf_hisse_dagilimi_ayikla(pdf_bytes)
    except Exception as e:
        return {"basarili": False, "hata": f"PDF ayrıştırılamadı: {e}"}

    if not hisseler:
        return {"basarili": False, "hata": "PDF'te hisse senedi bölümü bulunamadı (fon tamamen tahvil/repo ağırlıklı olabilir)."}

    hesaplanan_toplam = round(sum(a for _, a in hisseler), 2)
    dogrulandi = kap_toplam is not None and abs(hesaplanan_toplam - kap_toplam) < 0.5

    return {
        "basarili": True,
        "fon_kodu": fon_kodu.upper(),
        "fon_adi": fon_adi,
        "donem": publish_date,
        "hisseler": hisseler,
        "kap_toplam": kap_toplam,
        "hesaplanan_toplam": hesaplanan_toplam,
        "dogrulandi": dogrulandi,
    }
