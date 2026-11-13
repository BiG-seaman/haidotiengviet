# -*- coding: utf-8 -*-
"""
Hải đồ VN – bản Windows.

Dùng chung giao diện (web/index.html) với app Android. Phần này thay cho MainActivity.java:
- máy chủ nội bộ 127.0.0.1 phục vụ giao diện, ô bản đồ MBTiles và ô bản đồ trực tuyến (có lưu đệm),
- đọc NMEA (GPS tàu, AIS, la bàn, độ sâu) qua Wi-Fi (TCP/UDP) hoặc cổng COM (GPS/AIS cắm USB),
- lưu dữ liệu (điểm, tuyến, vết, cài đặt) vào %APPDATA%\\HaiDoVN,
- hộp thoại chọn/lưu tệp của Windows.
Cửa sổ dùng Microsoft Edge WebView2 (qua pywebview). Nếu không có, mở bằng Edge/trình duyệt.
"""
import hashlib, http.server, io, json, os, queue, re, socket, socketserver, sqlite3, sys, threading, time
import urllib.parse, urllib.request, webbrowser

APP_NAME = "Hải đồ VN"
UA = "HaiDoVN-PC/1.0 (Windows; ung dung hai do ca nhan)"
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(BASE, "web")


def data_root():
    root = os.environ.get("HAIDOVN_HOME") or os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "HaiDoVN")
    os.makedirs(root, exist_ok=True)
    return root


ROOT = data_root()
DATA = os.path.join(ROOT, "data"); os.makedirs(DATA, exist_ok=True)
CACHE = os.path.join(ROOT, "tilecache")
ZOOMS = os.path.join(ROOT, "zooms"); os.makedirs(ZOOMS, exist_ok=True)
CHARTS_FILE = os.path.join(ROOT, "charts.json")
LOG = os.path.join(ROOT, "haidovn.log")


def say(*a):
    """In ra màn hình nếu có (bản .exe dạng cửa sổ không có màn hình dòng lệnh)."""
    try:
        if sys.stdout:
            print(*a, flush=True)
    except Exception:
        pass


def log(*a):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a) + "\n")
    except Exception:
        pass


