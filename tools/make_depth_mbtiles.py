#!/usr/bin/env python3
"""
Tạo hải đồ độ sâu dạng MBTiles từ dữ liệu ETOPO 2022 (NOAA NCEI, 15 giây cung).

- Nguồn: ETOPO 2022 15 Arc-Second Global Relief Model, NOAA NCEI, doi:10.25921/fd45-gt74
  (dữ liệu công cộng của chính phủ Mỹ, phần đáy biển chủ yếu lấy từ GEBCO).
- Kết quả: vùng nước tô màu theo dải độ sâu, đường đẳng sâu và số độ sâu tham khảo;
  đất liền tô màu nền đất để dùng được khi không có mạng.

Cách dùng:
  python make_depth_mbtiles.py --bbox 99,0,122.5,24 --minzoom 4 --maxzoom 10 --out ra.mbtiles
  python make_depth_mbtiles.py --nc tep1.nc tep2.nc ...   (dùng tệp NetCDF đã có sẵn)
"""
import argparse, io, math, os, sqlite3, sys, time, urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage
from scipy.io import netcdf_file

ERDDAP = "https://coastwatch.pfeg.noaa.gov/erddap/griddap/ETOPO_2022_v1_15s.nc?z[({s}):({n})][({w}):({e})]"
THREDDS = "https://www.ngdc.noaa.gov/thredds/fileServer/global/ETOPO2022/15s/15s_surface_elev_netcdf/ETOPO_2022_v1_15s_{tile}_surface.nc"
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
    meta = {"name": "Độ sâu Biển Đông & Vịnh Thái Lan", "format": "png", "type": "baselayer", "version": "1",
            "minzoom": str(zmin), "maxzoom": str(zmax), "bounds": f"{w},{s},{e},{n}",
            "center": f"{(w+e)/2},{(s+n)/2},{zmin+2}",
            "description": "Độ sâu tham khảo từ ETOPO 2022 15 arc-second (NOAA NCEI). Không dùng thay hải đồ chính thức.",
            "attribution": "ETOPO 2022, NOAA NCEI (doi:10.25921/fd45-gt74)"}
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
    a = ap.parse_args()
    bbox = [float(v) for v in a.bbox.split(",")]
    parts = [read_any(p) for p in a.nc] if a.nc else download(bbox, a.work)
    lats, lons, grid = assemble(parts, bbox)
    build(lats, lons, grid, bbox, a.minzoom, a.maxzoom, a.out)


if __name__ == "__main__":
    main()
