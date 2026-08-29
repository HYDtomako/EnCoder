from PIL import Image
from collections import Counter

img = Image.open('D:/EnCoder/tui_image/style.png').convert('RGB')
w, h = img.size
px = img.load()

def sample(x0, x1, y0, y1, label):
    c = Counter()
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            if (r, g, b) == (0, 0, 0):
                continue  # skip pure black (bg/frame)
            c[(r, g, b)] += 1
    print(f'--- {label} ---')
    for col, n in c.most_common(10):
        r, g, b = col
        print(f'   #{r:02x}{g:02x}{b:02x}  n={n}')
    print()

# left gold logo block (rows 3-8 in ascii = y~68..204, x~35..200)
sample(35, 200, 68, 210, 'left gold logo block')
# right gold card (ascii rows 5-14 = y~113..330, x~1182..1490)
sample(1185, 1490, 115, 335, 'right gold banner card')
# sidebar list area behind banner
sample(1200, 1490, 360, 900, 'sidebar list region')
# main text paragraph area
sample(120, 900, 240, 420, 'main paragraph area')
# title bar text
sample(20, 600, 14, 30, 'title bar text')
# fine divider colors
sample(1155, 1162, 61, 997, 'divider')