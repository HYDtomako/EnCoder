from PIL import Image
from collections import Counter

img = Image.open('D:/EnCoder/tui_image/style.png').convert('RGB')
w, h = img.size
px = img.load()

def is_content(c):
    r, g, b = c
    return (0.299 * r + 0.587 * g + 0.114 * b) > 40

# --- content bounding box -------------------------------------------------
xs, ys = [], []
for y in range(h):
    for x in range(w):
        if is_content(px[x, y]):
            xs.append(x); ys.append(y)
print('content bbox: x', min(xs), '-', max(xs), ' y', min(ys), '-', max(ys))

# --- find vertical content bands (groups of columns with any content) ----
col_any = [any(is_content(px[x, y]) for y in range(h)) for x in range(w)]
bands = []
cur = None
for x in range(w):
    if col_any[x]:
        if cur is None:
            cur = [x, x]
        else:
            cur[1] = x
    else:
        if cur:
            bands.append(tuple(cur)); cur = None
if cur:
    bands.append(tuple(cur))
print('\nvertical content bands (x ranges):')
for b in bands:
    print('  x', b)

# --- find horizontal content bands ---------------------------------------
row_any = [any(is_content(px[x, y]) for x in range(w)) for y in range(h)]
bands = []
cur = None
for y in range(h):
    if row_any[y]:
        if cur is None:
            cur = [y, y]
        else:
            cur[1] = y
    else:
        if cur:
            bands.append(tuple(cur)); cur = None
if cur:
    bands.append(tuple(cur))
print('\nhorizontal content bands (y ranges):')
for b in bands:
    print('  y', b)