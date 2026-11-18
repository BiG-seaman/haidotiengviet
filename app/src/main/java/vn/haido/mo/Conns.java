package vn.haido.mo;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.hardware.usb.UsbDevice;
import android.hardware.usb.UsbDeviceConnection;
import android.hardware.usb.UsbManager;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;

import com.hoho.android.usbserial.driver.UsbSerialDriver;
import com.hoho.android.usbserial.driver.UsbSerialPort;
import com.hoho.android.usbserial.driver.UsbSerialProber;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Nhiều kết nối NMEA 0183 cùng lúc (giống trang "Kết nối" của OpenCPN):
 * TCP, UDP, và cổng COM qua cáp USB OTG (chip CP210x, CH340, PL2303, FTDI, CDC-ACM… nhờ thư viện usb-serial-for-android).
 * Mỗi kết nối tự kết nối lại khi mất; cổng COM có thể tự dò tốc độ baud.
 */
public class Conns {
    public interface Sink {
        void line(String id, String ln);
        void status(String id, String s);
    }

    static final int[] BAUDS = {4800, 38400, 9600, 115200, 19200, 57600};
    static final String ACTION_USB = "vn.haido.mo.USB_PERMISSION";

    final Context ctx;
    final Sink sink;
    final Map<String, Conn> conns = new ConcurrentHashMap<>();

    static final long NO_DATA_MS = 15000;      // không có dữ liệu quá lâu = coi như mất kết nối (thiết bị khởi động lại, mất Wi-Fi…)

    public Conns(Context ctx, Sink sink) {
        this.ctx = ctx; this.sink = sink;
        // Wi-Fi vừa có lại (vd AIS Pilot khởi động xong) → kết nối lại ngay, không chờ
        try {
            ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            cm.registerNetworkCallback(new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build(),
                new ConnectivityManager.NetworkCallback() {
                    @Override public void onAvailable(Network n) { kick(); }
                    @Override public void onLost(Network n) { kick(); }
                });
        } catch (Exception ignored) {}
    }

