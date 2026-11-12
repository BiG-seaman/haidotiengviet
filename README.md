# Hải Đồ Mở – app hải đồ Android

App xem hải đồ offline với GPS, nạp được tệp **.mbtiles**, điểm đánh dấu, tuyến, vết đi, đo khoảng cách,
báo động neo, AIS (NMEA qua Wi‑Fi), thời tiết biển, nhập/xuất GPX.

## Cách lấy tệp APK

### Cách 1 – GitHub tự build (không cần cài gì)
1. Tạo tài khoản tại github.com, tạo một kho (repository) mới, để trống.
2. Giải nén tệp zip, tải **toàn bộ nội dung** lên kho (trên máy tính: nút *Add file → Upload files*, kéo thả cả
   các thư mục `app`, `.github` và các tệp ở gốc). Thư mục `.github` là thư mục ẩn – nhớ bật hiện tệp ẩn.
3. Vào thẻ **Actions** → chọn lần chạy *Build APK* → đợi 3–5 phút → tải mục **HaiDoMo-apk** ở cuối trang.
4. Giải nén, chép `app-debug.apk` vào điện thoại và cài (cho phép "cài từ nguồn không xác định").

### Cách 2 – Android Studio
Mở thư mục này bằng Android Studio → *Build → Build APK(s)*.

## Nạp hải đồ MBTiles
Nút ▤ (Lớp hải đồ) → **Nạp tệp .mbtiles** → chọn tệp. App chép tệp vào bộ nhớ riêng nên cần đủ dung lượng trống;
nạp xong có thể xoá tệp gốc. Chỉ hỗ trợ MBTiles dạng ảnh (PNG/JPG/WebP), không hỗ trợ dạng vector (pbf).

## Lưu ý
- Bản đồ nền OpenStreetMap/OpenSeaMap cần mạng lần đầu; vùng đã xem sẽ được lưu để dùng offline.
- GPS, ghi vết và báo động neo chỉ chạy khi app đang mở trên màn hình (app giữ màn hình luôn sáng).
- App chỉ hỗ trợ tham khảo, không thay thế hải đồ chính thức và thiết bị hàng hải.
