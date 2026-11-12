#!/usr/bin/env python3
"""
Tạo hải đồ độ sâu dạng MBTiles từ GEBCO_2026 (mặc định) hoặc ETOPO 2022, độ phân giải 15 giây cung.

- GEBCO_2026 Grid, GEBCO Compilation Group (2026), doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa
  (dữ liệu công cộng, được dùng cả cho mục đích thương mại, cần ghi nguồn).
- ETOPO 2022 15 Arc-Second Global Relief Model, NOAA NCEI, doi:10.25921/fd45-gt74 (dự phòng).
- Kết quả: vùng nước tô màu theo dải độ sâu, đường đẳng sâu và số độ sâu tham khảo;
  đất liền tô màu nền đất để dùng được khi không có mạng.

Cách dùng:
  python make_depth_mbtiles.py --bbox 99,0,122.5,24 --minzoom 4 --maxzoom 10 --out ra.mbtiles
  python make_depth_mbtiles.py --nc tep1.nc tep2.nc ...   (dùng tệp NetCDF đã có sẵn)
"""
import argparse, io, math, os, sqlite3, sys, time, urllib.request, warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage
from scipy.io import netcdf_file

ERDDAP = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/ETOPO_2022_v1_15s.nc?z[({s}):({n})][({w}):({e})]"
THREDDS = "https://www.ngdc.noaa.gov/thredds/fileServer/global/ETOPO2022/15s/15s_surface_elev_netcdf/ETOPO_2022_v1_15s_{tile}_surface.nc"
GEBCO_ZIP = os.environ.get("GEBCO_URL") or "https://dap.ceda.ac.uk/bodc/gebco/global/gebco_2026/ice_surface_elevation/geotiff/gebco_2026_geotiff.zip?download=1"
UA = {"User-Agent": "HaiDoMo-depth-tiles/1.0 (offline nautical app; contact via GitHub)"}

# ---------------- Bảng màu (giống hải đồ: nước nông đậm, nước sâu nhạt) ----------------
EDGES = [0, 2, 5, 10, 20, 30, 50, 100, 200, 500, 1000, 2000]   # mét
WATER = [(96, 160, 220), (118, 176, 228), (142, 192, 235), (165, 206, 240), (184, 218, 244), (200, 227, 247),
         (214, 235, 249), (226, 241, 251), (235, 246, 253), (242, 249, 254), (247, 251, 255), (251, 253, 255)]
LAND = (241, 236, 220)
IDX_TRANSP, IDX_LAND, IDX_LINE, IDX_LINE_MAJOR, IDX_TEXT, IDX_TEXT_SHALLOW = 0, 1, 2, 3, 4, 5
IDX_WATER0 = 8
MAJOR = {10, 20, 50, 200, 1000}


def palette():
    pal = [(0, 0, 0)] * 256
    pal[IDX_LAND] = LAND
    pal[IDX_LINE] = (110, 150, 195)
    pal[IDX_LINE_MAJOR] = (55, 100, 160)
    pal[IDX_TEXT] = (40, 60, 85)
    pal[IDX_TEXT_SHALLOW] = (160, 20, 40)
    for i, c in enumerate(WATER):
        pal[IDX_WATER0 + i] = c
    return [v for c in pal for v in c]


