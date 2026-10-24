"""SVG source of the Xeno Saber BLE icon, in light and dark variants.

An original, generic energy blade with radio arcs. Drawn upright, then
rotated 45 degrees so the blade points to the top right.
"""

import math
import sys

BLADE = "#00E5A0"  # teal-green glow
BLADE_DEEP = "#00B37D"


def arc_path(x: float, y: float, r: float, half_angle: float = 48) -> str:
    a = math.radians(half_angle)
    x0, y0 = x + r * math.cos(a), y - r * math.sin(a)
    x1, y1 = x + r * math.cos(a), y + r * math.sin(a)
    return f'<path d="M {x0:.1f} {y0:.1f} A {r} {r} 0 0 1 {x1:.1f} {y1:.1f}"/>'


def icon_svg(dark: bool) -> str:
    hilt = "#C9CED6" if dark else "#2B3038"
    hilt_hi = "#EEF1F5" if dark else "#525A66"
    ring = "#7E858F" if dark else "#14171C"
    arc = BLADE if dark else BLADE_DEEP
    cx = 256
    blade_top, blade_bot, bw = 4, 332, 48  # bw = blade width
    hilt_top, hilt_bot, hw = 332, 490, 80  # hw = hilt width
    ew = 94  # emitter collar width
    bx, by = cx + 16, 390  # activation button
    rings = "".join(
        f'<rect x="{cx - hw / 2}" y="{y}" width="{hw}" height="11" fill="{ring}" opacity="0.85"/>'
        for y in (hilt_top + 84, hilt_top + 104, hilt_top + 124)
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <filter id="glow" x="-100%" y="-30%" width="300%" height="160%">
      <feGaussianBlur stdDeviation="16"/>
    </filter>
    <filter id="glow2" x="-100%" y="-30%" width="300%" height="160%">
      <feGaussianBlur stdDeviation="6"/>
    </filter>
    <linearGradient id="hilt" x1="0" x2="1" y1="0" y2="0">
      <stop offset="0" stop-color="{hilt_hi}"/>
      <stop offset="0.4" stop-color="{hilt}"/>
      <stop offset="1" stop-color="{ring}"/>
    </linearGradient>
  </defs>
  <g transform="rotate(45 256 256)">
    <rect x="{cx - bw}" y="{blade_top - 10}" width="{bw * 2}" height="{blade_bot - blade_top + 10}"
          rx="{bw}" fill="{BLADE}" opacity="{0.5 if dark else 0.4}" filter="url(#glow)"/>
    <rect x="{cx - bw / 2 - 5}" y="{blade_top - 4}" width="{bw + 10}" height="{blade_bot - blade_top + 4}"
          rx="{bw / 2 + 5}" fill="{BLADE}" filter="url(#glow2)"/>
    <rect x="{cx - bw / 2}" y="{blade_top}" width="{bw}" height="{blade_bot - blade_top}"
          rx="{bw / 2}" fill="{BLADE}"/>
    <rect x="{cx - bw / 2 + 12}" y="{blade_top + 12}" width="{bw - 24}" height="{blade_bot - blade_top - 12}"
          rx="{(bw - 24) / 2}" fill="#FFFFFF" opacity="0.95"/>
    <g fill="none" stroke="{arc}" stroke-width="18" stroke-linecap="round">
      {arc_path(bx, by, 52, 44)}
      {arc_path(bx, by, 88, 44)}
    </g>
    <rect x="{cx - ew / 2}" y="{hilt_top}" width="{ew}" height="32" rx="8" fill="{ring}"/>
    <rect x="{cx - hw / 2}" y="{hilt_top + 26}" width="{hw}" height="{hilt_bot - hilt_top - 26}"
          rx="16" fill="url(#hilt)"/>
    {rings}
    <circle cx="{bx}" cy="{by}" r="13" fill="{BLADE}"/>
  </g>
</svg>
"""


if __name__ == "__main__":
    out = sys.argv[1]
    for dark in (False, True):
        name = "icon_dark.svg" if dark else "icon.svg"
        with open(f"{out}/{name}", "w") as f:
            f.write(icon_svg(dark))
