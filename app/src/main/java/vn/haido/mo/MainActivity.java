package vn.haido.mo;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.location.Location;
import android.location.LocationListener;
import android.location.LocationManager;
import android.media.AudioManager;
import android.media.ToneGenerator;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Vibrator;
import android.provider.OpenableColumns;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class MainActivity extends Activity implements LocationListener {

    static final String HOST = "haido.local";
    static final String UA = "HaiDoMo/1.0 (Android; ung dung hai do ca nhan)";
    static final int REQ_LOC = 1, REQ_MBT = 10, REQ_GPX_IN = 11, REQ_GPX_OUT = 12;

    WebView web;
    LocationManager lm;
    boolean gpsOn = false;
    final Handler ui = new Handler(Looper.getMainLooper());
    final Map<String, SQLiteDatabase> dbs = new ConcurrentHashMap<>();
    final Map<String, JSONObject> infos = new ConcurrentHashMap<>();
    // nhiều kết nối đọc cho mỗi tệp MBTiles để đọc ô song song trên nhiều nhân CPU (~75% số nhân)
    final Map<String, SQLiteDatabase[]> pools = new ConcurrentHashMap<>();
    final Map<String, String> paths = new ConcurrentHashMap<>();
    // hải đồ BSB/KAP (cắt ô theo yêu cầu) và SQLiteDB (RMaps/OsmAnd: {đảo zoom, đảo y})
    final Map<String, KapChart> kaps = new ConcurrentHashMap<>();
    final Map<String, boolean[]> sqm = new ConcurrentHashMap<>();
    static final String EXT_RE = "(?i)\\.(mbtiles|kap|sqlitedb)$";
    final java.util.concurrent.atomic.AtomicInteger rr = new java.util.concurrent.atomic.AtomicInteger();
    final int POOL = Math.max(2, Math.min(8, Runtime.getRuntime().availableProcessors() * 3 / 4));
    String pendingGpx = null;

    // báo động
    ToneGenerator tone;
    boolean alarmOn = false;
    final Runnable alarmTick = new Runnable() {
        @Override public void run() {
            if (!alarmOn) return;
            try {
                if (tone == null) tone = new ToneGenerator(AudioManager.STREAM_ALARM, 100);
                tone.startTone(ToneGenerator.TONE_CDMA_EMERGENCY_RINGBACK, 800);
                Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
                if (v != null) v.vibrate(500);
            } catch (Exception ignored) {}
            ui.postDelayed(this, 1200);
        }
    };

    // AIS
    volatile Thread aisThread;
    volatile Socket aisSock;
    volatile DatagramSocket aisUdp;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        loadCharts();
        installDefaultChart();

        web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setSupportZoom(false);
        s.setTextZoom(100);
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                String host = u.getHost();
                if (host == null || !(host.equals(HOST) || host.endsWith("." + HOST))) return null;
                try {
                    WebResourceResponse res = serve(u.getPath());
                    return res != null ? res : notFound();
                } catch (Exception e) {
                    return notFound();
                }
            }
        });
        web.addJavascriptInterface(new Bridge(), "Android");
        web.loadUrl("https://" + HOST + "/index.html");

        lm = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) startGps();
        else requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOC);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] res) {
        super.onRequestPermissionsResult(code, perms, res);
        if (code == REQ_LOC) {
            if (checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) startGps();
            else toast("Chưa cấp quyền vị trí – không hiển thị được vị trí tàu");
        }
    }

    android.location.GnssStatus.Callback gnssCb;
    long lastSatMs = 0;

    void startGps() {
        if (gpsOn || lm == null) return;
        try {
            // đăng ký cả khi GPS đang tắt: người dùng bật lên là nhận vị trí ngay, không phải mở lại app
            lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000, 0, this, Looper.getMainLooper());
            gpsOn = true;
            if (!lm.isProviderEnabled(LocationManager.GPS_PROVIDER)) toast("Hãy bật GPS (Vị trí) trong cài đặt điện thoại");
            // số vệ tinh đang dùng / thấy được (hiển thị cạnh toạ độ)
            gnssCb = new android.location.GnssStatus.Callback() {
                @Override public void onSatelliteStatusChanged(android.location.GnssStatus st) {
                    long now = System.currentTimeMillis();
                    if (now - lastSatMs < 2000) return;
                    lastSatMs = now;
                    int used = 0, n = st.getSatelliteCount();
                    for (int i = 0; i < n; i++) if (st.usedInFix(i)) used++;
                    js("window.onSats&&onSats(" + used + "," + n + ")");
                }
            };
            try { lm.registerGnssStatusCallback(gnssCb, ui); } catch (Exception ignored) {}
            // vị trí cũ chỉ dùng nếu còn mới (dưới 2 phút) để không hiện sai chỗ lúc mới mở
            Location last = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER);
            if (last != null && System.currentTimeMillis() - last.getTime() < 120000) onLocationChanged(last);
        } catch (SecurityException ignored) {}
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (!gpsOn && checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) startGps();
    }

    @Override
    protected void onDestroy() {
        try { if (lm != null) lm.removeUpdates(this); } catch (Exception ignored) {}
        try { if (lm != null && gnssCb != null) lm.unregisterGnssStatusCallback(gnssCb); } catch (Exception ignored) {}
        alarmOn = false;
        ui.removeCallbacks(alarmTick);
        if (tone != null) tone.release();
        stopAis();
        for (String id : new java.util.ArrayList<>(dbs.keySet())) closePool(id);
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        web.evaluateJavascript("window.onBack?window.onBack():false", v -> { if (!"true".equals(v)) finish(); });
    }

    // ---------- GPS ----------
    @Override
    public void onLocationChanged(Location l) {
        js("window.onGps&&onGps(" + l.getLatitude() + "," + l.getLongitude() + ","
                + (l.hasSpeed() ? l.getSpeed() : -1) + "," + (l.hasBearing() ? l.getBearing() : -1) + ","
                + (l.hasAccuracy() ? l.getAccuracy() : -1) + "," + l.getTime() + ")");
    }
    @Override public void onStatusChanged(String p, int s, Bundle e) {}
    @Override public void onProviderEnabled(String p) { if (LocationManager.GPS_PROVIDER.equals(p)) toast("Đã bật GPS, đang dò vệ tinh…"); }
    @Override public void onProviderDisabled(String p) {}

    // ---------- tiện ích ----------
    void js(final String code) { ui.post(() -> { if (web != null) web.evaluateJavascript(code, null); }); }
    void toast(String m) { js("window.onToast&&onToast(" + JSONObject.quote(m) + ")"); }

    static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) o.write(buf, 0, n);
        in.close();
        return o.toByteArray();
    }

    WebResourceResponse notFound() {
        return new WebResourceResponse("text/plain", "utf-8", 404, "Not Found", new HashMap<String, String>(), new ByteArrayInputStream(new byte[0]));
    }

    WebResourceResponse image(byte[] d) {
        String mime = "image/png";
        if (d.length > 3) {
            if ((d[0] & 0xff) == 0xff && (d[1] & 0xff) == 0xd8) mime = "image/jpeg";
            else if (d[0] == 'R' && d[1] == 'I' && d[2] == 'F') mime = "image/webp";
        }
        Map<String, String> h = new HashMap<>();
        h.put("Access-Control-Allow-Origin", "*");
        h.put("Cache-Control", "max-age=86400");
        return new WebResourceResponse(mime, null, 200, "OK", h, new ByteArrayInputStream(d));
    }

    // ---------- phục vụ trang và ô bản đồ ----------
    WebResourceResponse serve(String path) throws Exception {
        if (path == null || path.equals("/") || path.equals("/index.html"))
            return new WebResourceResponse("text/html", "utf-8", getAssets().open("index.html"));
        String[] p = path.split("/");
        if (p.length != 6) return null;
        int z = Integer.parseInt(p[3]), x = Integer.parseInt(p[4]), y = Integer.parseInt(p[5]);
        if (z < 0 || z > 22) return null;
        byte[] d = null;
        if ("m".equals(p[1])) d = chartTile(p[2], z, x, y);
        else if ("t".equals(p[1])) d = onlineTile(p[2], z, x, y);
        return d == null ? null : image(d);
    }

    static SQLiteDatabase openDb(String path) {
        SQLiteDatabase d = SQLiteDatabase.openDatabase(path, null, SQLiteDatabase.OPEN_READONLY | SQLiteDatabase.NO_LOCALIZED_COLLATORS);
        try { Cursor pc = d.rawQuery("PRAGMA mmap_size=268435456", null); pc.moveToFirst(); pc.close(); } catch (Exception ignored) {}
        return d;
    }

    SQLiteDatabase conn(String id) {
        SQLiteDatabase[] p = pools.get(id);
        if (p == null) return dbs.get(id);
        int i = Math.floorMod(rr.getAndIncrement(), p.length);
        SQLiteDatabase d = p[i];
        if (d == null) {
            synchronized (p) {
                if (p[i] == null) {
                    try { p[i] = openDb(paths.get(id)); } catch (Exception e) { return dbs.get(id); }
                }
                d = p[i];
            }
        }
        return d;
    }

    void closePool(String id) {
        kaps.remove(id);
        sqm.remove(id);
        SQLiteDatabase[] p = pools.remove(id);
        paths.remove(id);
        SQLiteDatabase main = dbs.remove(id);
        if (p != null) for (SQLiteDatabase d : p) if (d != null && d != main) try { d.close(); } catch (Exception ignored) {}
        if (main != null) try { main.close(); } catch (Exception ignored) {}
    }

    byte[] chartTile(String id, int z, int x, int y) {
        KapChart k = kaps.get(id);
        if (k != null) {
            try { return k.tile(z, x, y); } catch (Throwable e) { return null; }
        }
        SQLiteDatabase db = conn(id);
        if (db == null) return null;
        int tmsY = (1 << z) - 1 - y;
        boolean[] sq = sqm.get(id);
        // số nguyên ghép thẳng vào câu lệnh: so sánh đúng cả khi "tiles" là VIEW (không có kiểu cột)
        try {
            String q = sq != null
                    ? "SELECT image FROM tiles WHERE x=" + x + " AND y=" + (sq[1] ? tmsY : y) + " AND z=" + (sq[0] ? 17 - z : z) + " LIMIT 1"
                    : "SELECT tile_data FROM tiles WHERE zoom_level=" + z + " AND tile_column=" + x + " AND tile_row=" + tmsY + " LIMIT 1";
            Cursor c = db.rawQuery(q, null);
            try { return c.moveToFirst() ? c.getBlob(0) : null; } finally { c.close(); }
        } catch (Exception e) {
            return null;   // tệp vừa bị xoá hoặc thay
        }
    }

    byte[] onlineTile(String src, int z, int x, int y) throws Exception {
        String url;
        if ("osm".equals(src)) url = "https://tile.openstreetmap.org/" + z + "/" + x + "/" + y + ".png";
        else if ("sea".equals(src)) url = "https://tiles.openseamap.org/seamark/" + z + "/" + x + "/" + y + ".png";
        else return null;
        File f = new File(getFilesDir(), "tilecache/" + src + "/" + z + "/" + x + "/" + y + ".png");
        boolean have = f.exists() && f.length() > 0;
        // ô đã lưu dưới 30 ngày: dùng luôn; cũ hơn: thử tải lại, lỗi thì vẫn dùng bản cũ
        if (have && System.currentTimeMillis() - f.lastModified() < 30L * 86400000L) return readAll(new FileInputStream(f));
        byte[] d = null;
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(7000);
            c.setReadTimeout(10000);
            c.setRequestProperty("User-Agent", UA);
            if (c.getResponseCode() == 200) d = readAll(c.getInputStream());
            c.disconnect();
        } catch (Exception ignored) {}
        if (d != null && d.length > 0) {
            try {
                f.getParentFile().mkdirs();
                File tmp = new File(f.getPath() + ".tmp" + Thread.currentThread().getId());
                FileOutputStream o = new FileOutputStream(tmp);
                o.write(d);
                o.close();
                tmp.renameTo(f);
            } catch (Exception ignored) {}
            return d;
        }
        return have ? readAll(new FileInputStream(f)) : null;
    }

    // ---------- MBTiles ----------
    File chartDir() {
        File d = getExternalFilesDir("charts");
        if (d == null) d = new File(getFilesDir(), "charts");
        d.mkdirs();
        return d;
    }

    // Hải đồ nền mặc định đóng gói sẵn trong bản cài (assets/default.mbtiles): chép ra và bật một lần sau khi cài
    void installDefaultChart() {
        final File flag = new File(getFilesDir(), "default_chart_installed");
        if (flag.exists()) return;
        String[] names;
        try { names = getAssets().list(""); } catch (Exception e) { return; }
        boolean has = false;
        if (names != null) for (String n : names) if ("default.mbtiles".equals(n)) has = true;
        if (!has) return;
        new Thread(() -> {
            String id = "Hải đồ nền";
            File out = new File(chartDir(), id + ".mbtiles");
            File tmp = new File(chartDir(), id + ".mbtiles.part");
            try {
                toast("Đang cài hải đồ nền mặc định…");
                long size = -1;
                try { size = getAssets().openFd("default.mbtiles").getLength(); } catch (Exception ignored) {}
                InputStream in = getAssets().open("default.mbtiles");
                OutputStream o = new FileOutputStream(tmp);
                byte[] buf = new byte[1 << 20];
                long done = 0;
                int n, lastPct = -1;
                while ((n = in.read(buf)) > 0) {
                    o.write(buf, 0, n);
                    done += n;
                    if (size > 0) {
                        int pct = (int) (done * 100 / size);
                        if (pct != lastPct && pct % 5 == 0) { lastPct = pct; js("window.onImportProgress&&onImportProgress(" + pct + "," + JSONObject.quote("hải đồ nền") + ")"); }
                    }
                }
                o.close();
                in.close();
                if (out.exists()) out.delete();
                if (!tmp.renameTo(out)) throw new Exception("rename");
                if (openChart(out)) {
                    flag.createNewFile();
                    new File(getFilesDir(), "default_chart_shown2").createNewFile();
                    toast("Đã cài hải đồ nền mặc định");
                    js("window.onChartsChanged&&onChartsChanged(" + JSONObject.quote(id) + ")");
                }
            } catch (Exception e) {
                tmp.delete();
                toast("Không cài được hải đồ nền (kiểm tra dung lượng trống)");
            }
        }).start();
    }

    void loadCharts() {
        File[] fs = chartDir().listFiles();
        if (fs == null) return;
        for (File f : fs) if (f.getName().matches("(?i).*\\.(mbtiles|kap|sqlitedb)$")) openChart(f);
    }

    // Nhận dạng theo nội dung: "mbtiles" / "sqlitedb" / "kap" / null
    static String chartKind(File f) {
        byte[] h = new byte[16];
        try (FileInputStream in = new FileInputStream(f)) {
            if (in.read(h) < 16) return null;
        } catch (Exception e) {
            return null;
        }
        if (new String(h, 0, 15, StandardCharsets.ISO_8859_1).equals("SQLite format 3")) {
            SQLiteDatabase db = null;
            try {
                db = SQLiteDatabase.openDatabase(f.getPath(), null, SQLiteDatabase.OPEN_READONLY | SQLiteDatabase.NO_LOCALIZED_COLLATORS);
                java.util.Set<String> cols = new java.util.HashSet<>();
                Cursor c = db.rawQuery("PRAGMA table_info(tiles)", null);
                while (c.moveToNext()) cols.add(c.getString(1).toLowerCase());
                c.close();
                if (cols.contains("tile_data")) return "mbtiles";
                if (cols.contains("image") && cols.contains("z")) return "sqlitedb";
            } catch (Exception ignored) {
            } finally {
                if (db != null) try { db.close(); } catch (Exception ignored) {}
            }
            return null;
        }
        return KapChart.isKap(f) ? "kap" : null;
    }

    boolean openKap(File f) {
        try {
            final KapChart k = new KapChart(f, new File(getCacheDir(), "kap"));
            String id = f.getName().replaceAll(EXT_RE, "");
            JSONObject o = new JSONObject();
            o.put("id", id);
            o.put("name", k.name.isEmpty() ? id : k.name);
            o.put("minzoom", k.minzoom);
            o.put("maxzoom", k.maxzoom);
            o.put("format", "png");
            o.put("type", "kap");
            o.put("size", f.length());
            JSONArray a = new JSONArray();
            for (double v : k.bounds) a.put(v);
            o.put("bounds", a);
            kaps.put(id, k);
            paths.put(id, f.getPath());
            infos.put(id, o);
            new Thread(() -> {     // giải mã ảnh một lần (lưu đệm), để các ô đầu tiên hiện nhanh
                try { k.image(); js("window.onChartsChanged&&onChartsChanged()"); } catch (Throwable ignored) {}
            }).start();
            return true;
        } catch (Throwable e) {
            return false;
        }
    }

    boolean openSqlitedb(File f) {
        SQLiteDatabase db = null;
        try {
            db = SQLiteDatabase.openDatabase(f.getPath(), null, SQLiteDatabase.OPEN_READONLY | SQLiteDatabase.NO_LOCALIZED_COLLATORS);
            boolean invZ = true, invY = false;
            try {
                Cursor c = db.rawQuery("SELECT * FROM info LIMIT 1", null);
                if (c.moveToFirst()) {
                    int i = c.getColumnIndex("tilenumbering");
                    if (i >= 0) invZ = "bigplanet".equalsIgnoreCase(c.getString(i) == null ? "" : c.getString(i));
                    i = c.getColumnIndex("inverted_y");
                    if (i >= 0) { String v = c.getString(i); invY = v != null && !v.isEmpty() && !"0".equals(v); }
                }
                c.close();
            } catch (Exception ignored) {}
            JSONObject zs = new JSONObject();
            int minz = 99, maxz = -1;
            Cursor c = db.rawQuery("SELECT z, MIN(x), MAX(x), MIN(y), MAX(y) FROM tiles GROUP BY z", null);
            while (c.moveToNext()) {
                int z = invZ ? 17 - c.getInt(0) : c.getInt(0);
                if (z < 0 || z > 24) continue;
                int y0 = c.getInt(3), y1 = c.getInt(4);
                if (invY) { int n = (1 << z) - 1, t = n - y1; y1 = n - y0; y0 = t; }
                JSONArray a = new JSONArray();
                a.put(c.getInt(1)); a.put(c.getInt(2)); a.put(y0); a.put(y1);
                zs.put(String.valueOf(z), a);
                minz = Math.min(minz, z); maxz = Math.max(maxz, z);
            }
            c.close();
            if (maxz < 0) throw new Exception("khong co o");
            String fmt = "png";
            c = db.rawQuery("SELECT image FROM tiles LIMIT 1", null);
            if (c.moveToFirst()) {
                byte[] d = c.getBlob(0);
                if (d != null && d.length > 2) {
                    if ((d[0] & 0xff) == 0xff && (d[1] & 0xff) == 0xd8) fmt = "jpg";
                    else if (d[0] == 'R' && d[1] == 'I') fmt = "webp";
                }
            }
            c.close();
            int bz = minz;
            for (int z = minz; z <= Math.min(maxz, 10); z++) if (zs.has(String.valueOf(z))) bz = z;
            JSONArray r = zs.getJSONArray(String.valueOf(bz));
            int n2 = 1 << bz;
            double x0 = r.getInt(0), x1 = r.getInt(1) + 1, y0 = r.getInt(2), y1 = r.getInt(3) + 1;
            JSONArray b = new JSONArray();
            b.put(x0 / n2 * 360 - 180);
            b.put(Math.toDegrees(Math.atan(Math.sinh(Math.PI * (1 - 2 * y1 / n2)))));
            b.put(x1 / n2 * 360 - 180);
            b.put(Math.toDegrees(Math.atan(Math.sinh(Math.PI * (1 - 2 * y0 / n2)))));
            String id = f.getName().replaceAll(EXT_RE, "");
            JSONObject o = new JSONObject();
            o.put("id", id);
            o.put("name", id);
            o.put("minzoom", minz);
            o.put("maxzoom", maxz);
            o.put("format", fmt);
            o.put("type", "sqlitedb");
            o.put("size", f.length());
            o.put("bounds", b);
            o.put("zooms", zs);
            sqm.put(id, new boolean[]{invZ, invY});
            dbs.put(id, db);
            infos.put(id, o);
            SQLiteDatabase[] pool = new SQLiteDatabase[POOL];
            pool[0] = db;
            paths.put(id, f.getPath());
            pools.put(id, pool);
            return true;
        } catch (Exception e) {
            if (db != null) try { db.close(); } catch (Exception ignored) {}
            return false;
        }
    }

    boolean openChart(File f) {
        String kind = chartKind(f);
        if ("kap".equals(kind)) return openKap(f);
        if ("sqlitedb".equals(kind)) return openSqlitedb(f);
        if (kind == null) return false;
        SQLiteDatabase db = null;
        try {
            db = SQLiteDatabase.openDatabase(f.getPath(), null, SQLiteDatabase.OPEN_READONLY | SQLiteDatabase.NO_LOCALIZED_COLLATORS);
            Map<String, String> meta = new HashMap<>();
            try {
                Cursor c = db.rawQuery("SELECT name, value FROM metadata", null);
                while (c.moveToNext()) if (c.getString(0) != null) meta.put(c.getString(0), c.getString(1));
                c.close();
            } catch (Exception ignored) {}
            int minz, maxz;
            try {
                minz = Integer.parseInt(meta.get("minzoom").trim());
                maxz = Integer.parseInt(meta.get("maxzoom").trim());
            } catch (Exception e) {
                Cursor c = db.rawQuery("SELECT min(zoom_level), max(zoom_level) FROM tiles", null);
                c.moveToFirst();
                minz = c.getInt(0);
                maxz = c.getInt(1);
                c.close();
            }
            String fmt = "png";
            Cursor c = db.rawQuery("SELECT tile_data FROM tiles LIMIT 1", null);
            if (c.moveToFirst()) {
                byte[] d = c.getBlob(0);
                if (d != null && d.length > 2) {
                    if ((d[0] & 0xff) == 0xff && (d[1] & 0xff) == 0xd8) fmt = "jpg";
                    else if (d[0] == 'R' && d[1] == 'I') fmt = "webp";
                    else if ((d[0] & 0xff) == 0x89 && d[1] == 'P') fmt = "png";
                    else fmt = "pbf";
                }
            }
            c.close();
            String id = f.getName().replaceAll(EXT_RE, "");
            JSONObject o = new JSONObject();
            o.put("id", id);
            String name = meta.get("name");
            String nm = name == null ? "" : name.trim();
            if (nm.isEmpty() || nm.equalsIgnoreCase("unnamed map") || nm.equalsIgnoreCase("untitled") || nm.equalsIgnoreCase("map")) nm = id;   // SAS Planet hay ghi "Unnamed map"
            if ("Hải đồ nền".equals(id)) nm = "Hải đồ nền";
            o.put("name", nm);
            o.put("minzoom", minz);
            o.put("maxzoom", maxz);
            o.put("format", fmt);
            o.put("size", f.length());
            String bs = meta.get("bounds");
            if (bs != null) {
                try {
                    String[] q = bs.split(",");
                    JSONArray a = new JSONArray();
                    for (int i = 0; i < 4; i++) a.put(Double.parseDouble(q[i].trim()));
                    o.put("bounds", a);
                } catch (Exception ignored) {}
            }
            try { Cursor pc = db.rawQuery("PRAGMA mmap_size=268435456", null); pc.moveToFirst(); pc.close(); } catch (Exception ignored) {}
            // Vùng phủ tính từ dữ liệu thật (SAS Planet và một số công cụ ghi sai vùng phủ trong metadata):
            // dùng mức zoom lớn nhất có dữ liệu trong khoảng <= 10
            try {
                for (int z = Math.min(maxz, 10); z >= minz; z--) {
                    Cursor bc = db.rawQuery("SELECT MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) FROM tiles WHERE zoom_level=" + z, null);
                    boolean found = false;
                    if (bc.moveToFirst() && !bc.isNull(0)) {
                        int n2 = 1 << z;
                        double x0 = bc.getInt(0), x1 = bc.getInt(1) + 1;
                        double y0 = n2 - 1 - bc.getInt(3), y1 = n2 - bc.getInt(2);
                        JSONArray a = new JSONArray();
                        a.put(x0 / n2 * 360 - 180);
                        a.put(Math.toDegrees(Math.atan(Math.sinh(Math.PI * (1 - 2 * y1 / n2)))));
                        a.put(x1 / n2 * 360 - 180);
                        a.put(Math.toDegrees(Math.atan(Math.sinh(Math.PI * (1 - 2 * y0 / n2)))));
                        o.put("bounds", a);
                        found = true;
                    }
                    bc.close();
                    if (found) break;
                }
            } catch (Exception ignored) {}
            dbs.put(id, db);
            infos.put(id, o);
            SQLiteDatabase[] pool = new SQLiteDatabase[POOL];
            pool[0] = db;
            paths.put(id, f.getPath());
            pools.put(id, pool);
            computeZooms(f, id, db);
            return true;
        } catch (Exception e) {
            if (db != null) try { db.close(); } catch (Exception ignored) {}
            return false;
        }
    }

    // Đọc phạm vi ô của từng mức zoom (lưu kèm tệp .zooms.json để lần sau mở nhanh)
    void computeZooms(final File f, final String id, final SQLiteDatabase db) {
        new Thread(() -> {
            try {
                File side = new File(f.getPath() + ".zooms.json");
                String sig = f.length() + ":" + f.lastModified();
                JSONObject zs = null;
                if (side.exists()) {
                    JSONObject c = new JSONObject(new String(readAll(new FileInputStream(side)), StandardCharsets.UTF_8));
                    if (sig.equals(c.optString("sig"))) zs = c.getJSONObject("zooms");
                }
                if (zs == null) {
                    zs = new JSONObject();
                    Cursor c = db.rawQuery("SELECT zoom_level, MIN(tile_column), MAX(tile_column), MIN(tile_row), MAX(tile_row) FROM tiles GROUP BY zoom_level", null);
                    while (c.moveToNext()) {
                        int z = c.getInt(0);
                        if (z < 0 || z > 24) continue;
                        int n = (1 << z) - 1;
                        JSONArray a = new JSONArray();
                        a.put(c.getInt(1)); a.put(c.getInt(2)); a.put(n - c.getInt(4)); a.put(n - c.getInt(3));
                        zs.put(String.valueOf(z), a);
                    }
                    c.close();
                    JSONObject out = new JSONObject();
                    out.put("sig", sig);
                    out.put("zooms", zs);
                    FileOutputStream o = new FileOutputStream(side);
                    o.write(out.toString().getBytes(StandardCharsets.UTF_8));
                    o.close();
                }
                JSONObject info = infos.get(id);
                if (info != null && dbs.get(id) == db && zs.length() > 0) {
                    synchronized (info) { info.put("zooms", zs); }
                    js("window.onChartsChanged&&onChartsChanged()");
                }
            } catch (Exception ignored) {}
        }).start();
    }

    // Chèn một hoặc nhiều tệp hải đồ (MBTiles / KAP / SQLiteDB), lần lượt từng tệp trên một luồng nền
    void importMbtilesMany(final java.util.List<Uri> uris) {
        new Thread(() -> {
            int ok = 0, total = uris.size();
            String lastId = null;
            for (int k = 0; k < total; k++) {
                String id = importOne(uris.get(k), k + 1, total);
                if (id != null) {
                    ok++;
                    lastId = id;
                    if (k < total - 1) js("window.onChartsChanged&&onChartsChanged()");
                }
            }
            if (total > 1) toast("Đã chèn " + ok + "/" + total + " hải đồ");
            if (lastId != null && ok > 0) js("window.onChartsChanged&&onChartsChanged(" + JSONObject.quote(lastId) + ")");
        }).start();
    }

    String importOne(final Uri uri, int idx, int total) {
        String name = "haido.mbtiles";
        long size = -1;
        try {
            Cursor c = getContentResolver().query(uri, null, null, null, null);
            if (c != null) {
                if (c.moveToFirst()) {
                    int i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                    if (i >= 0 && c.getString(i) != null) name = c.getString(i);
                    i = c.getColumnIndex(OpenableColumns.SIZE);
                    if (i >= 0 && !c.isNull(i)) size = c.getLong(i);
                }
                c.close();
            }
        } catch (Exception ignored) {}
        name = name.replaceAll("[^\\p{L}\\p{N}._ -]", "_").replaceAll("['\"]", "_");
        String ext = ".mbtiles";
        java.util.regex.Matcher em = java.util.regex.Pattern.compile(EXT_RE).matcher(name);
        if (em.find()) { ext = "." + em.group(1).toLowerCase(); name = name.substring(0, em.start()); }
        name = name + ext;
        String id = name.replaceAll(EXT_RE, "");
        String label = (total > 1 ? "(" + idx + "/" + total + ") " : "") + name;
        File out = new File(chartDir(), name);
        File tmp = new File(chartDir(), name + ".part");
        try {
            infos.remove(id);
            closePool(id);
            InputStream in = getContentResolver().openInputStream(uri);
            OutputStream o = new FileOutputStream(tmp);
            byte[] buf = new byte[1 << 20];
            long done = 0;
            int n, lastPct = -1;
            while ((n = in.read(buf)) > 0) {
                o.write(buf, 0, n);
                done += n;
                if (size > 0) {
                    int pct = (int) (done * 100 / size);
                    if (pct != lastPct) { lastPct = pct; js("window.onImportProgress&&onImportProgress(" + pct + "," + JSONObject.quote(label) + ")"); }
                }
            }
            o.close();
            in.close();
            // nhận dạng theo nội dung: đặt đúng đuôi tệp dù tên tệp gốc không có
            String kind = chartKind(tmp);
            if (kind == null) {
                tmp.delete();
                toast("Không đọc được hải đồ (cần MBTiles, KAP hoặc SQLiteDB): " + name);
                return null;
            }
            if (!("." + kind).equals(ext)) {
                File o2 = new File(chartDir(), id + "." + kind);
                tmp.renameTo(new File(o2.getPath() + ".part"));
                tmp = new File(o2.getPath() + ".part");
                out = o2;
            }
            for (String e2 : new String[]{".mbtiles", ".kap", ".sqlitedb"}) new File(chartDir(), id + e2).delete();
            new File(out.getPath() + ".zooms.json").delete();
            if (!tmp.renameTo(out)) throw new Exception("rename");
            if (openChart(out)) {
                toast("Đã chèn hải đồ: " + label);
                return id;
            }
            out.delete();
            toast("Không đọc được hải đồ: " + name);
        } catch (Exception e) {
            tmp.delete();
            toast("Không chèn được " + name + " (kiểm tra dung lượng trống)");
        }
        return null;
    }

    // ---------- chọn / lưu tệp ----------
    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (res != RESULT_OK || data == null) return;
        if (req == REQ_MBT) {
            java.util.List<Uri> list = new java.util.ArrayList<>();
            android.content.ClipData cd = data.getClipData();
            if (cd != null) for (int k = 0; k < cd.getItemCount(); k++) { Uri u = cd.getItemAt(k).getUri(); if (u != null) list.add(u); }
            else if (data.getData() != null) list.add(data.getData());
            if (!list.isEmpty()) importMbtilesMany(list);
            return;
        }
        if (data.getData() == null) return;
        final Uri uri = data.getData();
        if (req == REQ_GPX_IN) {
            new Thread(() -> {
                try {
                    byte[] d = readAll(getContentResolver().openInputStream(uri));
                    js("window.onGpxImported&&onGpxImported(" + JSONObject.quote(new String(d, StandardCharsets.UTF_8)) + ")");
                } catch (Exception e) { toast("Không đọc được tệp"); }
            }).start();
        } else if (req == REQ_GPX_OUT && pendingGpx != null) {
            try {
                OutputStream o = getContentResolver().openOutputStream(uri);
                o.write(pendingGpx.getBytes(StandardCharsets.UTF_8));
                o.close();
                toast("Đã xuất tệp GPX");
            } catch (Exception e) { toast("Không ghi được tệp"); }
            pendingGpx = null;
        }
    }

    void pick(int req) {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        if (req == REQ_MBT) i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);   // chọn nhiều tệp cùng lúc
        try { startActivityForResult(i, req); } catch (Exception e) { toast("Không mở được trình chọn tệp"); }
    }

    // ---------- AIS ----------
    void stopAis() {
        Thread t = aisThread;
        aisThread = null;
        if (t != null) t.interrupt();
        try { if (aisSock != null) aisSock.close(); } catch (Exception ignored) {}
        try { if (aisUdp != null) aisUdp.close(); } catch (Exception ignored) {}
        aisSock = null;
        aisUdp = null;
    }

    void aisStatus(String s) { js("window.onAisStatus&&onAisStatus(" + JSONObject.quote(s) + ")"); }

    void startAis(final String host, final int port, final boolean udp) {
        stopAis();
        Thread t = new Thread(() -> {
            Thread me = Thread.currentThread();
            while (aisThread == me) {
                try {
                    aisStatus("Đang kết nối…");
                    if (udp) {
                        DatagramSocket ds = new DatagramSocket(null);
                        ds.setReuseAddress(true);
                        ds.bind(new InetSocketAddress(port));
                        aisUdp = ds;
                        aisStatus("Đang nghe UDP cổng " + port);
                        byte[] buf = new byte[4096];
                        while (aisThread == me) {
                            DatagramPacket pk = new DatagramPacket(buf, buf.length);
                            ds.receive(pk);
                            String txt = new String(pk.getData(), 0, pk.getLength(), StandardCharsets.US_ASCII);
                            for (String ln : txt.split("[\\r\\n]+")) nmea(ln);
                        }
                    } else {
                        Socket sk = new Socket();
                        aisSock = sk;
                        sk.connect(new InetSocketAddress(host, port), 8000);
                        aisStatus("Đã kết nối " + host + ":" + port);
                        BufferedReader r = new BufferedReader(new InputStreamReader(sk.getInputStream(), StandardCharsets.US_ASCII));
                        String ln;
                        while (aisThread == me && (ln = r.readLine()) != null) nmea(ln);
                    }
                } catch (Exception e) {
                    if (aisThread == me) aisStatus("Mất kết nối, thử lại sau 5 giây");
                }
                try { if (aisSock != null) aisSock.close(); } catch (Exception ignored) {}
                try { if (aisUdp != null) aisUdp.close(); } catch (Exception ignored) {}
                if (aisThread != me) break;
                try { Thread.sleep(5000); } catch (InterruptedException e) { break; }
            }
        });
        aisThread = t;
        t.start();
    }

    // Gom các câu NMEA (AIS "!" và GPS/la bàn "$") gửi sang giao diện 5 lần mỗi giây
    final StringBuilder nmeaBuf = new StringBuilder();
    boolean nmeaPending = false;
    final Runnable nmeaFlush = new Runnable() {
        @Override public void run() {
            String s;
            synchronized (nmeaBuf) { s = nmeaBuf.toString(); nmeaBuf.setLength(0); nmeaPending = false; }
            if (web != null && s.length() > 0) web.evaluateJavascript("window.onNmeaBatch&&onNmeaBatch(" + JSONObject.quote(s) + ")", null);
        }
    };

    void nmea(String ln) {
        ln = ln.trim();
        if (ln.length() < 6 || ln.length() > 300) return;
        if (ln.indexOf('!') < 0 && ln.indexOf('$') < 0) return;
        synchronized (nmeaBuf) {
            if (nmeaBuf.length() < 200000) nmeaBuf.append(ln).append('\n');
            if (nmeaPending) return;
            nmeaPending = true;
        }
        ui.postDelayed(nmeaFlush, 200);
    }

    // ---------- cầu nối JavaScript ----------
    class Bridge {
        File dataFile(String key) {
            File d = new File(getFilesDir(), "data");
            d.mkdirs();
            return new File(d, key.replaceAll("[^a-zA-Z0-9_]", "_") + ".json");
        }

        @JavascriptInterface public void ready() {
            // máy đã cài hải đồ nền bằng bản trước (vùng phủ sai): đưa bản đồ tới đúng vùng một lần
            File shown = new File(getFilesDir(), "default_chart_shown2");
            if (!shown.exists() && infos.containsKey("Hải đồ nền")) {
                try { shown.createNewFile(); } catch (Exception ignored) {}
                ui.postDelayed(() -> js("window.onChartsChanged&&onChartsChanged(" + JSONObject.quote("Hải đồ nền") + ")"), 800);
            }
        }

        @JavascriptInterface public String load(String key) {
            try {
                File f = dataFile(key);
                if (!f.exists()) return null;
                return new String(readAll(new FileInputStream(f)), StandardCharsets.UTF_8);
            } catch (Exception e) { return null; }
        }

        @JavascriptInterface public synchronized void save(String key, String val) {
            try {
                File f = dataFile(key), tmp = new File(f.getPath() + ".tmp");
                FileOutputStream o = new FileOutputStream(tmp);
                o.write(val.getBytes(StandardCharsets.UTF_8));
                o.close();
                tmp.renameTo(f);
            } catch (Exception ignored) {}
        }

        @JavascriptInterface public String listCharts() {
            JSONArray a = new JSONArray();
            for (JSONObject o : infos.values()) { synchronized (o) { try { a.put(new JSONObject(o.toString())); } catch (Exception ignored) {} } }
            return a.toString();
        }

        // ID máy cho bản quyền: ANDROID_ID cố định theo máy + khoá ký app (cài lại app vẫn giữ nguyên)
        @JavascriptInterface public String deviceId() {
            String a = android.provider.Settings.Secure.getString(getContentResolver(), android.provider.Settings.Secure.ANDROID_ID);
            return (a == null ? "" : a) + "|" + android.os.Build.MANUFACTURER + "|" + android.os.Build.MODEL;
        }

        @JavascriptInterface public void pickMbtiles() { ui.post(() -> pick(REQ_MBT)); }

        @JavascriptInterface public void deleteChart(String id) {
            infos.remove(id);
            closePool(id);
            File[] fs = chartDir().listFiles();
            if (fs != null) for (File f : fs) {
                String nm = f.getName();
                if ((nm.matches("(?i).*\\.(mbtiles|kap|sqlitedb)$") && nm.replaceAll(EXT_RE, "").equals(id)) || nm.equals(id + ".mbtiles.zooms.json")) f.delete();
            }
        }

        @JavascriptInterface public void importGpx() { ui.post(() -> pick(REQ_GPX_IN)); }

        @JavascriptInterface public void exportGpx(final String name, String content) {
            pendingGpx = content;
            ui.post(() -> {
                Intent i = new Intent(Intent.ACTION_CREATE_DOCUMENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("application/gpx+xml");
                i.putExtra(Intent.EXTRA_TITLE, name);
                try { startActivityForResult(i, REQ_GPX_OUT); } catch (Exception e) { toast("Không mở được trình lưu tệp"); }
            });
        }

        @JavascriptInterface public void alarm(final boolean on) {
            ui.post(() -> {
                ui.removeCallbacks(alarmTick);
                alarmOn = on;
                if (on) alarmTick.run();
            });
        }

        @JavascriptInterface public void beep() {
            ui.post(() -> {
                try {
                    if (tone == null) tone = new ToneGenerator(AudioManager.STREAM_ALARM, 100);
                    tone.startTone(ToneGenerator.TONE_PROP_BEEP2, 600);
                } catch (Exception ignored) {}
            });
        }

        @JavascriptInterface public void aisConnect(String host, int port, String proto) { startAis(host, port, "udp".equals(proto)); }

        @JavascriptInterface public void aisDisconnect() { stopAis(); aisStatus("Đã ngắt"); }

        @JavascriptInterface public void openUrl(final String url) {
            if (url == null || !(url.startsWith("https://") || url.startsWith("http://") || url.startsWith("tel:"))) return;
            ui.post(() -> {
                try {
                    Intent i = new Intent(url.startsWith("tel:") ? Intent.ACTION_DIAL : Intent.ACTION_VIEW, Uri.parse(url));
                    startActivity(i);
                } catch (Exception e) {
                    toast("Không mở được liên kết");
                }
            });
        }
    }
}