# ---------------- Tải dữ liệu ----------------
def fetch(url, dest, tries=5):
    for k in range(tries):
        try:
            print("  tải", url, flush=True)
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=900) as r, open(dest + ".part", "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(dest + ".part", dest)
            print("  xong", os.path.getsize(dest) // 1048576, "MB", flush=True)
            return True
        except Exception as e:
            print("  lỗi:", e, "- thử lại", flush=True)
            time.sleep(20 * (k + 1))
    return False


def read_nc3(path):
    f = netcdf_file(path, "r", mmap=False)
    v = f.variables
    lat = np.array(v["latitude"].data if "latitude" in v else v["lat"].data, dtype=np.float64)
    lon = np.array(v["longitude"].data if "longitude" in v else v["lon"].data, dtype=np.float64)
    zv = v["z"]
    z = np.array(zv.data, dtype=np.float32)
    fill = getattr(zv, "_FillValue", None)
    if fill is not None:
        z[z == np.float32(fill)] = np.nan
    f.close()
    return lat, lon, z


def read_nc4(path):
    import h5py  # tệp THREDDS là NetCDF-4 (HDF5)
    with h5py.File(path, "r") as f:
        lat = f["lat"][:].astype(np.float64)
        lon = f["lon"][:].astype(np.float64)
        z = f["z"][:].astype(np.float32)
    return lat, lon, z


def read_any(path):
    with open(path, "rb") as f:
        head = f.read(4)
    return read_nc3(path) if head[:3] == b"CDF" else read_nc4(path)


def assemble(parts, bbox):
    """Ghép các mảnh lưới (lat, lon, z) thành một lưới đều phủ bbox, vĩ độ tăng dần."""
    w, s, e, n = bbox
    step = 1 / 240.0
    lats = np.arange(math.floor(s / step) * step + step / 2, n + step, step)
    lons = np.arange(math.floor(w / step) * step + step / 2, e + step, step)
    grid = np.full((len(lats), len(lons)), np.nan, dtype=np.float32)
    for lat, lon, z in parts:
        if lat[0] > lat[-1]:
            lat, z = lat[::-1], z[::-1]
        if lon[0] > lon[-1]:
            lon, z = lon[::-1], z[:, ::-1]
        r0 = int(round((lat[0] - lats[0]) / step)); c0 = int(round((lon[0] - lons[0]) / step))
        rs0, cs0 = max(0, -r0), max(0, -c0)
        rd0, cd0 = max(0, r0), max(0, c0)
        nr = min(len(lat) - rs0, len(lats) - rd0); nc = min(len(lon) - cs0, len(lons) - cd0)
        if nr > 0 and nc > 0:
            sub = z[rs0:rs0 + nr, cs0:cs0 + nc]
            dst = grid[rd0:rd0 + nr, cd0:cd0 + nc]
            m = ~np.isnan(sub)
            dst[m] = sub[m]
    miss = np.isnan(grid).mean()
    print(f"Lưới {grid.shape[1]}x{grid.shape[0]}, thiếu dữ liệu {miss*100:.2f}%", flush=True)
    return lats, lons, grid


def download(bbox, workdir):
    w, s, e, n = bbox
    os.makedirs(workdir, exist_ok=True)
    parts = []
    # 1) ERDDAP: cắt theo dải vĩ độ 4 độ cho mỗi lần tải
    ok = True
    la = s
    while la < n - 1e-9:
        lb = min(n, la + 4)
        dest = os.path.join(workdir, f"etopo_{la:.2f}_{lb:.2f}.nc")
        if not os.path.exists(dest):
            ok = fetch(ERDDAP.format(s=la, n=lb, w=w, e=e), dest)
            if not ok:
                break
        parts.append(read_any(dest))
        la = lb
    if ok:
        return parts
    # 2) Dự phòng: các ô 15x15 độ trên THREDDS
    print("ERDDAP lỗi, chuyển sang THREDDS", flush=True)
    parts = []
    for top in range(int(math.ceil(s / 15.0)) * 15 + 15, int(math.ceil(n / 15.0)) * 15 + 1, 15):
        for left in range(int(math.floor(w / 15.0)) * 15, int(math.ceil(e / 15.0)) * 15, 15):
            tile = f"{'N' if top >= 0 else 'S'}{abs(top):02d}{'E' if left >= 0 else 'W'}{abs(left):03d}"
            dest = os.path.join(workdir, tile + ".nc")
            if not os.path.exists(dest) and not fetch(THREDDS.format(tile=tile), dest):
                sys.exit("Không tải được dữ liệu ETOPO")
            parts.append(read_any(dest))
    return parts


# ---------------- GEBCO: chỉ lấy ô GeoTIFF 90x90 độ cần dùng trong tệp zip lớn ----------------
class HttpFile(io.RawIOBase):
    """Tệp trên mạng đọc theo đoạn (HTTP Range) để zipfile chỉ tải phần cần thiết."""
    def __init__(self, url):
        self.url, self.pos = url, 0
        req = urllib.request.Request(url, headers={**UA, "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            cr = r.headers.get("Content-Range")
            if r.status != 206 or not cr:
                raise IOError("máy chủ không hỗ trợ tải từng đoạn")
            self.size = int(cr.split("/")[-1])
            self.url = r.geturl()
        self.nreq = 0
    def readable(self): return True
    def seekable(self): return True
    def tell(self): return self.pos
    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos
    def readinto(self, b):
        if self.pos >= self.size:
            return 0
        end = min(self.size, self.pos + len(b)) - 1
        for k in range(6):
            try:
                req = urllib.request.Request(self.url, headers={**UA, "Range": f"bytes={self.pos}-{end}"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read()
                break
            except Exception as e:
                print("  lỗi đoạn, thử lại:", e, flush=True); time.sleep(10 * (k + 1))
        else:
            raise IOError("không tải được đoạn dữ liệu")
        n = len(data); b[:n] = data; self.pos += n; self.nreq += 1
        return n


def tile_bounds(name):
    import re
    m = re.search(r"n(-?\d+(?:\.\d+)?)_s(-?\d+(?:\.\d+)?)_w(-?\d+(?:\.\d+)?)_e(-?\d+(?:\.\d+)?)", name)
    return tuple(float(v) for v in m.groups()) if m else None   # n, s, w, e


def download_gebco(bbox, workdir):
    import zipfile, shutil
    w, s, e, n = bbox
    os.makedirs(workdir, exist_ok=True)
    try:
        src = io.BufferedReader(HttpFile(GEBCO_ZIP), buffer_size=8 << 20)
        print("Máy chủ GEBCO cho tải từng đoạn: chỉ tải ô cần dùng", flush=True)
    except Exception as ex:
        print("Không tải từng đoạn được (", ex, ") - tải cả tệp zip ~4 GB", flush=True)
        zpath = os.path.join(workdir, "gebco_2026_geotiff.zip")
        if not os.path.exists(zpath) and not fetch(GEBCO_ZIP, zpath):
            raise IOError("không tải được GEBCO")
        src = open(zpath, "rb")
    parts = []
    with zipfile.ZipFile(src) as zf:
        for info in zf.infolist():
            tb = tile_bounds(info.filename)
            if not tb or not info.filename.lower().endswith((".tif", ".tiff")):
                continue
            tn, ts, tw, te = tb
            if tw >= e or te <= w or ts >= n or tn <= s:
                continue
            dest = os.path.join(workdir, os.path.basename(info.filename))
            if not os.path.exists(dest):
                print(f"  giải nén {info.filename} ({info.compress_size >> 20} MB nén)", flush=True)
                t0 = time.time()
                with zf.open(info) as fi, open(dest + ".part", "wb") as fo:
                    shutil.copyfileobj(fi, fo, 8 << 20)
                os.replace(dest + ".part", dest)
                print(f"  xong sau {time.time()-t0:.0f}s", flush=True)
            parts.append(read_geotiff_window(dest, (tn, ts, tw, te), bbox))
    if not parts:
        raise IOError("không tìm thấy ô GEBCO phủ vùng yêu cầu")
    return parts


def read_geotiff_window(path, tb, bbox):
    import tifffile
    tn, ts, tw, te = tb
    w, s, e, n = bbox
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        H, Wd = page.shape[:2]
        step_y, step_x = (tn - ts) / H, (te - tw) / Wd
        try:
            arr = tf.asarray(out="memmap")
        except Exception:
            arr = page.asarray()
    r0 = max(0, int(math.floor((tn - min(n, tn)) / step_y)) - 2); r1 = min(H, int(math.ceil((tn - max(s, ts)) / step_y)) + 2)
    c0 = max(0, int(math.floor((max(w, tw) - tw) / step_x)) - 2); c1 = min(Wd, int(math.ceil((min(e, te) - tw) / step_x)) + 2)
    z = np.array(arr[r0:r1, c0:c1], dtype=np.float32)
    lat = tn - (np.arange(r0, r1) + 0.5) * step_y
    lon = tw + (np.arange(c0, c1) + 0.5) * step_x
    print(f"  đọc {os.path.basename(path)}: {z.shape[1]}x{z.shape[0]} điểm", flush=True)
    return lat, lon, z


# ---------------- Ô bản đồ ----------------
def lon2tx(lon, z): return (lon + 180.0) / 360.0 * (1 << z)
def lat2ty(lat, z):
    r = math.radians(lat)
    return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (1 << z)
def tx2lon(x, z): return x / (1 << z) * 360.0 - 180.0
def ty2lat(y, z): return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / (1 << z)))))


class Pyramid:
    """Các mức lưới thu nhỏ (trung bình 2x2) để vẽ mức zoom thấp không bị răng cưa."""
    def __init__(self, lats, lons, grid):
        self.levels = [(lats, lons, grid)]
        while len(self.levels[-1][0]) > 600:
            la, lo, g = self.levels[-1]
            r, c = (g.shape[0] // 2) * 2, (g.shape[1] // 2) * 2
            g2 = np.nanmean(g[:r, :c].reshape(r // 2, 2, c // 2, 2), axis=(1, 3)).astype(np.float32)
            self.levels.append((la[:r].reshape(-1, 2).mean(1), lo[:c].reshape(-1, 2).mean(1), g2))

    def pick(self, deg_per_px):
        best = self.levels[0]
        for lv in self.levels:
            if (lv[0][1] - lv[0][0]) <= deg_per_px * 1.2:
                best = lv
        return best


def sample(pyr, z, tx, ty, size=256):
    lon_a = np.array([tx2lon(tx + (i + 0.5) / size, z) for i in range(size)])
    lat_a = np.array([ty2lat(ty + (j + 0.5) / size, z) for j in range(size)])
    lats, lons, g = pyr.pick((360.0 / (1 << z)) / size)
    dlat, dlon = lats[1] - lats[0], lons[1] - lons[0]
    rows = (lat_a - lats[0]) / dlat
    cols = (lon_a - lons[0]) / dlon
    R, C = np.meshgrid(rows, cols, indexing="ij")
    filled = np.where(np.isnan(g), 0.0, g)
    val = ndimage.map_coordinates(filled, [R, C], order=1, mode="nearest")
    nanm = ndimage.map_coordinates(np.isnan(g).astype(np.float32), [R, C], order=1, mode="nearest") > 0.5
    outside = (R < -0.5) | (R > len(lats) - 0.5) | (C < -0.5) | (C > len(lons) - 0.5)
    val[nanm | outside] = np.nan
    return val


def get_font(px):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, px)
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


FONT = None


def render(elev, z, pal):
    """elev: mảng 256x256 độ cao (âm = độ sâu). Trả về PNG bytes hoặc None nếu ô trống."""
    global FONT
    nod = np.isnan(elev)
    if nod.all():
        return None
    depth = -elev
    water = (~nod) & (depth > 0)
    band = np.digitize(np.where(water, depth, 0), EDGES[1:])          # 0..len(EDGES)-1
    img = np.full(elev.shape, IDX_TRANSP, np.uint8)
    img[(~nod) & ~water] = IDX_LAND
    img[water] = (IDX_WATER0 + band[water]).astype(np.uint8)
    # đường đẳng sâu: ranh giới giữa hai dải màu
    if z >= 6:
        b = np.where(water, band, -1)
        right = np.zeros_like(water); right[:, :-1] = (b[:, :-1] != b[:, 1:]) & water[:, :-1] & water[:, 1:]
        down = np.zeros_like(water); down[:-1, :] = (b[:-1, :] != b[1:, :]) & water[:-1, :] & water[1:, :]
        edge = right | down
        hi = np.maximum(b, np.roll(b, -1, 1))
        hi2 = np.maximum(b, np.roll(b, -1, 0))
        edge_band = np.where(right, hi, hi2)
        major = np.isin(np.array(EDGES)[np.clip(edge_band, 0, len(EDGES) - 1)], list(MAJOR))
        if z < 8:
            edge &= major
        img[edge & ~major] = IDX_LINE
        img[edge & major] = IDX_LINE_MAJOR
    # số độ sâu tham khảo
    if z >= 9:
        if FONT is None:
            FONT = get_font(11)
        im = Image.fromarray(img, "P")
        d = ImageDraw.Draw(im)
        stepp = 64 if z >= 10 else 85
        for y in range(stepp // 2, 256, stepp):
            for x in range(stepp // 2, 256, stepp):
                if not water[y, x]:
                    continue
                v = depth[y, x]
                t = (f"{v:.1f}".rstrip("0").rstrip(".") if v < 10 else str(int(round(v))))
                d.text((x, y), t, fill=IDX_TEXT_SHALLOW if v < 5 else IDX_TEXT, font=FONT, anchor="mm")
        img = np.array(im)
    im = Image.fromarray(img, "P")
    im.putpalette(pal)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True, transparency=IDX_TRANSP)
    return out.getvalue()


NAME = "Độ sâu GEBCO 2026 – Biển Đông & Vịnh Thái Lan"
DESC = "Độ sâu tham khảo từ GEBCO_2026 Grid (15 arc-second). Không dùng thay hải đồ chính thức."
ATTR = "GEBCO Compilation Group (2026) GEBCO 2026 Grid (doi:10.5285/4f68d5c7-45eb-f999-e063-7086abc036fa)"


def build(lats, lons, grid, bbox, zmin, zmax, out):
    w, s, e, n = bbox
    pyr = Pyramid(lats, lons, grid)
    pal = palette()
    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    db.executescript("""
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);
        CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);""")
    meta = {"name": NAME, "format": "png", "type": "baselayer", "version": "1",
            "minzoom": str(zmin), "maxzoom": str(zmax), "bounds": f"{w},{s},{e},{n}",
            "center": f"{(w+e)/2},{(s+n)/2},{zmin+2}",
            "description": DESC, "attribution": ATTR}
    db.executemany("INSERT INTO metadata VALUES (?,?)", meta.items())
    total = 0; nbytes = 0
    for z in range(zmin, zmax + 1):
        x0, x1 = int(lon2tx(w, z)), int(lon2tx(e, z))
        y0, y1 = int(lat2ty(n, z)), int(lat2ty(s, z))
        cnt = 0; t0 = time.time()
        for tx in range(x0, x1 + 1):
            rows = []
            for ty in range(y0, y1 + 1):
                elev = sample(pyr, z, tx, ty)
                png = render(elev, z, pal)
                if png is None:
                    continue
                rows.append((z, tx, (1 << z) - 1 - ty, sqlite3.Binary(png)))
                nbytes += len(png)
            db.executemany("INSERT INTO tiles VALUES (?,?,?,?)", rows)
            cnt += len(rows)
        db.commit()
        total += cnt
        print(f"zoom {z}: {cnt} ô ({time.time()-t0:.0f}s), tổng {total} ô, {nbytes/1048576:.1f} MB", flush=True)
    db.execute("VACUUM")
    db.close()
    print("Đã tạo", out, os.path.getsize(out) // 1048576, "MB", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default="99,0,122.5,24", help="tây,nam,đông,bắc")
    ap.add_argument("--minzoom", type=int, default=4)
    ap.add_argument("--maxzoom", type=int, default=11)
    ap.add_argument("--out", default="dosau.mbtiles")
    ap.add_argument("--work", default="etopo_data")
    ap.add_argument("--nc", nargs="*", help="dùng tệp NetCDF có sẵn thay vì tải")
    ap.add_argument("--tif", nargs="*", help="dùng ô GeoTIFF GEBCO có sẵn (tên tệp chứa n.._s.._w.._e..)")
    ap.add_argument("--source", choices=["gebco", "etopo"], default="gebco")
    a = ap.parse_args()
    bbox = [float(v) for v in a.bbox.split(",")]
    global NAME, DESC, ATTR
    if a.source == "etopo" and not a.tif:
        NAME = "Độ sâu ETOPO 2022 – Biển Đông & Vịnh Thái Lan"
        DESC = "Độ sâu tham khảo từ ETOPO 2022 15 arc-second (NOAA NCEI). Không dùng thay hải đồ chính thức."
        ATTR = "ETOPO 2022, NOAA NCEI (doi:10.25921/fd45-gt74)"
    if a.tif:
        parts = [read_geotiff_window(p, tile_bounds(os.path.basename(p)), bbox) for p in a.tif]
    elif a.nc:
        parts = [read_any(p) for p in a.nc]
    elif a.source == "gebco":
        try:
            parts = download_gebco(bbox, a.work)
        except Exception as ex:
            print("GEBCO lỗi:", ex, "- chuyển sang ETOPO 2022", flush=True)
            NAME, DESC, ATTR = (NAME.replace("GEBCO 2026", "ETOPO 2022"),
                                "Độ sâu tham khảo từ ETOPO 2022 15 arc-second (NOAA NCEI). Không dùng thay hải đồ chính thức.",
                                "ETOPO 2022, NOAA NCEI (doi:10.25921/fd45-gt74)")
            parts = download(bbox, a.work)
    else:
        parts = download(bbox, a.work)
    lats, lons, grid = assemble(parts, bbox)
    build(lats, lons, grid, bbox, a.minzoom, a.maxzoom, a.out)


if __name__ == "__main__":
    main()
