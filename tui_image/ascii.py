from PIL import Image

img = Image.open('D:/EnCoder/tui_image/style.png').convert('RGB')
w, h = img.size

# ASCII map of layout: reduce to ~120 cols. Gray ramp, plus markers for gold/beige.
cols = 150
rows = 45
cw = w / cols
ch = h / rows

ramp = ' .:-=+*#%@'

out = []
for ry in range(rows):
    line = ''
    for rx in range(cols):
        x0, x1 = int(rx * cw), int((rx + 1) * cw)
        y0, y1 = int(ry * ch), int((ry + 1) * ch)
        lum_sum, gold, beige, n = 0, 0, 0, 0
        for y in range(y0, y1, 2):
            for x in range(x0, x1, 2):
                r, g, b = img.getpixel((x, y))
                n += 1
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                lum_sum += lum
                if r > 170 and 90 < g < 200 and b < 130 and r - b > 80:
                    gold += 1
                if r > 200 and 130 < g < 215 and b < 160 and r - b > 50 and not (r > 170 and 90 < g < 200 and b < 130 and r - b > 80):
                    beige += 1
        avg = lum_sum / max(1, n)
        # priority: gold marker, beige marker, then luminance
        if gold > 0 and gold / max(1, n) > 0.12:
            line += 'G'
        elif beige > 0 and beige / max(1, n) > 0.18:
            line += 'B'
        else:
            idx = min(len(ramp) - 1, int(avg / 256 * len(ramp)))
            line += ramp[idx]
    out.append(line)

for line in out:
    print(line)