    /** Mạng Wi-Fi hiện có (kể cả Wi-Fi "không có internet" của AIS Pilot), để kết nối không bị Android đẩy sang 4G. */
    Network wifiNet() {
        try {
            ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            for (Network n : cm.getAllNetworks()) {
                NetworkCapabilities c = cm.getNetworkCapabilities(n);
                if (c != null && c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return n;
            }
        } catch (Exception ignored) {}
        return null;
    }

    static boolean isLocal(String host) {
        try { InetAddress a = InetAddress.getByName(host); return a.isSiteLocalAddress() || a.isLinkLocalAddress(); }
        catch (Exception e) { return host.startsWith("192.168.") || host.startsWith("10.") || host.startsWith("172."); }
    }

    /** Đóng kết nối mạng hiện tại để các kết nối thử lại ngay. */
    void kick() {
        for (Conn c : conns.values()) {
            if ("serial".equals(c.type)) continue;
            c.kicked = true;
            try { if (c.sock != null) c.sock.close(); } catch (Exception ignored) {}
            try { if (c.udp != null) c.udp.close(); } catch (Exception ignored) {}
            if (c.th != null) c.th.interrupt();
        }
    }

    class Conn {
        final String id, type, host;
        final int port, baud;
        volatile boolean run = true;
        volatile Socket sock;
        volatile DatagramSocket udp;
        volatile UsbSerialPort usb;
        volatile OutputStream out;
        volatile boolean kicked;
        Thread th;

        Conn(JSONObject o) {
            id = o.optString("id");
            type = o.optString("type", "tcp");
            host = o.optString("host", "");
            port = o.optInt("port", 10110);
            baud = o.optInt("baud", 0);
        }

        void close() {
            run = false;
            try { if (sock != null) sock.close(); } catch (Exception ignored) {}
            try { if (udp != null) udp.close(); } catch (Exception ignored) {}
            try { if (usb != null) usb.close(); } catch (Exception ignored) {}
            if (th != null) th.interrupt();
        }

        void loop() {
            while (run) {
                try {
                    sink.status(id, "Đang kết nối…");
                    if ("udp".equals(type)) runUdp();
                    else if ("serial".equals(type)) runUsb();
                    else runTcp();
                } catch (Exception e) {
                    if (run) sink.status(id, (kicked ? "Mạng Wi-Fi thay đổi, đang kết nối lại…" : "Mất kết nối: " + (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()) + " – tự kết nối lại"));
                }
                try { if (sock != null) sock.close(); } catch (Exception ignored) {}
                try { if (udp != null) udp.close(); } catch (Exception ignored) {}
                try { if (usb != null) usb.close(); } catch (Exception ignored) {}
                sock = null; udp = null; usb = null; out = null;
                if (!run) break;
                if (kicked) { kicked = false; try { Thread.sleep(1500); } catch (InterruptedException e) { if (!run) break; } continue; }
                try { Thread.sleep(3000); } catch (InterruptedException e) { if (!run) break; }
            }
            sink.status(id, "Đã ngắt");
        }

        void runTcp() throws Exception {
            Network wn = isLocal(host) ? wifiNet() : null;
            Socket sk = wn != null ? wn.getSocketFactory().createSocket() : new Socket();
            sock = sk;
            sk.setKeepAlive(true);
            sk.setTcpNoDelay(true);
            sk.connect(new InetSocketAddress(host, port), 8000);
            sk.setSoTimeout(3000);
            out = sk.getOutputStream();
            sink.status(id, "Đã kết nối " + host + ":" + port);
            InputStream in = sk.getInputStream();
            byte[] buf = new byte[8192];
            StringBuilder pend = new StringBuilder();
            long last = System.currentTimeMillis();
            boolean warned = false;
            while (run) {
                int n;
                try { n = in.read(buf); }
                catch (SocketTimeoutException te) {
                    long idle = System.currentTimeMillis() - last;
                    if (idle > NO_DATA_MS) throw new Exception("không nhận dữ liệu " + (idle / 1000) + " giây");
                    if (idle > 8000 && !warned) { warned = true; sink.status(id, "Đã kết nối nhưng chưa có dữ liệu…"); }
                    continue;
                }
                if (n < 0) throw new Exception("thiết bị đóng kết nối");
                last = System.currentTimeMillis();
                if (warned) { warned = false; sink.status(id, "Đã kết nối " + host + ":" + port); }
                pend.append(new String(buf, 0, n, StandardCharsets.US_ASCII));
                int k;
                while ((k = pend.indexOf("\n")) >= 0) { sink.line(id, pend.substring(0, k)); pend.delete(0, k + 1); }
                if (pend.length() > 16384) pend.setLength(0);
            }
        }

        void runUdp() throws Exception {
            DatagramSocket ds = new DatagramSocket(null);
            ds.setReuseAddress(true);
            ds.bind(new InetSocketAddress(port));
            udp = ds;
            Network wn = wifiNet();
            if (wn != null) try { wn.bindSocket(ds); } catch (Exception ignored) {}
            ds.setSoTimeout(3000);
            sink.status(id, "Đang nghe UDP cổng " + port);
            byte[] buf = new byte[8192];
            long last = System.currentTimeMillis();
            while (run) {
                DatagramPacket pk = new DatagramPacket(buf, buf.length);
                try { ds.receive(pk); }
                catch (SocketTimeoutException te) {
                    if (System.currentTimeMillis() - last > NO_DATA_MS * 2) throw new Exception("không nhận dữ liệu UDP");
                    continue;
                }
                last = System.currentTimeMillis();
                String txt = new String(pk.getData(), 0, pk.getLength(), StandardCharsets.US_ASCII);
                for (String ln : txt.split("[\\r\\n]+")) sink.line(id, ln);
            }
        }

        void runUsb() throws Exception {
            UsbManager um = (UsbManager) ctx.getSystemService(Context.USB_SERVICE);
            UsbSerialDriver drv = findDriver(um, host);
            if (drv == null) throw new Exception("chưa cắm thiết bị USB-COM (cáp OTG)");
            UsbDevice dev = drv.getDevice();
            if (!um.hasPermission(dev)) {
                askPermission(um, dev);
                sink.status(id, "Hãy bấm Cho phép để app dùng thiết bị USB");
                for (int i = 0; i < 60 && run && !um.hasPermission(dev); i++) Thread.sleep(500);
                if (!um.hasPermission(dev)) throw new Exception("chưa được cho phép dùng USB");
            }
            int[] tries = baud > 0 ? new int[]{baud} : BAUDS;
            for (int b : tries) {
                if (!run) return;
                UsbDeviceConnection c = um.openDevice(dev);
                if (c == null) throw new Exception("không mở được thiết bị USB");
                UsbSerialPort p = drv.getPorts().get(0);
                p.open(c);
                p.setParameters(b, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE);
                try { p.setDTR(true); p.setRTS(true); } catch (Exception ignored) {}
                usb = p;
                if (baud == 0) sink.status(id, "Đang dò tốc độ " + b + " baud…");
                if (readUsb(p, b, baud == 0)) return;     // đọc tới khi mất kết nối
                try { p.close(); } catch (Exception ignored) {}
                usb = null;
            }
            throw new Exception("không nhận ra dữ liệu NMEA ở các tốc độ baud");
        }

        /** Trả về false nếu đang dò baud mà 3 giây không thấy câu NMEA hợp lệ. */
        boolean readUsb(final UsbSerialPort p, int b, boolean probing) throws Exception {
            out = new OutputStream() {
                @Override public void write(int x) throws java.io.IOException { write(new byte[]{(byte) x}, 0, 1); }
                @Override public void write(byte[] d, int o, int l) throws java.io.IOException {
                    byte[] s = new byte[l]; System.arraycopy(d, o, s, 0, l); p.write(s, 1000);
                }
            };
            byte[] buf = new byte[4096];
            StringBuilder pend = new StringBuilder();
            long t0 = System.currentTimeMillis();
            boolean ok = !probing;
            if (ok) sink.status(id, "Đã mở cổng USB-COM (" + b + " baud)");
            while (run) {
                int n = p.read(buf, 500);
                if (n > 0) {
                    pend.append(new String(buf, 0, n, StandardCharsets.US_ASCII));
                    int k;
                    while ((k = pend.indexOf("\n")) >= 0) {
                        String ln = pend.substring(0, k).trim();
                        pend.delete(0, k + 1);
                        if (!ok && validNmea(ln)) { ok = true; sink.status(id, "Đã mở cổng USB-COM (tự dò: " + b + " baud)"); }
                        if (ok) sink.line(id, ln);
                    }
                    if (pend.length() > 8192) pend.setLength(0);
                }
                if (!ok && System.currentTimeMillis() - t0 > 3000) return false;
            }
            return true;
        }
    }

    static boolean validNmea(String s) {
        int a = Math.max(s.indexOf('$'), s.indexOf('!'));
        if (a < 0) return false;
        int st = s.indexOf('*', a);
        if (st < 0 || st + 3 > s.length()) return false;
        int c = 0;
        for (int i = a + 1; i < st; i++) c ^= s.charAt(i);
        try { return Integer.parseInt(s.substring(st + 1, st + 3), 16) == c; } catch (Exception e) { return false; }
    }

    UsbSerialDriver findDriver(UsbManager um, String key) {
        List<UsbSerialDriver> list = UsbSerialProber.getDefaultProber().findAllDrivers(um);
        if (list.isEmpty()) return null;
        for (UsbSerialDriver d : list) if (key.equals(usbKey(d.getDevice()))) return d;
        return list.get(0);                       // chỉ có một thiết bị / đã đổi cổng cắm
    }

    static String usbKey(UsbDevice d) { return "usb:" + d.getVendorId() + ":" + d.getProductId(); }

    void askPermission(UsbManager um, UsbDevice dev) {
        int fl = Build.VERSION.SDK_INT >= 31 ? PendingIntent.FLAG_MUTABLE : 0;
        Intent it = new Intent(ACTION_USB).setPackage(ctx.getPackageName());
        um.requestPermission(dev, PendingIntent.getBroadcast(ctx, 0, it, fl));
    }

    /** Danh sách thiết bị USB-COM đang cắm: [{port, desc}] */
    public String listUsb() {
        JSONArray a = new JSONArray();
        try {
            UsbManager um = (UsbManager) ctx.getSystemService(Context.USB_SERVICE);
            for (UsbSerialDriver d : UsbSerialProber.getDefaultProber().findAllDrivers(um)) {
                UsbDevice dev = d.getDevice();
                String nm = d.getClass().getSimpleName().replace("SerialDriver", "");
                String prod = Build.VERSION.SDK_INT >= 21 && dev.getProductName() != null ? dev.getProductName() : "";
                JSONObject o = new JSONObject();
                o.put("port", usbKey(dev));
                o.put("desc", (prod.isEmpty() ? "USB-COM" : prod) + " (" + nm + ")");
                a.put(o);
            }
        } catch (Exception ignored) {}
        return a.toString();
    }

    public void open(String json) {
        try {
            JSONObject o = new JSONObject(json);
            close(o.optString("id"));
            final Conn c = new Conn(o);
            conns.put(c.id, c);
            c.th = new Thread(c::loop, "conn-" + c.id);
            c.th.start();
        } catch (Exception e) { sink.status("", "Cấu hình kết nối lỗi"); }
    }

    public void close(String id) {
        Conn c = conns.remove(id);
        if (c != null) c.close();
    }

    public void closeAll() { for (String id : conns.keySet()) close(id); }

    /** Gửi câu NMEA ra kết nối (máy lái tự động / thiết bị nhận). */
    public void send(String id, final String text) {
        final Conn c = conns.get(id);
        if (c == null || c.out == null) return;
        new Thread(() -> {
            try {
                OutputStream o = c.out;
                if (o != null) { o.write(text.getBytes(StandardCharsets.US_ASCII)); o.flush(); }
            } catch (Exception ignored) {}
        }).start();
    }
}
