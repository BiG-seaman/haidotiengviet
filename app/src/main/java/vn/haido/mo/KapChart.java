package vn.haido.mo;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.nio.MappedByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.CRC32;
import java.util.zip.Deflater;

/**
 * Hải đồ raster BSB/KAP (NOAA, UKHO, hải đồ quét...). Giải mã ảnh chỉ số màu một lần ra tệp đệm
 * (ánh xạ bộ nhớ, không chiếm heap), khớp phép biến đổi (kinh độ, Mercator Y) -> (x, y) ảnh từ các
 * điểm REF/ bằng bình phương tối thiểu, rồi cắt ô 256x256 Web Mercator theo yêu cầu (lưu đệm PNG).
 * Không phụ thuộc Android để có thể kiểm thử bằng Java thường.
 */
public class KapChart {
    public final File file;
    public int w, h, depth = 7, minzoom, maxzoom;
    public String name = "";
    public double[] bounds;          // lon_w, lat_s, lon_e, lat_n
    final int[] pal = new int[256];  // 0xRRGGBB
    final List<double[]> ply = new ArrayList<>();   // lat, lon
    int order;
    double lon0, m0;
    double[] cx, cy;
    int rowsAt;
    final File cache;
    volatile MappedByteBuffer img;
    final Object lock = new Object();

