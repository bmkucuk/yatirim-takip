// AI Asistan — YT dashboard'a gömülü sohbet widget'ı
(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);

  const toggleBtn = $("#asistanToggle");
  const panel = $("#asistanPanel");
  const closeBtn = $("#asistanKapat");
  const mesajlarEl = $("#asistanMesajlar");
  const formEl = $("#asistanForm");
  const inputEl = $("#asistanInput");
  const gonderBtn = $("#asistanGonder");

  if (!toggleBtn || !panel) return;

  let gecmis = []; // Anthropic mesaj geçmişi (sadece bu sayfa oturumu boyunca)
  let bekliyor = false; // API isteği sürerken input kilitli

  function acKapat(force) {
    const acilsin = force !== undefined ? force : panel.hidden;
    panel.hidden = !acilsin;
    if (acilsin) inputEl.focus();
  }

  toggleBtn.addEventListener("click", () => acKapat());
  closeBtn.addEventListener("click", () => acKapat(false));

  inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      formEl.requestSubmit();
    }
  });

  function balonEkle(rol, html) {
    const div = document.createElement("div");
    div.className = "asistan-balon asistan-balon-" + rol;
    div.innerHTML = html;
    mesajlarEl.appendChild(div);
    mesajlarEl.scrollTop = mesajlarEl.scrollHeight;
    return div;
  }

  function kacir(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function kilitle(kilit) {
    bekliyor = kilit;
    inputEl.disabled = kilit;
    gonderBtn.disabled = kilit;
  }

  function sonucIsle(sonuc) {
    if (sonuc.tip === "hata") {
      balonEkle("asistan hata", kacir(sonuc.mesaj || "Bir hata oluştu."));
      kilitle(false);
      return;
    }
    gecmis = sonuc.gecmis || gecmis;
    if (sonuc.tip === "onay_gerekli") {
      const aciklamalar = (sonuc.onay_bekleyen || []).map((o) => kacir(o.aciklama)).join("<br>");
      const balon = balonEkle("asistan onay", `
        <div class="asistan-onay-metin">⚠️ ${aciklamalar}</div>
        <div class="asistan-onay-butonlar">
          <button type="button" class="asistan-btn-onayla">Onayla</button>
          <button type="button" class="asistan-btn-iptal">İptal</button>
        </div>
      `);
      const onaylaBtn = balon.querySelector(".asistan-btn-onayla");
      const iptalBtn = balon.querySelector(".asistan-btn-iptal");
      const bitir = (onaylandi) => {
        onaylaBtn.disabled = true;
        iptalBtn.disabled = true;
        kilitle(true);
        fetch("/api/asistan/onayla", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            gecmis: gecmis,
            hazir_sonuclar: sonuc.hazir_sonuclar || {},
            onay_bekleyen: sonuc.onay_bekleyen || [],
            onaylandi: onaylandi,
          }),
        })
          .then((r) => r.json())
          .then((s2) => { kilitle(false); sonucIsle(s2); })
          .catch(() => { kilitle(false); balonEkle("asistan hata", "Bağlantı hatası, tekrar dener misin?"); });
      };
      onaylaBtn.addEventListener("click", () => bitir(true));
      iptalBtn.addEventListener("click", () => bitir(false));
      kilitle(false);
      return;
    }
    // tip === "cevap"
    balonEkle("asistan", kacir(sonuc.mesaj || "").replace(/\n/g, "<br>"));
    kilitle(false);
  }

  formEl.addEventListener("submit", (e) => {
    e.preventDefault();
    if (bekliyor) return;
    const mesaj = inputEl.value.trim();
    if (!mesaj) return;
    balonEkle("kullanici", kacir(mesaj));
    inputEl.value = "";
    kilitle(true);
    fetch("/api/asistan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mesaj: mesaj, gecmis: gecmis }),
    })
      .then((r) => r.json())
      .then(sonucIsle)
      .catch(() => { kilitle(false); balonEkle("asistan hata", "Bağlantı hatası, tekrar dener misin?"); });
  });
})();
