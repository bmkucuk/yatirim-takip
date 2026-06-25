// Flash mesajlarını 4 saniye sonra gizle
document.querySelectorAll('.flash').forEach(el => {
  setTimeout(() => {
    el.style.transition = 'opacity 0.5s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 500);
  }, 4000);
});

// Tüm data-tarih elementlerini GG.AA.YYYY formatına çevir
function trTarih(str) {
  if (!str) return str;
  // YYYY-MM-DD veya YYYY-MM-DD HH:MM:SS formatını yakala
  const m = str.match(/^(\d{4})-(\d{2})-(\d{2})(.*)/);
  if (m) return m[3] + '.' + m[2] + '.' + m[1] + m[4];
  return str;
}
document.querySelectorAll('.tr-tarih').forEach(el => {
  el.textContent = trTarih(el.textContent.trim());
});
