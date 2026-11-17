# -*- coding: utf-8 -*-
"""Đọc hải đồ raster BSB/KAP (NOAA, UKHO, hải đồ quét...) và cắt ra ô 256x256 theo
chuẩn Web Mercator để hiển thị chung với MBTiles.

Cách làm: giải mã toàn bộ ảnh chỉ số màu một lần ra tệp đệm (memmap, không chiếm RAM),
dùng các điểm REF/ để khớp phép biến đổi (kinh độ, Mercator Y) -> (x, y) ảnh bằng bình
phương tối thiểu, rồi lấy mẫu từng ô; vùng ngoài đường viền PLY/ để trong suốt."""
import hashlib
import io
import math
import os
import re
import threading

import numpy as np
from PIL import Image, ImageDraw


def is_kap(path):
    try:
        with open(path, "rb") as f:
            h = f.read(4096)
        return b"RA=" in h and (b"BSB/" in h or b"NOS/" in h) and b"\x1a" in open(path, "rb").read(1 << 20)
    except Exception:
        return False


def _merc(lat):
    lat = max(-85.05, min(85.05, lat))
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _terms(lon, m, order):
    one = np.ones_like(lon)
    t = [one, lon, m]
    if order == 2:
        t += [lon * lon, lon * m, m * m]
    elif order == 3:
        t += [lon * lon, lon * m, m * m, lon ** 3, lon * lon * m, lon * m * m, m ** 3]
    return np.stack(t, axis=-1)


