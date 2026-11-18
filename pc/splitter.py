# -*- coding: utf-8 -*-
"""Chia một tệp MBTiles lớn thành nhiều tệp nhỏ theo vùng (mỗi tệp giữ đủ các mức zoom),
để đưa lên kho và cho khách chọn tải từng vùng.

Cách làm: đo dung lượng ô ở mức zoom lớn nhất theo từng khối, rồi cắt đôi vùng liên tục
(theo trục dài, tại điểm chia đều dung lượng) cho tới khi mỗi phần ≤ dung lượng mong muốn.
Mỗi phần chép mọi ô (mọi zoom) nằm trong vùng đó; ô zoom nhỏ ở mép được chép cho cả hai bên."""
import math
import os
import sqlite3


def _fmt_ll(lat, lon):
    return "%d%s%d%s" % (round(abs(lat)), "N" if lat >= 0 else "S", round(abs(lon)), "E" if lon >= 0 else "W")


def plan(src, target_bytes):
    """Trả về (maxz, zooms, [ (x0,x1,y0,y1, est_bytes) ... ]) theo toạ độ ô ở zoom lớn nhất (hàng TMS)."""
    db = sqlite3.connect("file:" + src.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        zooms = [r[0] for r in db.execute("SELECT DISTINCT zoom_level FROM tiles ORDER BY 1") if r[0] is not None]
        if not zooms:
            raise ValueError("tệp không có ô nào")
        maxz = zooms[-1]
        x0, x1, y0, y1 = db.execute(f"SELECT MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) FROM tiles WHERE zoom_level={maxz}").fetchone()
        k = 0
        while ((x1 >> k) - (x0 >> k) + 1) * ((y1 >> k) - (y0 >> k) + 1) > 4096:
            k += 1
        blocks = {}
        tot_max = 0
        for bx, by, sz in db.execute(f"SELECT tile_column>>{k}, tile_row>>{k}, SUM(length(tile_data)) FROM tiles WHERE zoom_level={maxz} GROUP BY 1, 2"):
            blocks[(bx, by)] = sz or 0
            tot_max += sz or 0
        tot_all = db.execute("SELECT SUM(length(tile_data)) FROM tiles").fetchone()[0] or tot_max
    finally:
        db.close()
    factor = (tot_all / tot_max) if tot_max else 1.0      # các zoom nhỏ cộng thêm vào mỗi phần

    def size(r):
        a, b, c, d = r
        return sum(v for (bx, by), v in blocks.items() if a <= bx <= b and c <= by <= d) * factor

    out = []

    def split(r):
        s = size(r)
        if s <= 0:
            return
        a, b, c, d = r
        if s <= target_bytes or (a == b and c == d):
            out.append((a << k, ((b + 1) << k) - 1, c << k, ((d + 1) << k) - 1, int(s)))
            return
        # cắt theo trục dài hơn, tại chỗ chia đôi dung lượng
        if (b - a) >= (d - c) and b > a:
            acc, half = 0, s / 2
            for m in range(a, b):
                acc += size((m, m, c, d))
                if acc >= half:
                    break
            split((a, m, c, d)); split((m + 1, b, c, d))
        else:
            acc, half = 0, s / 2
            for m in range(c, d):
                acc += size((a, b, m, m))
                if acc >= half:
                    break
            split((a, b, c, m)); split((a, b, m + 1, d))

    split(((x0 >> k), (x1 >> k), (y0 >> k), (y1 >> k)))
    return maxz, zooms, out


def split_mbtiles(src, out_dir, target_bytes, progress=None, cancel=lambda: False):
    maxz, zooms, parts = plan(src, target_bytes * 0.85)   # chừa phần phụ của tệp SQLite
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    meta = {}
    try:
        m = sqlite3.connect("file:" + src.replace("\\", "/") + "?mode=ro", uri=True)
        meta = {str(k): v for k, v in m.execute("SELECT name, value FROM metadata")}
        m.close()
    except Exception:
        pass
    base_name = meta.get("name") or stem
    if base_name.lower() in ("unnamed map", ""):
        base_name = stem
    outputs = []
    total = len(parts) * len(zooms)
    step = 0
    for i, (x0, x1, y0, y1, est) in enumerate(parts, 1):
        n = 1 << maxz
        lon_w, lon_e = x0 / n * 360 - 180, (x1 + 1) / n * 360 - 180
        # hàng TMS: y0 là phía nam (cạnh dưới ô y0), y1 phía bắc (cạnh trên ô y1)
        lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ((n - 1 - y0) + 1) / n))))
        lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (n - 1 - y1) / n))))
        fn = os.path.join(out_dir, "%s_%02d_%s.mbtiles" % (stem, i, _fmt_ll((lat_s + lat_n) / 2, (lon_w + lon_e) / 2)))
        if os.path.exists(fn):
            os.remove(fn)
        o = sqlite3.connect("file:" + fn.replace("\\", "/"), uri=True)
        o.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                        "CREATE TABLE metadata (name TEXT, value TEXT);"
                        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB);")
        o.execute("ATTACH DATABASE ? AS s", ("file:" + src.replace("\\", "/") + "?mode=ro",))
        md = dict(meta)
        md.update({"name": "%s – phần %d" % (base_name, i), "bounds": "%.6f,%.6f,%.6f,%.6f" % (lon_w, lat_s, lon_e, lat_n),
                   "minzoom": str(zooms[0]), "maxzoom": str(maxz), "type": md.get("type", "overlay")})
        md.setdefault("format", "png")
        o.executemany("INSERT INTO metadata VALUES (?, ?)", list(md.items()))
        for z in zooms:
            if cancel():
                o.close(); os.remove(fn)
                raise InterruptedError("đã dừng")
            sh = maxz - z
            o.execute(f"INSERT INTO tiles SELECT zoom_level, tile_column, tile_row, tile_data FROM s.tiles "
                      f"WHERE zoom_level={z} AND tile_column BETWEEN {x0 >> sh} AND {x1 >> sh} AND tile_row BETWEEN {y0 >> sh} AND {y1 >> sh}")
            o.commit()
            step += 1
            if progress:
                progress(step, total, "Phần %d/%d – zoom %d" % (i, len(parts), z))
        o.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
        o.commit()
        o.execute("DETACH DATABASE s")
        o.close()
        outputs.append((fn, os.path.getsize(fn)))
    return outputs
