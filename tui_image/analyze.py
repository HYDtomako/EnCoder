from PIL import Image
from collections import Counter

img = Image.open('D:/EnCoder/tui_image/style.png').convert('RGB')
w, h = img.size
px = img.load()


def cls(pxv):
    r, g, b = pxv
    if r > 200 and 130 < g < 210 and b < 150 and r - b > 50:
        return 'beige'
    if r > 170 and 90 < g < 200 and b < 130 and r - b > 80:
        return 'gold'
    if (0.299 * r + 0.587 * g + 0.114 * b) > 80:
        return 'bright'
    return 'dark'


print('=== per-row class counts (every 4px) ===')
prev = ''
for y in range(0, h, 4):
    d = {'dark': 0, 'beige': 0, 'gold': 0, 'bright': 0}
    for x in range(w):
        d[cls(px[x, y])] += 1
    top = max(d, key=d.get)
    if top != prev or y == 0:
        print(f'y={y:4d} dark={d["dark"]:4d} beige={d["beige"]:4d} gold={d["gold"]:4d} bright={d["bright"]:4d}')
        prev = top

print()
print('=== per-column class counts (every 4px) ===')
prev = ''
for x in range(0, w, 4):
    d = {'dark': 0, 'beige': 0, 'gold': 0, 'bright': 0}
    for y in range(h):
        d[cls(px[x, y])] += 1
    top = max(d, key=d.get)
    if top != prev or x == 0:
        print(f'x={x:4d} dark={d["dark"]:4d} beige={d["beige"]:4d} gold={d["gold"]:4d} bright={d["bright"]:4d}')
        prev = top