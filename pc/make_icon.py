"""Vẽ biểu tượng app (giống biểu tượng trên Android) và lưu thành tệp .ico cho Windows."""
import sys
from PIL import Image, ImageDraw
S = 256
im = Image.new("RGBA", (S, S), (11, 57, 84, 255))
d = ImageDraw.Draw(im)
k = S / 108
d.polygon([(0, 72 * k), (27 * k, 62 * k), (54 * k, 72 * k), (81 * k, 82 * k), (108 * k, 72 * k), (108 * k, 108 * k), (0, 108 * k)], fill=(30, 136, 229))
d.polygon([(54 * k, 18 * k), (74 * k, 66 * k), (54 * k, 56 * k), (34 * k, 66 * k)], fill="white")
d.ellipse([48 * k, 76 * k, 60 * k, 88 * k], fill=(255, 213, 74))
im.save(sys.argv[1] if len(sys.argv) > 1 else "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