# ---------------- Sự kiện đẩy sang giao diện (Server-Sent Events) ----------------
class Events:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def add(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.append(q)
        return q

    def remove(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def send(self, fn, *args):
        msg = json.dumps({"fn": fn, "args": list(args)}, ensure_ascii=False)
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


EV = Events()


def toast(m):
    EV.send("onToast", m)


# ---------------- Lưu dữ liệu ----------------
def key_file(k):
    return os.path.join(DATA, re.sub(r"[^a-zA-Z0-9_]", "_", k) + ".json")


def atomic_write(path, data: bytes):
    tmp = path + ".tmp%d" % threading.get_ident()
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# ---------------- Hải đồ MBTiles (dùng tại chỗ, không chép) ----------------
class Charts:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}      # id -> {"path":..., "info":{...}}
        self.pools = {}      # id -> queue of sqlite connections
        try:
            saved = json.load(open(CHARTS_FILE, encoding="utf-8"))
        except Exception:
            saved = []
        for e in saved:
            if os.path.exists(e.get("path", "")):
                self.open(e["path"], e.get("id"), save=False)
        self.save()

    def save(self):
        with self.lock:
            lst = [{"id": k, "path": v["path"]} for k, v in self.items.items()]
        atomic_write(CHARTS_FILE, json.dumps(lst, ensure_ascii=False, indent=1).encode("utf-8"))

    def connect(self, path):
        c = sqlite3.connect(path, check_same_thread=False)
        try:
            c.execute("PRAGMA query_only=1")
            c.execute("PRAGMA mmap_size=268435456")
        except Exception:
            pass
        return c

    def conn(self, cid):
        p = self.pools.get(cid)
        if p is None:
            return None, None
        try:
            return p, p.get_nowait()
        except queue.Empty:
            it = self.items.get(cid)
            return (p, self.connect(it["path"])) if it else (None, None)

    def release(self, p, c):
        try:
            p.put_nowait(c)
        except queue.Full:
            c.close()

    def open(self, path, cid=None, save=True):
        path = os.path.abspath(path)
        with self.lock:
            for k, v in self.items.items():
                if os.path.normcase(v["path"]) == os.path.normcase(path):
                    return k
        try:
            db = self.connect(path)
            meta = {}
            try:
                for n, v in db.execute("SELECT name, value FROM metadata"):
                    if n is not None:
                        meta[n] = v
            except Exception:
                pass
            try:
                minz, maxz = int(str(meta["minzoom"]).strip()), int(str(meta["maxzoom"]).strip())
            except Exception:
                minz, maxz = db.execute("SELECT min(zoom_level), max(zoom_level) FROM tiles").fetchone()
            row = db.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
            fmt = "png"
            if row and row[0]:
                d = bytes(row[0][:4])
                fmt = "jpg" if d[:2] == b"\xff\xd8" else "webp" if d[:2] == b"RI" else "png" if d[:2] == b"\x89P" else "pbf"
            base = re.sub(r"(?i)\.mbtiles$", "", os.path.basename(path))
            if not cid:
                cid = base
                n = 2
                while cid in self.items:
                    cid = f"{base} ({n})"; n += 1
            name = (meta.get("name") or "").strip() or base
            info = {"id": cid, "name": name, "minzoom": int(minz), "maxzoom": int(maxz), "format": fmt,
                    "size": os.path.getsize(path), "path": path}
            try:
                b = [float(x) for x in str(meta.get("bounds", "")).split(",")]
                if len(b) == 4:
                    info["bounds"] = b
            except Exception:
                pass
        except Exception as e:
            log("open chart failed", path, e)
            return None
        with self.lock:
            self.items[cid] = {"path": path, "info": info}
            q = queue.Queue(maxsize=max(2, min(8, (os.cpu_count() or 4) * 3 // 4)))
            q.put(db)
            self.pools[cid] = q
        if save:
            self.save()
        threading.Thread(target=self.zooms, args=(cid, path), daemon=True).start()
        return cid

    def zooms(self, cid, path):
        """Đọc phạm vi ô của từng mức zoom (lưu lại để lần sau mở nhanh)."""
        try:
            st = os.stat(path)
            sig = f"{st.st_size}:{int(st.st_mtime)}"
            cf = os.path.join(ZOOMS, hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest() + ".json")
            zs = None
            try:
                c = json.load(open(cf, encoding="utf-8"))
                if c.get("sig") == sig:
                    zs = c["zooms"]
            except Exception:
                pass
            if zs is None:
                db = self.connect(path)
                zs = {}
                for z, a, b, c, d in db.execute("SELECT zoom_level, MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) FROM tiles GROUP BY zoom_level"):
                    if z is None or z < 0 or z > 24:
                        continue
                    n = (1 << z) - 1
                    zs[str(z)] = [a, b, n - d, n - c]
                db.close()
                atomic_write(cf, json.dumps({"sig": sig, "zooms": zs}).encode("utf-8"))
            with self.lock:
                it = self.items.get(cid)
                if it and it["path"] == path and zs:
                    it["info"]["zooms"] = zs
            EV.send("onChartsChanged")
        except Exception as e:
            log("zooms failed", path, e)

    def remove(self, cid):
        with self.lock:
            self.items.pop(cid, None)
            p = self.pools.pop(cid, None)
        if p:
            while True:
                try:
                    p.get_nowait().close()
                except Exception:
                    break
        self.save()

    def list(self):
        with self.lock:
            return [dict(v["info"]) for v in self.items.values()]

    def tile(self, cid, z, x, y):
        p, c = self.conn(cid)
        if c is None:
            return None
        try:
            r = c.execute(f"SELECT tile_data FROM tiles WHERE zoom_level={z} AND tile_column={x} AND tile_row={(1 << z) - 1 - y} LIMIT 1").fetchone()
            return bytes(r[0]) if r and r[0] else None
        except Exception:
            return None
        finally:
            self.release(p, c)


CHARTS = None

ONLINE = {"osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
          "sea": "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png"}


def online_tile(src, z, x, y):
    if src not in ONLINE:
        return None
    f = os.path.join(CACHE, src, str(z), str(x), f"{y}.png")
    have = os.path.exists(f) and os.path.getsize(f) > 0
    if have and time.time() - os.path.getmtime(f) < 30 * 86400:
        return open(f, "rb").read()
    d = None
    try:
        req = urllib.request.Request(ONLINE[src].format(z=z, x=x, y=y), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 200:
                d = r.read()
    except Exception:
        pass
    if d:
        try:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            atomic_write(f, d)
        except Exception:
            pass
        return d
    return open(f, "rb").read() if have else None


# ---------------- NMEA: Wi-Fi (TCP/UDP) hoặc cổng COM ----------------
class Nmea:
    def __init__(self):
        self.th = None
        self.stop_ev = threading.Event()
        self.buf = []
        self.blk = threading.Lock()
        self.status = "Chưa kết nối"
        threading.Thread(target=self.flusher, daemon=True).start()

    def set_status(self, s):
        self.status = s
        EV.send("onAisStatus", s)

    def flusher(self):
        while True:
            time.sleep(0.2)
            with self.blk:
                if not self.buf:
                    continue
                txt = "\n".join(self.buf); self.buf = []
            EV.send("onNmeaBatch", txt)

    def line(self, ln):
        ln = ln.strip()
        if 6 <= len(ln) <= 300 and ("!" in ln or "$" in ln):
            with self.blk:
                if len(self.buf) < 5000:
                    self.buf.append(ln)

    def connect(self, host, port, proto):
        self.disconnect()
        self.stop_ev = threading.Event()
        self.th = threading.Thread(target=self.run, args=(host, port, proto, self.stop_ev), daemon=True)
        self.th.start()

    def disconnect(self):
        self.stop_ev.set()
        self.th = None

    def run(self, host, port, proto, stop):
        while not stop.is_set():
            try:
                self.set_status("Đang kết nối…")
                if proto == "serial":
                    import serial
                    with serial.Serial(host, int(port) or 4800, timeout=1) as sp:
                        self.set_status(f"Đã mở cổng {host} ({port} baud)")
                        pending = b""
                        while not stop.is_set():
                            pending += sp.read(4096)
                            *lines, pending = pending.split(b"\n")
                            for l in lines:
                                self.line(l.decode("ascii", "ignore"))
                elif proto == "udp":
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("", int(port))); s.settimeout(1)
                    self.set_status(f"Đang nghe UDP cổng {port}")
                    with s:
                        while not stop.is_set():
                            try:
                                d, _ = s.recvfrom(8192)
                            except socket.timeout:
                                continue
                            for l in d.decode("ascii", "ignore").splitlines():
                                self.line(l)
                else:
                    with socket.create_connection((host, int(port)), timeout=8) as s:
                        s.settimeout(1)
                        self.set_status(f"Đã kết nối {host}:{port}")
                        pending = b""
                        while not stop.is_set():
                            try:
                                d = s.recv(8192)
                            except socket.timeout:
                                continue
                            if not d:
                                break
                            pending += d
                            *lines, pending = pending.split(b"\n")
                            for l in lines:
                                self.line(l.decode("ascii", "ignore"))
            except Exception as e:
                log("nmea", e)
            if stop.is_set():
                break
            self.set_status("Mất kết nối, thử lại sau 5 giây")
            stop.wait(5)
        self.set_status("Đã ngắt")


NMEA = Nmea()


def serial_ports():
    try:
        from serial.tools import list_ports
        return [{"port": p.device, "desc": p.description} for p in list_ports.comports()]
    except Exception:
        return []


# ---------------- Hộp thoại tệp ----------------
WINDOW = None


def dialog(kind, multiple=False, types=None, name=""):
    """kind: 'open' | 'save'. Trả về list đường dẫn (open) hoặc đường dẫn (save) hoặc None."""
    if WINDOW is not None:
        try:
            import webview
            FD = getattr(webview, "FileDialog", None)
            dt = (FD.OPEN if kind == "open" else FD.SAVE) if FD else (webview.OPEN_DIALOG if kind == "open" else webview.SAVE_DIALOG)
            r = WINDOW.create_file_dialog(dt, allow_multiple=multiple, file_types=tuple(types or ()), save_filename=name)
            if not r:
                return None
            if kind == "save":
                return r if isinstance(r, str) else r[0]
            return list(r)
        except Exception as e:
            log("webview dialog failed", e)
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        ft = []
        for t in types or ():
            m = re.match(r"(.*)\((.*)\)", t)
            if m:
                ft.append((m.group(1).strip(), m.group(2).replace(";", " ")))
        if kind == "open":
            r = filedialog.askopenfilenames(filetypes=ft or [("Tất cả", "*.*")]) if multiple else filedialog.askopenfilename(filetypes=ft or [("Tất cả", "*.*")])
            r = list(r) if multiple else ([r] if r else [])
        else:
            r = filedialog.asksaveasfilename(initialfile=name, defaultextension=".gpx", filetypes=ft or [("Tất cả", "*.*")])
        root.destroy()
        return r or None
    except Exception as e:
        log("tk dialog failed", e)
        return None


DIALOG_LOCK = threading.Lock()


# ---------------- Máy chủ nội bộ ----------------
INJECT = None


def page():
    global INJECT
    html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    head = ('<script>window.HD_PC=true;window.HD_SHARDS=' + json.dumps(SHARDS) + ';</script>'
            '<script src="/pc.js"></script>')
    return html.replace("<head>", "<head>\n" + head, 1).encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HaiDoVN"

    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode("utf-8") if n else ""

    def do_OPTIONS(self):
        self.send(204, extra={"Access-Control-Allow-Methods": "GET, POST", "Access-Control-Allow-Headers": "*"})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self.send(200, page(), "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            if p == "/pc.js":
                return self.send(200, open(os.path.join(WEB, "pc.js"), "rb").read(), "application/javascript; charset=utf-8", {"Cache-Control": "no-store"})
            m = re.match(r"^/(m|t)/([^/]+)/(\d+)/(-?\d+)/(-?\d+)$", p)
            if m:
                kind, ident, z, x, y = m.group(1), urllib.parse.unquote(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
                d = CHARTS.tile(ident, z, x, y) if kind == "m" else online_tile(ident, z, x, y)
                if not d:
                    return self.send(404)
                ct = "image/jpeg" if d[:2] == b"\xff\xd8" else "image/webp" if d[:2] == b"RI" else "image/png"
                return self.send(200, d, ct, {"Cache-Control": "max-age=86400"})
            if p == "/api/load":
                f = key_file(q.get("k", ""))
                return self.send(200, open(f, "rb").read()) if os.path.exists(f) else self.send(204)
            if p == "/api/charts":
                return self.send(200, json.dumps(CHARTS.list(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            if p == "/api/serial_ports":
                return self.send(200, json.dumps(serial_ports(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            if p == "/api/events":
                return self.events()
            return self.send(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            log("GET", p, e)
            try:
                self.send(500)
            except Exception:
                pass

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        qq = EV.add()
        try:
            self.wfile.write(b": ok\n\n"); self.wfile.flush()
            while True:
                try:
                    msg = qq.get(timeout=15)
                    self.wfile.write(("data: " + msg + "\n\n").encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            EV.remove(qq)
            self.close_connection = True

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        p = u.path
        try:
            b = self.body()
            if p == "/api/save":
                atomic_write(key_file(q.get("k", "")), b.encode("utf-8"))
                return self.send(204)
            if p == "/api/pick_mbtiles":
                self.send(204)
                threading.Thread(target=pick_mbtiles, daemon=True).start()
                return
            if p == "/api/delete_chart":
                CHARTS.remove(q.get("id", ""))
                return self.send(204)
            if p == "/api/import_gpx":
                with DIALOG_LOCK:
                    r = dialog("open", False, ["GPX (*.gpx)", "Tất cả (*.*)"])
                if not r:
                    return self.send(200, b"")
                return self.send(200, open(r[0], "rb").read())
            if p == "/api/export_gpx":
                with DIALOG_LOCK:
                    path = dialog("save", False, ["GPX (*.gpx)"], q.get("name", "haido.gpx"))
                if not path:
                    return self.send(200, b"")
                if not path.lower().endswith(".gpx"):
                    path += ".gpx"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(b)
                return self.send(200, ("Đã xuất tệp GPX: " + path).encode("utf-8"))
            if p == "/api/open_url":
                if b.startswith(("http://", "https://")):
                    webbrowser.open(b)
                elif b.startswith("tel:"):
                    toast("Gọi số " + b[4:] + " bằng điện thoại")
                return self.send(204)
            if p == "/api/ais_connect":
                NMEA.connect(q.get("host", ""), q.get("port", "10110"), q.get("proto", "tcp"))
                return self.send(204)
            if p == "/api/ais_disconnect":
                NMEA.disconnect()
                return self.send(204)
            return self.send(404)
        except Exception as e:
            log("POST", p, e)
            try:
                self.send(500, str(e).encode("utf-8"))
            except Exception:
                pass


def pick_mbtiles():
    with DIALOG_LOCK:
        paths = dialog("open", True, ["Hải đồ MBTiles (*.mbtiles)", "Tất cả (*.*)"])
    if not paths:
        return
    last, ok = None, 0
    for i, pth in enumerate(paths):
        cid = CHARTS.open(pth)
        if cid:
            ok += 1; last = cid
            if i < len(paths) - 1:
                EV.send("onChartsChanged")
        else:
            toast("Không phải MBTiles hợp lệ: " + os.path.basename(pth))
    if last:
        toast(f"Đã thêm {ok}/{len(paths)} tệp hải đồ")
        EV.send("onChartsChanged", last)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


SHARDS = []


def start_servers():
    """Một cổng chính + 4 cổng phụ để trình duyệt tải ô song song (giới hạn 6 kết nối/địa chỉ)."""
    for attempt in range(50):
        base = 18700 + attempt * 10
        servers = []
        try:
            for k in range(5):
                servers.append(Server(("127.0.0.1", base + k), Handler))
        except OSError:
            for s in servers:
                s.server_close()
            continue
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        SHARDS[:] = [f"http://127.0.0.1:{base + k}" for k in range(1, 5)]
        return f"http://127.0.0.1:{base}/"
    raise SystemExit("Không mở được cổng nội bộ")


# ---------------- Tự kiểm tra (dùng khi build) ----------------
def selftest(url):
    import tempfile
    out = []
    try:
        tmp = tempfile.mkdtemp()
        mb = os.path.join(tmp, "ThuNghiem.mbtiles")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        db = sqlite3.connect(mb)
        db.executescript("CREATE TABLE metadata(name,value); CREATE TABLE tiles(zoom_level INTEGER,tile_column INTEGER,tile_row INTEGER,tile_data BLOB);")
        db.executemany("INSERT INTO metadata VALUES(?,?)", [("name", "Thử"), ("minzoom", "5"), ("maxzoom", "5"), ("bounds", "100,5,110,15")])
        db.execute("INSERT INTO tiles VALUES(5,25,(1<<5)-1-14,?)", (png,))
        db.commit(); db.close()
        cid = CHARTS.open(mb)
        get = lambda pth: urllib.request.urlopen(url.rstrip("/") + pth, timeout=10)
        assert b"HD_PC" in get("/").read(), "index"
        assert b"Android" in get("/pc.js").read(), "pc.js"
        lst = json.loads(get("/api/charts").read())
        assert any(c["id"] == cid for c in lst), "charts"
        assert get(f"/m/{urllib.parse.quote(cid)}/5/25/14").read() == png, "tile"
        req = urllib.request.Request(url.rstrip("/") + "/api/save?k=tt", data=b'{"a":1}', method="POST")
        urllib.request.urlopen(req, timeout=10)
        assert get("/api/load?k=tt").read() == b'{"a":1}', "save/load"
        CHARTS.remove(cid)
        import serial  # noqa: F401  (kiểm tra đã đóng gói pyserial)
        out.append("SELFTEST OK")
        code = 0
    except Exception as e:
        out.append("SELFTEST FAIL: %r" % (e,))
        code = 1
    with open(os.path.join(ROOT, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    say("\n".join(out))
    return code


def main():
    global CHARTS, WINDOW
    CHARTS = Charts()
    url = start_servers()
    log("start", url)
    if "--selftest" in sys.argv:
        sys.exit(selftest(url))
    if "--serve" in sys.argv:          # chỉ chạy máy chủ (để thử nghiệm)
        say(url)
        while True:
            time.sleep(3600)
    try:
        import webview
        WINDOW = webview.create_window(APP_NAME, url, width=1280, height=820, min_size=(760, 560), text_select=False)
        webview.start(private_mode=False, storage_path=os.path.join(ROOT, "webview"))
    except Exception as e:
        log("webview failed", e)
        WINDOW = None
        edge = None
        for pth in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(pth):
                edge = pth
        if edge:
            import subprocess
            subprocess.Popen([edge, "--app=" + url, "--user-data-dir=" + os.path.join(ROOT, "edge")])
        else:
            webbrowser.open(url)
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
