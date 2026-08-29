from PIL import Image
from collections import Counter

img = Image.open('D:/EnCoder/tui_image/style.png').convert('RGB')
w, h = img.size
px = img.load()


def region_stats(x0, x1, y0, y1, label):
    c = Counter()
    for y in range(y0, y1, 2):
        for x in range(x0, x1, 2):
            r, g, b = px[x, y]
            c[(r // 16 * 16, g // 16 * 16, b // 16 * 16)] += 1
    print(f'--- {label} ({x0},{y0})-({x1},{y1}) ---')
    for col, n in c.most_common(6):
        r, g, b = col
        print(f'   #{r:02x}{g:02x}{b:02x} rgb({r},{g},{b})  n={n}')
    print()


region_stats(0, w, 0, 12, 'outer top edge')
region_stats(0, w, 13, 32, 'title bar')
region_stats(21, 1157, 61, 120, 'main content top-left')
region_stats(21, 400, 61, 997, 'main left column')
region_stats(400, 1157, 61, 997, 'main right column')
region_stats(1182, 1507, 61, 997, 'right sidebar')
region_stats(1155, 1161, 61, 997, 'divider strip')
region_stats(0, w, 32, 61, 'gap between title and content')

# find where beige color appears
beige_locs = []
for y in range(h):
    for x in range(w):
        r, g, b = px[x, y]
        if r > 220 and 150 < g < 210 and b < 160 and r - b > 60:
            beige_locs.append((x, y))
print('beige pixel count:', len(beige_locs))
if beige_locs:
    xs = [p[0] for p in beige_locs]
    ys = [p[1] for p in beige_locs]
    print('beige bbox: x', min(xs), '-', max(xs), ' y', min(ys), '-', max(ys))