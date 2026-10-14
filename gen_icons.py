"""Génère les icônes de l'app (atome blanc sur dégradé bleu encre & ambre). Usage : python gen_icons.py"""
from PIL import Image, ImageDraw
import math

def icon(size, path):
    S = size * 4
    bg = Image.new("RGBA", (S, S))
    px = bg.load()
    for y in range(S):
        for x in range(S):
            t = (x + y) / (2 * S)
            px[x, y] = (int(58 + (30 - 58) * t), int(102 + (63 - 102) * t), int(224 + (168 - 224) * t), 255)
    atom = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for ang in (0, 60, 120):
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        rx, ry, c = S * 0.34, S * 0.125, S / 2
        d.ellipse([c - rx, c - ry, c + rx, c + ry], outline=(255, 255, 255, 235), width=int(S * 0.018))
        layer = layer.rotate(ang, center=(c, c))
        atom = Image.alpha_composite(atom, layer)
    d = ImageDraw.Draw(atom)
    r = S * 0.07
    d.ellipse([S / 2 - r, S / 2 - r, S / 2 + r, S / 2 + r], fill=(255, 255, 255, 255))
    out = Image.alpha_composite(bg, atom).resize((size, size), Image.LANCZOS)
    out.convert("RGB").save(path)

icon(192, "static/icons/icon-192.png")
icon(512, "static/icons/icon-512.png")
print("icônes créées")