class KapChart:
    def __init__(self, path, cache_dir):
        self.path = path
        self.lock = threading.Lock()
        self.img = None
        data = open(path, "rb").read()
        end = data.find(b"\x1a")
        if end < 0:
            raise ValueError("không có phần đầu KAP")
        hdr = data[:end].decode("latin-1")
        # nối các dòng tiếp nối (bắt đầu bằng 4 dấu cách)
        lines = []
        for ln in re.split(r"\r?\n", hdr):
            if ln.startswith("    ") and lines:
                lines[-1] += "," + ln.strip()
            elif ln.strip() and not ln.startswith("!"):
                lines.append(ln.strip())
        self.w = self.h = 0
        self.name = ""
        pal, refs, ply = {}, [], []
        dlat = dlon = 0.0
        for ln in lines:
            tag, _, body = ln.partition("/")
            tag = tag.upper()
            if tag in ("BSB", "NOS"):
                m = re.search(r"RA=\s*(\d+)\s*,\s*(\d+)", body)
                if m:
                    self.w, self.h = int(m.group(1)), int(m.group(2))
                m = re.search(r"NA=([^,]*)", body)
                if m and m.group(1).strip():
                    self.name = m.group(1).strip()
            elif tag == "RGB":
                p = [x.strip() for x in body.split(",")]
                if len(p) >= 4:
                    pal[int(p[0])] = (int(p[1]), int(p[2]), int(p[3]))
            elif tag == "REF":
                p = [float(x) for x in body.split(",")[:5]]
                if len(p) == 5:
                    refs.append(p[1:])
            elif tag == "PLY":
                p = [float(x) for x in body.split(",")[:3]]
                if len(p) == 3:
                    ply.append((p[1], p[2]))
            elif tag == "DTM":
                p = [float(x) for x in body.split(",")[:2]]
                if len(p) == 2:
                    dlat, dlon = p[0] / 3600.0, p[1] / 3600.0
        if not (self.w and self.h) or len(refs) < 3:
            raise ValueError("thiếu RA= hoặc điểm REF")
        p = end + 1
        while p < len(data) and data[p] == 0:
            p += 1
        self.depth = data[p] if p < len(data) else 7
        if not 1 <= self.depth <= 7:
            self.depth = 7
        self.rows_at = p + 1
        self.pal = np.zeros((256, 3), np.uint8)
        for k, v in pal.items():
            if 0 <= k < 256:
                self.pal[k] = v
        # Phép khớp: (lon, mercY) -> (x, y)
        r = np.array(refs, float)
        lat, lon = r[:, 2] + dlat, r[:, 3] + dlon
        self.lon0 = float(lon.mean())
        mm = np.array([_merc(a) for a in lat])
        self.m0 = float(mm.mean())
        L, M = np.radians(lon - self.lon0), mm - self.m0
        self.order = 3 if len(r) >= 20 else 2 if len(r) >= 8 else 1
        A = _terms(L, M, self.order)
        self.cx = np.linalg.lstsq(A, r[:, 0], rcond=None)[0]
        self.cy = np.linalg.lstsq(A, r[:, 1], rcond=None)[0]
        A1 = _terms(L, M, 1)
        # Phép ngược bậc 1 (x,y -> lon, merc) để tính vùng phủ và độ phân giải
        B = np.stack([np.ones(len(r)), r[:, 0], r[:, 1]], -1)
        il = np.linalg.lstsq(B, lon, rcond=None)[0]
        im = np.linalg.lstsq(B, mm, rcond=None)[0]
        del A1
        self.ply = [(a + dlat, b + dlon) for a, b in ply]
        if len(self.ply) >= 3:
            lats = [a for a, _ in self.ply]; lons = [b for _, b in self.ply]
        else:
            cs = [(0, 0), (self.w, 0), (0, self.h), (self.w, self.h)]
            lons = [il[0] + il[1] * x + il[2] * y for x, y in cs]
            lats = [math.degrees(math.atan(math.sinh(im[0] + im[1] * x + im[2] * y))) for x, y in cs]
        self.bounds = [min(lons), min(lats), max(lons), max(lats)]
        dpp = math.hypot(il[1], il[2]) or 1e-6           # độ kinh / điểm ảnh
        native = math.log2(360.0 / (256 * dpp))
        self.maxzoom = max(1, min(22, int(round(native))))
        self.minzoom = max(1, self.maxzoom - 7)
        self.native = native
        key = hashlib.sha1((os.path.normcase(os.path.abspath(path)) + str(os.path.getsize(path)) +
                            str(int(os.path.getmtime(path)))).encode("utf-8")).hexdigest()
        self.cache = os.path.join(cache_dir, key)
        os.makedirs(self.cache, exist_ok=True)

    # ---------- giải mã ảnh ----------
    def _decode_all(self, data):
        w, h, depth = self.w, self.h, self.depth
        out = np.zeros((h, w), np.uint8)
        cshift = 7 - depth
        cmask = ((1 << depth) - 1) << cshift
        nmask = (1 << cshift) - 1
        # bảng chỉ mục dòng ở cuối tệp (nếu có)
        offs = None
        try:
            ti = int.from_bytes(data[-4:], "big")
            if self.rows_at <= ti and ti + 4 * h <= len(data) - 4:
                o = np.frombuffer(data, ">u4", h, ti).astype(np.int64)
                if o.min() >= self.rows_at and o.max() < ti:
                    offs = o
        except Exception:
            offs = None
        p = self.rows_at
        n = len(data)
        for row in range(h):
            if offs is not None:
                p = int(offs[row])
            # số dòng (varint)
            rn = 0
            while p < n:
                b = data[p]; p += 1
                rn = (rn << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            y = rn - 1 if offs is None else row
            if not 0 <= y < h:
                y = row
            cols, cnts = [], []
            while p < n:
                c = data[p]; p += 1
                if c == 0:
                    break
                col = (c & cmask) >> cshift
                cnt = c & nmask
                while c & 0x80 and p < n:
                    c = data[p]; p += 1
                    cnt = (cnt << 7) + (c & 0x7F)
                cols.append(col); cnts.append(cnt + 1)
            if cols:
                line = np.repeat(np.array(cols, np.uint8), np.array(cnts, np.int64))
                k = min(w, len(line))
                out[y, :k] = line[:k]
            if p >= n:
                break
        return out

    def _image(self):
        if self.img is not None:
            return self.img
        with self.lock:
            if self.img is None:
                f = os.path.join(self.cache, "idx.u8")
                if not (os.path.exists(f) and os.path.getsize(f) == self.w * self.h):
                    arr = self._decode_all(open(self.path, "rb").read())
                    tmp = f + ".tmp"
                    arr.tofile(tmp)
                    os.replace(tmp, f)
                    del arr
                self.img = np.memmap(f, np.uint8, "r", shape=(self.h, self.w))
        return self.img

    # ---------- cắt ô ----------
    def tile(self, z, x, y):
        f = os.path.join(self.cache, str(z), str(x), f"{y}.png")
        if os.path.exists(f):
            d = open(f, "rb").read()
            return d or None
        d = self._render(z, x, y)
        try:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f + ".tmp", "wb") as o:
                o.write(d or b"")
            os.replace(f + ".tmp", f)
        except Exception:
            pass
        return d

    def _render(self, z, x, y):
        n = 1 << z
        lon_w = x / n * 360 - 180
        lon_e = (x + 1) / n * 360 - 180
        lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
        b = self.bounds
        if lon_e < b[0] or lon_w > b[2] or lat_n < b[1] or lat_s > b[3]:
            return None
        img = self._image()
        S = 256
        t = (np.arange(S) + 0.5) / S
        L = np.radians(lon_w + (lon_e - lon_w) * t) - math.radians(self.lon0)
        M = math.pi * (1 - 2 * (y + t) / n) - self.m0
        LL, MM = np.meshgrid(L, M)
        A = _terms(LL, MM, self.order)
        px = np.floor(A @ self.cx).astype(np.int64)
        py = np.floor(A @ self.cy).astype(np.int64)
        ok = (px >= 0) & (px < self.w) & (py >= 0) & (py < self.h)
        if not ok.any():
            return None
        idx = np.zeros((S, S), np.uint8)
        idx[ok] = img[py[ok], px[ok]]
        rgba = np.zeros((S, S, 4), np.uint8)
        rgba[..., :3] = self.pal[idx]
        alpha = ok & (idx != 0)
        if len(self.ply) >= 3:
            poly = [(((lon - lon_w) / (lon_e - lon_w)) * S,
                     (math.pi * (1 - 2 * y / n) - _merc(lat)) / (2 * math.pi / n) * S) for lat, lon in self.ply]
            mk = Image.new("L", (S, S), 0)
            ImageDraw.Draw(mk).polygon(poly, fill=255)
            alpha &= np.asarray(mk) > 0
        if not alpha.any():
            return None
        rgba[..., 3] = alpha * 255
        bio = io.BytesIO()
        Image.fromarray(rgba, "RGBA").save(bio, "PNG", optimize=False, compress_level=3)
        return bio.getvalue()


def write_test_kap(path, lon0=106.0, lon1=107.0, lat0=10.0, lat1=11.0):
    """Tệp KAP nhỏ (64x64, một màu) dùng cho tự kiểm tra bản đóng gói."""
    W = H = 64
    hdr = ["VER/3.0", f"BSB/NA=Thu KAP,NU=1,RA={W},{H},DU=254", "RGB/1,200,100,50"]
    for i, (x, y, la, lo) in enumerate([(0, 0, lat1, lon0), (W, 0, lat1, lon1), (0, H, lat0, lon0), (W, H, lat0, lon1)]):
        hdr.append(f"REF/{i + 1},{x},{y},{la},{lo}")
    out = bytearray(("\r\n".join(hdr) + "\r\n").encode()) + b"\x1a\x00\x07"
    for y in range(H):
        out += bytes([y + 1, 0x81, W - 1, 0])
    open(path, "wb").write(out)
