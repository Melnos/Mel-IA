"""
Générateur du logo et icônes officiels MEL-IA.
Style moderne, épais, néon cyan électrique & bleu nuit profond avec effet 3D / verre.
Usage : python gen_icons.py
"""
import math
from PIL import Image, ImageDraw, ImageFilter

def create_mel_logo(size=512):
    S = size * 2  # Super-sampling 2x pour un anti-aliasing parfait
    
    # Fond squircle (carré arrondi moderne)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    
    # 1. Dégradé de fond radial riche (Bleu nuit abyssal -> Cyan sombre)
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    
    # Squircle arrondi
    corner_radius = int(S * 0.22)
    bg_draw.rounded_rectangle([0, 0, S, S], radius=corner_radius, fill=(6, 11, 24, 255))
    
    # Lumière radiale au centre-haut
    radial_glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    r_draw = ImageDraw.Draw(radial_glow)
    cx, cy = S // 2, int(S * 0.45)
    max_r = int(S * 0.7)
    for r in range(max_r, 0, -8):
        alpha = int(40 * (1.0 - (r / max_r) ** 1.3))
        r_draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 229, 255, alpha))
    
    bg = Image.alpha_composite(bg, radial_glow)
    
    # Bordure subtile du squircle
    border_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    b_draw = ImageDraw.Draw(border_layer)
    b_draw.rounded_rectangle([2, 2, S - 2, S - 2], radius=corner_radius, outline=(56, 189, 248, 70), width=int(S * 0.012))
    bg = Image.alpha_composite(bg, border_layer)
    
    # 2. Dessin de l'emblème épais et dynamique MEL-IA
    # Deux anneaux orbitaux épais et entrelacés en perspective + Noyau quantique lumineux
    emblem = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    e_draw = ImageDraw.Draw(emblem)
    
    # Anneau 1 (incliné -35°) - épais et néon cyan
    ring1 = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    r1_draw = ImageDraw.Draw(ring1)
    rx, ry = int(S * 0.36), int(S * 0.15)
    thick = int(S * 0.046)  # Épais et impactant
    
    # Glow sous l'anneau
    r1_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(0, 229, 255, 120), width=thick + int(S * 0.02))
    ring1_blurred = ring1.filter(ImageFilter.GaussianBlur(int(S * 0.018)))
    
    # Tracé net avec dégradé cyan / sky
    r1_sharp = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    s1_draw = ImageDraw.Draw(r1_sharp)
    s1_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(0, 229, 255, 255), width=thick)
    # Ligne de surbrillance blanche au centre
    s1_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(255, 255, 255, 200), width=int(thick * 0.35))
    
    ring1_comp = Image.alpha_composite(ring1_blurred, r1_sharp)
    ring1_rot = ring1_comp.rotate(-32, resample=Image.BICUBIC, center=(cx, cy))
    
    # Anneau 2 (incliné +40°) - épais et bleu électrique / cyan
    ring2 = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    r2_draw = ImageDraw.Draw(ring2)
    r2_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(59, 130, 246, 120), width=thick + int(S * 0.02))
    ring2_blurred = ring2.filter(ImageFilter.GaussianBlur(int(S * 0.018)))
    
    r2_sharp = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    s2_draw = ImageDraw.Draw(r2_sharp)
    s2_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(37, 99, 235, 255), width=thick)
    s2_draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], outline=(147, 197, 253, 220), width=int(thick * 0.35))
    
    ring2_comp = Image.alpha_composite(ring2_blurred, r2_sharp)
    ring2_rot = ring2_comp.rotate(40, resample=Image.BICUBIC, center=(cx, cy))
    
    # Anneau 3 vertical stylisé (symbole de convergence IA)
    ring3 = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    r3_draw = ImageDraw.Draw(ring3)
    rx3, ry3 = int(S * 0.16), int(S * 0.36)
    r3_draw.ellipse([cx - rx3, cy - ry3, cx + rx3, cy + ry3], outline=(6, 182, 212, 180), width=int(thick * 0.75))
    r3_draw.ellipse([cx - rx3, cy - ry3, cx + rx3, cy + ry3], outline=(255, 255, 255, 180), width=int(thick * 0.25))
    ring3_blurred = ring3.filter(ImageFilter.GaussianBlur(int(S * 0.01)))
    ring3_comp = Image.alpha_composite(ring3_blurred, ring3)
    
    # 3. Cœur quantique (Noyau central éclatant)
    core = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    c_draw = ImageDraw.Draw(core)
    core_r = int(S * 0.11)
    
    # Halo externe doux
    for r in range(int(core_r * 2.2), core_r, -4):
        p = (r - core_r) / (core_r * 1.2)
        alpha = int(70 * (1.0 - p))
        c_draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 229, 255, alpha))
        
    # Sphère centrale solide avec dégradé lumière
    c_draw.ellipse([cx - core_r, cy - core_r, cx + core_r, cy + core_r], fill=(0, 229, 255, 255))
    # Point chaud blanc pur éclatant
    spot_r = int(core_r * 0.55)
    spot_ox, spot_oy = int(cx - core_r * 0.25), int(cy - core_r * 0.25)
    c_draw.ellipse([spot_ox - spot_r, spot_oy - spot_r, spot_ox + spot_r, spot_oy + spot_r], fill=(255, 255, 255, 255))
    
    # Orbes satellites énergétiques sur les anneaux
    for px, py in [(cx + int(rx * 0.78), cy - int(ry * 1.8)), (cx - int(rx * 0.75), cy + int(ry * 1.6))]:
        sb_r = int(S * 0.026)
        c_draw.ellipse([px - sb_r * 2, py - sb_r * 2, px + sb_r * 2, py + sb_r * 2], fill=(0, 229, 255, 80))
        c_draw.ellipse([px - sb_r, py - sb_r, px + sb_r, py + sb_r], fill=(255, 255, 255, 255))
        
    # Assemblage
    final = Image.alpha_composite(bg, ring2_rot)
    final = Image.alpha_composite(final, ring3_comp)
    final = Image.alpha_composite(final, ring1_rot)
    final = Image.alpha_composite(final, core)
    
    # Redimensionnement final avec filtre de haute qualité LANCZOS
    return final.resize((size, size), Image.Resampling.LANCZOS)

def main():
    print("Génération des icônes MEL-IA modernes...")
    
    icon_192 = create_mel_logo(192)
    icon_192.save("static/icons/icon-192.png", format="PNG", optimize=True)
    
    icon_512 = create_mel_logo(512)
    icon_512.save("static/icons/icon-512.png", format="PNG", optimize=True)
    
    # Génération du .ico multi-résolutions pour Windows (16, 32, 48, 64, 128, 256)
    icon_512.save(
        "static/icons/icon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print("[OK] Icones creees avec succes : icon-192.png, icon-512.png, icon.ico")

if __name__ == "__main__":
    main()
