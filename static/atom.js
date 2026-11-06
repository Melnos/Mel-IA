// Logo Officiel MEL-IA 2.0 : Vgex Studio (Argent Platine, Cyan Ocanique & Teal Luminescent)
function atom(size, opts) {
  var s = size || 32;
  var o = opts || {};
  var id = 'm-' + Math.random().toString(36).substr(2, 6);
  
  return '<svg viewBox="0 0 256 256" width="' + s + '" height="' + s + '" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Logo MEL-IA 2.0">' +
    '<defs>' +
      '<filter id="sh-' + id + '" x="-20%" y="-20%" width="150%" height="150%">' +
        '<feDropShadow dx="0" dy="3" stdDeviation="5" flood-color="#000000" flood-opacity="0.35" />' +
      '</filter>' +
      '<linearGradient id="st-' + id + '" x1="160" y1="40" x2="310" y2="280" gradientUnits="userSpaceOnUse">' +
        '<stop offset="0%" stop-color="#FFFFFF" />' +
        '<stop offset="30%" stop-color="#E2E8F0" />' +
        '<stop offset="68%" stop-color="#94A3B8" />' +
        '<stop offset="100%" stop-color="#475569" />' +
      '</linearGradient>' +
      '<linearGradient id="sb-' + id + '" x1="150" y1="360" x2="320" y2="430" gradientUnits="userSpaceOnUse">' +
        '<stop offset="0%" stop-color="#F8FAFC" />' +
        '<stop offset="45%" stop-color="#CBD5E1" />' +
        '<stop offset="100%" stop-color="#64748B" />' +
      '</linearGradient>' +
      '<linearGradient id="sr-' + id + '" x1="330" y1="170" x2="415" y2="420" gradientUnits="userSpaceOnUse">' +
        '<stop offset="0%" stop-color="#F1F5F9" />' +
        '<stop offset="50%" stop-color="#94A3B8" />' +
        '<stop offset="100%" stop-color="#475569" />' +
      '</linearGradient>' +
      '<linearGradient id="tw1-' + id + '" x1="110" y1="160" x2="230" y2="390" gradientUnits="userSpaceOnUse">' +
        '<stop offset="0%" stop-color="#22D3EE" />' +
        '<stop offset="40%" stop-color="#0D9488" />' +
        '<stop offset="100%" stop-color="#044E54" />' +
      '</linearGradient>' +
      '<linearGradient id="tw2-' + id + '" x1="90" y1="260" x2="310" y2="380" gradientUnits="userSpaceOnUse">' +
        '<stop offset="0%" stop-color="#06B6D4" />' +
        '<stop offset="55%" stop-color="#0F766E" />' +
        '<stop offset="100%" stop-color="#023E42" />' +
      '</linearGradient>' +
      '<linearGradient id="rc-' + id + '" x1="340" y1="170" x2="300" y2="420" gradientUnits="userSpaceOnUse">' +
        '<stop offset="0%" stop-color="#67E8F9" />' +
        '<stop offset="70%" stop-color="#06B6D4" />' +
        '<stop offset="100%" stop-color="#0891B2" />' +
      '</linearGradient>' +
    '</defs>' +
    '<g transform="translate(128, 128) scale(0.52) translate(-256, -256)" filter="url(#sh-' + id + ')">' +
      '<path d="M 84 262 C 86 312, 136 376, 218 395 C 265 405, 305 385, 318 376 C 248 372, 185 334, 150 286 C 122 248, 106 200, 112 152 C 90 200, 82 232, 84 262 Z" fill="url(#tw2-' + id + ')" />' +
      '<path d="M 112 150 C 96 215, 120 288, 172 328 C 220 365, 276 355, 312 338 C 240 338, 182 292, 162 234 C 146 188, 158 136, 178 94 C 138 106, 118 126, 112 150 Z" fill="url(#tw1-' + id + ')" />' +
      '<path d="M 144 384 C 196 424, 268 428, 314 416 C 352 406, 386 374, 404 336 C 362 382, 296 408, 232 400 C 188 394, 158 376, 144 384 Z" fill="url(#sb-' + id + ')" />' +
      '<path d="M 256 42 C 172 44, 130 92, 130 160 C 130 236, 194 286, 268 284 C 304 283, 310 262, 292 258 C 238 246, 184 204, 184 146 C 184 96, 222 62, 276 62 C 316 62, 338 84, 344 112 C 342 78, 314 44, 256 42 Z" fill="url(#st-' + id + ')" />' +
      '<path d="M 334 162 C 352 232, 350 334, 300 422 C 374 382, 412 306, 412 242 C 412 178, 384 136, 334 162 Z" fill="url(#sr-' + id + ')" />' +
      '<path d="M 336 172 C 350 238, 348 334, 302 420" stroke="url(#rc-' + id + ')" stroke-width="4.5" stroke-linecap="round" fill="none" opacity="0.95" />' +
    '</g>' +
    '</svg>';
}

function mountAtom(id, size, opts) {
  var el = document.getElementById(id);
  if (!el) return;
  var sz = size || 32;
  el.innerHTML = atom(sz, opts);
  el.style.width = el.style.height = sz + "px";
  el.style.display = "inline-flex";
  el.style.alignItems = "center";
  el.style.justifyContent = "center";
}

if (typeof window !== 'undefined') {
  window.atom = atom;
  window.mountAtom = mountAtom;
}

