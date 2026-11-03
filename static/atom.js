// Logo & Emblème Officiel MEL-IA : Noyau quantique haute densité, anneaux orbitaux épais et lumineux
var atomCount = 0;
function atom() {
  var n = ++atomCount, s = "";
  var path = "M -76 0 A 76 28 0 1 1 76 0 A 76 28 0 1 1 -76 0";
  [ -32, 38, 90 ].forEach(function (r, i) {
    s += '<g transform="rotate(' + r + ')">' +
      // Glow externe de l'anneau
      '<ellipse rx="76" ry="28" fill="none" stroke="url(#ring' + n + ')" stroke-opacity=".25" stroke-width="8"/>' +
      // Anneau épais principal
      '<ellipse rx="76" ry="28" fill="none" stroke="url(#ring' + n + ')" stroke-width="4.2" stroke-linecap="round"/>' +
      // Électron / particule orbitale lumineuse
      '<circle r="4.8" fill="#ffffff" filter="url(#glow' + n + ')">' +
      '<animateMotion dur="' + [3.2, 4.0, 4.8][i] + 's" repeatCount="indefinite" path="' + path + '"/>' +
      '</circle></g>';
  });
  return '<svg viewBox="-105 -105 210 210" width="100%" height="100%" role="img" aria-label="Logo MEL-IA : Noyau Quantique">' +
    '<defs>' +
    '<linearGradient id="core' + n + '" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0%" stop-color="#38bdf8"/><stop offset="60%" stop-color="#00e5ff"/><stop offset="100%" stop-color="#0284c7"/></linearGradient>' +
    '<linearGradient id="ring' + n + '" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0%" stop-color="#00e5ff"/><stop offset="50%" stop-color="#38bdf8"/><stop offset="100%" stop-color="#2563eb"/></linearGradient>' +
    '<radialGradient id="halo' + n + '">' +
    '<stop offset="0%" stop-color="#00e5ff" stop-opacity=".55"/>' +
    '<stop offset="50%" stop-color="#0284c7" stop-opacity=".25"/>' +
    '<stop offset="100%" stop-color="#060b17" stop-opacity="0"/></radialGradient>' +
    '<filter id="glow' + n + '" x="-50%" y="-50%" width="200%" height="200%">' +
    '<feGaussianBlur stdDeviation="3" result="blur"/>' +
    '<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>' +
    '</filter>' +
    '</defs>' +
    // Halo d'énergie pulsé
    '<circle r="46" fill="url(#halo' + n + ')">' +
    '<animate attributeName="r" values="36;52;36" dur="3s" repeatCount="indefinite"/>' +
    '<animate attributeName="opacity" values="0.7;1;0.7" dur="3s" repeatCount="indefinite"/>' +
    '</circle>' +
    // Anneaux orbitaux
    s +
    // Noyau central solide et éclatant
    '<circle r="18" fill="url(#core' + n + ')" filter="url(#glow' + n + ')"/>' +
    '<circle r="14" fill="#00e5ff"/>' +
    '<circle cx="-4" cy="-4" r="5" fill="#ffffff" opacity=".85"/>' +
    '</svg>';
}

function mountAtom(id, size) {
  var el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = atom();
  if (size) { 
    el.style.width = el.style.height = size + "px"; 
    el.style.display = "inline-flex"; 
    el.style.alignItems = "center";
    el.style.justifyContent = "center";
  }
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var v = el.querySelector("svg"); v && v.pauseAnimations && v.pauseAnimations();
  }
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(function () {});
