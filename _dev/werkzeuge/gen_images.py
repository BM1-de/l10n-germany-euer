#!/usr/bin/env python3
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Banner and icon of l10n_de_euer (static/description).

Runs inside the Odoo container, which has Pillow and the DejaVu fonts::

    docker compose run --rm --no-deps -T --entrypoint python3 odoo - \
        < _dev/werkzeuge/gen_images.py
"""

# pylint: disable=print-used

from PIL import Image, ImageDraw, ImageFont

OUT = "/mnt/extra-addons/l10n_de_euer/static/description/"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
DARK, LIGHT, ACCENT, WHITE = (
    (31, 58, 96),
    (233, 239, 247),
    (52, 120, 184),
    (255, 255, 255),
)


def icon():
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=40, fill=DARK)
    # a sheet of the form with lines and the result
    draw.rounded_rectangle((52, 36, 204, 220), radius=10, fill=WHITE)
    for index, y in enumerate((64, 86, 108, 130)):
        draw.rectangle((70, y, 150, y + 8), fill=LIGHT)
        draw.rectangle((160, y, 186, y + 8), fill=ACCENT if index == 3 else LIGHT)
    draw.rectangle((70, 150, 186, 153), fill=DARK)
    font = ImageFont.truetype(BOLD, 44)
    text = "EÜR"
    width = draw.textlength(text, font=font)
    draw.text(((size - width) / 2, 162), text, font=font, fill=DARK)
    img.resize((128, 128), Image.LANCZOS).save(OUT + "icon.png", optimize=True)


def fitted(draw, text, path, size, width):
    """Font of at most ``size`` points whose ``text`` fits into ``width``."""
    while size > 10:
        font = ImageFont.truetype(path, size)
        if draw.textlength(text, font=font) <= width:
            return font
        size -= 1
    return ImageFont.truetype(path, size)


def banner():
    width, height = 1120, 560
    margin = 80
    img = Image.new("RGB", (width, height), DARK)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, height - 120, width, height), fill=ACCENT)
    rows = (
        ("Anlage EÜR · AVEÜR", BOLD, 84, WHITE, 100),
        ("Einnahmenüberschussrechnung nach § 4 Abs. 3 EStG", REGULAR, 38, LIGHT, 240),
        ("nach Zahlung · bis zum Beleg nachvollziehbar", REGULAR, 38, LIGHT, 300),
        (
            "Odoo 19 · Kleinunternehmer und Regelbesteuerung · SKR03 und SKR04",
            REGULAR,
            30,
            WHITE,
            height - 82,
        ),
    )
    for text, path, size, color, top in rows:
        font = fitted(draw, text, path, size, width - 2 * margin)
        draw.text((margin, top), text, font=font, fill=color)
    img.save(OUT + "banner.png", optimize=True)


icon()
banner()
print("ok")
