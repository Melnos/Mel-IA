// Logo atome MEL-IA : noyau + 3 anneaux, électrons en mouvement permanent
var atomCount = 0;
function atom() {
  var n = ++atomCount, s = "";
  var path = "M -80 0 A 80 30 0 1 1 80 0 A 80 30 0 1 1 -80 0";
  [0, 60, 120].forEach(function (r, i) {
    s += '<g transform="rotate(' + r + ')">' +
      '<ellipse rx="80" ry="30" fill="none" stroke="url(#ring' + n + ')" stroke-opacity=".5" stroke-width="1.5"/>' +
      '<circle r="5" fill="url(#g' + n + ')">' +
      '<animateMotion dur="' + [3.5, 4.5, 5.5][i] + 's" repeatCount="indefinite" path="' + path + '"/>' +
      '</circle></g>';
  });
  return '<svg viewBox="-100 -100 200 200" width="100%" height="100%" role="img" aria-label="Logo MEL-IA : un atome">' +
    '<defs>' +
    '<linearGradient id="g' + n + '" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="#E0865F"/><stop offset="1" stop-color="#C96442"/></linearGradient>' +
    '<linearGradient id="ring' + n + '" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="#D97757"/><stop offset="1" stop-color="#E8A985"/></linearGradient>' +
    '<radialGradient id="h' + n + '">' +
    '<stop offset="0" stop-color="#D97757" stop-opacity=".38"/>' +
    '<stop offset="1" stop-color="#D97757" stop-opacity="0"/></radialGradient>' +
    '</defs>' +
    '<circle r="36" fill="url(#h' + n + ')">' +
    '<animate attributeName="r" values="28;40;28" dur="3s" repeatCount="indefinite"/></circle>' +
    s +
    '<circle r="13" fill="url(#g' + n + ')"/>' +
    '<circle r="6" fill="#fff" opacity=".6"/>' +
    '</svg>';
}

function mountAtom(id, size) {
  var el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = atom();
  if (size) { el.style.width = el.style.height = size + "px"; el.style.display = "inline-block"; }
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var v = el.querySelector("svg"); v && v.pauseAnimations && v.pauseAnimations();
  }
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(function () {});