    public static boolean isKap(File f) {
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] b = new byte[4096];
            int n = in.read(b);
            if (n <= 0) return false;
            String s = new String(b, 0, n, StandardCharsets.ISO_8859_1);
            return s.contains("RA=") && (s.contains("BSB/") || s.contains("NOS/"));
        } catch (Exception e) {
            return false;
        }
    }

    static double merc(double lat) {
        lat = Math.max(-85.05, Math.min(85.05, lat));
        return Math.log(Math.tan(Math.PI / 4 + Math.toRadians(lat) / 2));
    }

    static int nTerms(int order) { return order == 3 ? 10 : order == 2 ? 6 : 3; }

    static void terms(double l, double m, int order, double[] t) {
        t[0] = 1; t[1] = l; t[2] = m;
        if (order >= 2) { t[3] = l * l; t[4] = l * m; t[5] = m * m; }
        if (order == 3) { t[6] = l * l * l; t[7] = l * l * m; t[8] = l * m * m; t[9] = m * m * m; }
    }

    /** Bình phương tối thiểu qua phương trình chuẩn (A^T A) c = A^T b, khử Gauss có chọn trụ. */
    static double[] lsq(double[][] A, double[] b) {
        int k = A[0].length;
        double[][] M = new double[k][k + 1];
        for (int r = 0; r < A.length; r++)
            for (int i = 0; i < k; i++) {
                for (int j = 0; j < k; j++) M[i][j] += A[r][i] * A[r][j];
                M[i][k] += A[r][i] * b[r];
            }
        for (int c = 0; c < k; c++) {
            int p = c;
            for (int r = c + 1; r < k; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
            double[] t = M[c]; M[c] = M[p]; M[p] = t;
            double d = M[c][c];
            if (Math.abs(d) < 1e-300) continue;
            for (int r = 0; r < k; r++) {
                if (r == c) continue;
                double f = M[r][c] / d;
                if (f != 0) for (int j = c; j <= k; j++) M[r][j] -= f * M[c][j];
            }
        }
        double[] x = new double[k];
        for (int i = 0; i < k; i++) x[i] = Math.abs(M[i][i]) < 1e-300 ? 0 : M[i][k] / M[i][i];
        return x;
    }

    public KapChart(File f, File cacheRoot) throws Exception {
        file = f;
        byte[] head;
        try (RandomAccessFile raf = new RandomAccessFile(f, "r")) {
            int n = (int) Math.min(raf.length(), 1 << 20);
            head = new byte[n];
            raf.readFully(head);
        }
        int end = -1;
        for (int i = 0; i < head.length; i++) if (head[i] == 0x1a) { end = i; break; }
        if (end < 0) throw new Exception("khong co phan dau KAP");
        String hdr = new String(head, 0, end, StandardCharsets.ISO_8859_1);
        List<String> lines = new ArrayList<>();
        for (String ln : hdr.split("\r?\n")) {
            if (ln.startsWith("    ") && !lines.isEmpty()) lines.set(lines.size() - 1, lines.get(lines.size() - 1) + "," + ln.trim());
            else if (!ln.trim().isEmpty() && !ln.startsWith("!")) lines.add(ln.trim());
        }
        List<double[]> refs = new ArrayList<>();
        double dlat = 0, dlon = 0;
        Pattern ra = Pattern.compile("RA=\\s*(\\d+)\\s*,\\s*(\\d+)"), na = Pattern.compile("NA=([^,]*)");
        for (String ln : lines) {
            int s = ln.indexOf('/');
            if (s < 0) continue;
            String tag = ln.substring(0, s).toUpperCase(), body = ln.substring(s + 1);
            String[] p = body.split(",");
            try {
                if (tag.equals("BSB") || tag.equals("NOS")) {
                    Matcher m = ra.matcher(body);
                    if (m.find()) { w = Integer.parseInt(m.group(1)); h = Integer.parseInt(m.group(2)); }
                    m = na.matcher(body);
                    if (m.find() && !m.group(1).trim().isEmpty()) name = m.group(1).trim();
                } else if (tag.equals("RGB") && p.length >= 4) {
                    int i = Integer.parseInt(p[0].trim());
                    if (i >= 0 && i < 256) pal[i] = (Integer.parseInt(p[1].trim()) << 16) | (Integer.parseInt(p[2].trim()) << 8) | Integer.parseInt(p[3].trim());
                } else if (tag.equals("REF") && p.length >= 5) {
                    refs.add(new double[]{Double.parseDouble(p[1].trim()), Double.parseDouble(p[2].trim()), Double.parseDouble(p[3].trim()), Double.parseDouble(p[4].trim())});
                } else if (tag.equals("PLY") && p.length >= 3) {
                    ply.add(new double[]{Double.parseDouble(p[1].trim()), Double.parseDouble(p[2].trim())});
                } else if (tag.equals("DTM") && p.length >= 2) {
                    dlat = Double.parseDouble(p[0].trim()) / 3600.0;
                    dlon = Double.parseDouble(p[1].trim()) / 3600.0;
                }
            } catch (NumberFormatException ignored) {}
        }
        if (w <= 0 || h <= 0 || refs.size() < 3) throw new Exception("thieu RA= hoac diem REF");
        int p = end + 1;
        while (p < head.length && head[p] == 0) p++;
        depth = p < head.length ? head[p] & 0xff : 7;
        if (depth < 1 || depth > 7) depth = 7;
        rowsAt = p + 1;
        for (double[] q : ply) { q[0] += dlat; q[1] += dlon; }

        int nr = refs.size();
        order = nr >= 20 ? 3 : nr >= 8 ? 2 : 1;
        double sl = 0, sm = 0;
        double[] lon = new double[nr], mm = new double[nr], X = new double[nr], Y = new double[nr];
        for (int i = 0; i < nr; i++) {
            double[] r = refs.get(i);
            X[i] = r[0]; Y[i] = r[1];
            lon[i] = r[3] + dlon;
            mm[i] = merc(r[2] + dlat);
            sl += lon[i]; sm += mm[i];
        }
        lon0 = sl / nr; m0 = sm / nr;
        int k = nTerms(order);
        double[][] A = new double[nr][k];
        for (int i = 0; i < nr; i++) terms(Math.toRadians(lon[i] - lon0), mm[i] - m0, order, A[i]);
        cx = lsq(A, X);
        cy = lsq(A, Y);
        // phép ngược bậc 1 (x, y -> lon, merc) để tính vùng phủ và độ phân giải
        double[][] B = new double[nr][];
        for (int i = 0; i < nr; i++) B[i] = new double[]{1, X[i], Y[i]};
        double[] il = lsq(B, lon), im = lsq(B, mm);
        double a0 = 1e9, a1 = -1e9, b0 = 1e9, b1 = -1e9;
        if (ply.size() >= 3) {
            for (double[] q : ply) { b0 = Math.min(b0, q[0]); b1 = Math.max(b1, q[0]); a0 = Math.min(a0, q[1]); a1 = Math.max(a1, q[1]); }
        } else {
            double[][] cs = {{0, 0}, {w, 0}, {0, h}, {w, h}};
            for (double[] c : cs) {
                double lo = il[0] + il[1] * c[0] + il[2] * c[1];
                double la = Math.toDegrees(Math.atan(Math.sinh(im[0] + im[1] * c[0] + im[2] * c[1])));
                a0 = Math.min(a0, lo); a1 = Math.max(a1, lo); b0 = Math.min(b0, la); b1 = Math.max(b1, la);
            }
        }
        bounds = new double[]{a0, b0, a1, b1};
        double dpp = Math.hypot(il[1], il[2]);
        if (dpp <= 0) dpp = 1e-6;
        double nat = Math.log(360.0 / (256 * dpp)) / Math.log(2);
        maxzoom = Math.max(1, Math.min(22, (int) Math.round(nat)));
        minzoom = Math.max(1, maxzoom - 7);
        MessageDigest md = MessageDigest.getInstance("SHA-1");
        md.update((f.getAbsolutePath() + f.length() + ":" + f.lastModified()).getBytes(StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) sb.append(String.format("%02x", b));
        cache = new File(cacheRoot, sb.toString());
        cache.mkdirs();
    }

    // ---------- giải mã ----------
    void decodeAll(File out) throws Exception {
        byte[] data;
        try (RandomAccessFile raf = new RandomAccessFile(file, "r")) {
            data = new byte[(int) raf.length()];
            raf.readFully(data);
        }
        int n = data.length;
        int cshift = 7 - depth, cmask = ((1 << depth) - 1) << cshift, nmask = (1 << cshift) - 1;
        long[] offs = null;
        if (n >= 8) {
            long ti = ((data[n - 4] & 0xffL) << 24) | ((data[n - 3] & 0xff) << 16) | ((data[n - 2] & 0xff) << 8) | (data[n - 1] & 0xff);
            if (ti >= rowsAt && ti + 4L * h <= n - 4) {
                offs = new long[h];
                boolean ok = true;
                for (int r = 0; r < h; r++) {
                    int q = (int) ti + 4 * r;
                    long o = ((data[q] & 0xffL) << 24) | ((data[q + 1] & 0xff) << 16) | ((data[q + 2] & 0xff) << 8) | (data[q + 3] & 0xff);
                    if (o < rowsAt || o >= ti) { ok = false; break; }
                    offs[r] = o;
                }
                if (!ok) offs = null;
            }
        }
        File tmp = new File(out.getPath() + ".tmp");
        try (FileOutputStream fo = new FileOutputStream(tmp)) {
            byte[] line = new byte[w];
            int p = rowsAt, written = 0;
            // ghi tuần tự; các dòng ngoài thứ tự (hiếm) được xử lý bằng cách giải mã theo bảng chỉ mục
            for (int row = 0; row < h; row++) {
                java.util.Arrays.fill(line, (byte) 0);
                if (offs != null) p = (int) offs[row];
                while (p < n) { int b = data[p++] & 0xff; if ((b & 0x80) == 0) break; }   // số dòng (varint), bỏ qua
                int x = 0;
                while (p < n) {
                    int c = data[p++] & 0xff;
                    if (c == 0) break;
                    int col = (c & cmask) >> cshift;
                    int cnt = c & nmask;
                    while ((c & 0x80) != 0 && p < n) { c = data[p++] & 0xff; cnt = (cnt << 7) + (c & 0x7f); }
                    cnt++;
                    int e = Math.min(w, x + cnt);
                    for (int i = x; i < e; i++) line[i] = (byte) col;
                    x = e;
                }
                fo.write(line);
                written++;
                if (p >= n && row < h - 1) {      // tệp cụt: phần còn lại để trống
                    java.util.Arrays.fill(line, (byte) 0);
                    for (int r = row + 1; r < h; r++) fo.write(line);
                    break;
                }
            }
        }
        if (out.exists()) out.delete();
        if (!tmp.renameTo(out)) throw new Exception("rename");
    }

    MappedByteBuffer image() throws Exception {
        MappedByteBuffer m = img;
        if (m != null) return m;
        synchronized (lock) {
            if (img == null) {
                File f = new File(cache, "idx.u8");
                if (!(f.exists() && f.length() == (long) w * h)) decodeAll(f);
                try (RandomAccessFile raf = new RandomAccessFile(f, "r")) {
                    img = raf.getChannel().map(FileChannel.MapMode.READ_ONLY, 0, (long) w * h);
                }
            }
            return img;
        }
    }

    // ---------- cắt ô ----------
    public byte[] tile(int z, int x, int y) throws Exception {
        File f = new File(cache, z + "/" + x + "/" + y + ".png");
        if (f.exists()) {
            if (f.length() == 0) return null;
            byte[] d = new byte[(int) f.length()];
            try (FileInputStream in = new FileInputStream(f)) { int o = 0, r; while (o < d.length && (r = in.read(d, o, d.length - o)) > 0) o += r; }
            return d;
        }
        byte[] d = render(z, x, y);
        try {
            f.getParentFile().mkdirs();
            File t = new File(f.getPath() + ".tmp" + Thread.currentThread().getId());
            try (FileOutputStream o = new FileOutputStream(t)) { if (d != null) o.write(d); }
            t.renameTo(f);
        } catch (Exception ignored) {}
        return d;
    }

    static double tileLat(double y, int n) { return Math.toDegrees(Math.atan(Math.sinh(Math.PI * (1 - 2 * y / n)))); }

    byte[] render(int z, int tx, int ty) throws Exception {
        int n = 1 << z;
        double lonW = (double) tx / n * 360 - 180, lonE = (double) (tx + 1) / n * 360 - 180;
        double latN = tileLat(ty, n), latS = tileLat(ty + 1, n);
        if (lonE < bounds[0] || lonW > bounds[2] || latN < bounds[1] || latS > bounds[3]) return null;
        MappedByteBuffer im = image();
        final int S = 256;
        int[] px = new int[S * S];
        boolean any = false;
        // mặt nạ đường viền PLY (tô quét dòng, chẵn-lẻ) theo toạ độ điểm ảnh của ô
        double[] polX = null, polY = null;
        if (ply.size() >= 3) {
            int k = ply.size();
            polX = new double[k]; polY = new double[k];
            double topM = Math.PI * (1 - 2.0 * ty / n), scale = S / (2 * Math.PI / n);
            for (int i = 0; i < k; i++) {
                double[] q = ply.get(i);
                polX[i] = (q[1] - lonW) / (lonE - lonW) * S;
                polY[i] = (topM - merc(q[0])) * scale;
            }
        }
        int kt = nTerms(order);
        double[] t = new double[kt];
        double[] xs = new double[S * 2];
        for (int r = 0; r < S; r++) {
            double fy = r + 0.5;
            double M = Math.PI * (1 - 2 * (ty + fy / S) / n) - m0;
            int nx = 0;
            if (polX != null) {
                int k = polX.length;
                for (int i = 0, j = k - 1; i < k; j = i++) {
                    if ((polY[i] > fy) != (polY[j] > fy))
                        xs[nx++] = polX[i] + (fy - polY[i]) * (polX[j] - polX[i]) / (polY[j] - polY[i]);
                    if (nx >= xs.length - 1) break;
                }
                java.util.Arrays.sort(xs, 0, nx);
                if (nx == 0) continue;
            }
            int seg = 0;
            for (int c = 0; c < S; c++) {
                double fx = c + 0.5;
                if (polX != null) {
                    while (seg < nx && xs[seg] <= fx) seg++;
                    if ((seg & 1) == 0) continue;     // ngoài đường viền
                }
                double L = Math.toRadians(lonW + (lonE - lonW) * fx / S - lon0);
                terms(L, M, order, t);
                double ix = 0, iy = 0;
                for (int i = 0; i < kt; i++) { ix += cx[i] * t[i]; iy += cy[i] * t[i]; }
                int X = (int) Math.floor(ix), Y = (int) Math.floor(iy);
                if (X < 0 || Y < 0 || X >= w || Y >= h) continue;
                int idx = im.get((int) ((long) Y * w + X)) & 0xff;
                if (idx == 0) continue;
                px[r * S + c] = 0xff000000 | pal[idx];
                any = true;
            }
        }
        return any ? png(px, S, S) : null;
    }

    // ---------- mã hoá PNG RGBA (không cần thư viện đồ hoạ) ----------
    static byte[] png(int[] argb, int W, int H) throws Exception {
        byte[] raw = new byte[H * (1 + W * 4)];
        int o = 0;
        for (int y = 0; y < H; y++) {
            raw[o++] = 0;
            for (int x = 0; x < W; x++) {
                int v = argb[y * W + x];
                raw[o++] = (byte) (v >> 16); raw[o++] = (byte) (v >> 8); raw[o++] = (byte) v; raw[o++] = (byte) (v >>> 24);
            }
        }
        Deflater d = new Deflater(3);
        d.setInput(raw);
        d.finish();
        ByteArrayOutputStream z = new ByteArrayOutputStream();
        byte[] buf = new byte[65536];
        while (!d.finished()) { int n = d.deflate(buf); z.write(buf, 0, n); }
        d.end();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(new byte[]{(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'});
        byte[] ihdr = {(byte) (W >> 24), (byte) (W >> 16), (byte) (W >> 8), (byte) W, (byte) (H >> 24), (byte) (H >> 16), (byte) (H >> 8), (byte) H, 8, 6, 0, 0, 0};
        chunk(out, "IHDR", ihdr);
        chunk(out, "IDAT", z.toByteArray());
        chunk(out, "IEND", new byte[0]);
        return out.toByteArray();
    }

    static void chunk(ByteArrayOutputStream out, String type, byte[] data) {
        int n = data.length;
        out.write(n >> 24); out.write(n >> 16); out.write(n >> 8); out.write(n);
        byte[] t = type.getBytes(StandardCharsets.ISO_8859_1);
        out.write(t, 0, 4);
        out.write(data, 0, n);
        CRC32 c = new CRC32();
        c.update(t);
        c.update(data);
        long v = c.getValue();
        out.write((int) (v >> 24)); out.write((int) (v >> 16)); out.write((int) (v >> 8)); out.write((int) v);
    }
}
