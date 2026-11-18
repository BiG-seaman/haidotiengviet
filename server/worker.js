// Máy chủ bản quyền Hải đồ VN (Cloudflare Worker + KV "LIC")
// - Khoá ký ECDSA P-256 được tạo tự động lần đầu và chỉ nằm trong KV (không ở đâu khác).
// - /trial: cấp mã dùng thử 7 ngày tính từ lần ĐẦU TIÊN máy đó liên hệ (cài lại app cũng không reset).
// - /redeem: đổi mã ngắn (HD-XXXX-XXXX-XX) lấy giấy phép ký số gắn với ID máy.
// - /admin: trang quản lý của chủ phần mềm (đặt mật khẩu ở lần mở đầu tiên).
const TRIAL_DAYS = 7;
const EPOCH = Date.UTC(2025, 0, 1);
const B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
const CORS = { "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "Content-Type" };
const json = (o, s = 200) => new Response(JSON.stringify(o), { status: s, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...CORS } });
const enc = new TextEncoder();

function b32(bytes) {
  let bits = 0, val = 0, out = "";
  for (const b of bytes) { val = (val << 8) | b; bits += 8; while (bits >= 5) { out += B32[(val >>> (bits - 5)) & 31]; bits -= 5; } }
  if (bits > 0) out += B32[(val << (5 - bits)) & 31];
  return out;
}
function randKey() {
  const r = crypto.getRandomValues(new Uint8Array(7));
  const s = b32(r).slice(0, 10);
  return "HD-" + s.slice(0, 4) + "-" + s.slice(4, 8) + "-" + s.slice(8, 10);
}
const normKey = k => { const s = String(k || "").toUpperCase().replace(/[^0-9A-Z]/g, "").replace(/O/g, "0").replace(/[IL]/g, "1"); return s.length === 12 && s.startsWith("HD") ? "HD-" + s.slice(2, 6) + "-" + s.slice(6, 10) + "-" + s.slice(10, 12) : null; };
const validId = id => typeof id === "string" && /^[AW][0-9A-Z]{11}$/.test(id);

async function keys(env) {
  let priv = await env.LIC.get("key:priv", "json"), pub = await env.LIC.get("key:pub", "json");
  if (!priv || !pub) {
    const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
    priv = await crypto.subtle.exportKey("jwk", kp.privateKey);
    pub = await crypto.subtle.exportKey("jwk", kp.publicKey);
    // không ghi đè nếu một yêu cầu song song đã tạo trước
    const again = await env.LIC.get("key:priv", "json");
    if (again) { priv = again; pub = await env.LIC.get("key:pub", "json"); }
    else { await env.LIC.put("key:priv", JSON.stringify(priv)); await env.LIC.put("key:pub", JSON.stringify(pub)); }
  }
  return { priv, pub: { kty: pub.kty, crv: pub.crv, x: pub.x, y: pub.y } };
}

// mã giấy phép = base32( ver(1) | loại(1: 1 dùng thử, 2 bản quyền) | hết hạn tính bằng phút từ 2025-01-01 (4) | chữ ký(64) )
async function issue(env, id, type, expMs) {
  const { priv } = await keys(env);
  const k = await crypto.subtle.importKey("jwk", priv, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const mins = Math.max(0, Math.floor((expMs - EPOCH) / 60000));
  const p = new Uint8Array([1, type, (mins >>> 24) & 255, (mins >>> 16) & 255, (mins >>> 8) & 255, mins & 255]);
  const msg = new Uint8Array([...p, ...enc.encode("HDVN|" + id)]);
  const sig = new Uint8Array(await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, k, msg));
  return b32(new Uint8Array([...p, ...sig]));
}

async function hashPw(pw, salt) {
  const k = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: enc.encode(salt), iterations: 100000 }, k, 256);
  return b32(new Uint8Array(bits));
}

async function bestLicense(env, id, now) {
  const l = await env.LIC.get("l:" + id, "json");
  if (l && l.exp > now) return { code: await issue(env, id, 2, l.exp), type: "paid", exp: l.exp, now };
  return null;
}

async function api(req, env, path) {
  const now = Date.now();
  let body = {};
  try { body = await req.json(); } catch (e) {}
  const ip = req.headers.get("CF-Connecting-IP") || "?";

  if (path === "/trial") {
    const id = body.id;
    if (!validId(id)) return json({ error: "ID máy không hợp lệ" }, 400);
    const paid = await bestLicense(env, id, now);
    if (paid) return json(paid);
    let t = await env.LIC.get("t:" + id, "json");
    if (!t) { t = { first: now, plat: String(body.plat || "").slice(0, 20), ver: String(body.ver || "").slice(0, 10), ip }; await env.LIC.put("t:" + id, JSON.stringify(t)); }
    const exp = t.first + TRIAL_DAYS * 86400000;
    const l = await env.LIC.get("l:" + id, "json");
    return json({ code: await issue(env, id, 1, exp), type: "trial", exp, now, first: t.first, expiredPaid: l ? l.exp : null });
  }

  if (path === "/redeem") {
    const id = body.id, key = normKey(body.key);
    if (!validId(id)) return json({ error: "ID máy không hợp lệ" }, 400);
    const failK = "fail:" + ip, fails = +(await env.LIC.get(failK) || 0);
    if (fails >= 15) return json({ error: "Nhập sai quá nhiều lần, thử lại sau 30 phút" }, 429);
    const k = key && await env.LIC.get("k:" + key, "json");
    if (!k || k.revoked) { await env.LIC.put(failK, String(fails + 1), { expirationTtl: 1800 }); return json({ error: "Mã kích hoạt không đúng" }, 404); }
    if (k.id && k.id !== id) return json({ error: "Mã này đã kích hoạt cho máy khác" }, 409);
    if (k.id === id && k.exp) return json({ code: await issue(env, id, 2, k.exp), type: "paid", exp: k.exp, now });
    if (k.forId && k.forId !== id) return json({ error: "Mã này được cấp cho máy khác" }, 409);
    const l = await env.LIC.get("l:" + id, "json");
    const exp = Math.max(now, l && l.exp || 0) + k.days * 86400000;   // gia hạn nối tiếp nếu còn hạn
    await env.LIC.put("l:" + id, JSON.stringify({ exp, days: k.days, key, note: k.note || (l && l.note) || "", at: now }));
    k.id = id; k.exp = exp; k.usedAt = now;
    await env.LIC.put("k:" + key, JSON.stringify(k));
    return json({ code: await issue(env, id, 2, exp), type: "paid", exp, now });
  }

  if (path === "/pub") return json({ jwk: (await keys(env)).pub });

  // Kho hải đồ trên Google Drive: 3 mục, mỗi mục gán một thư mục (đặt ở trang quản lý)
  if (path === "/charts") {
    const cfg = await driveCfg(env);
    return json({ cats: DRIVE_CATS.map(c => ({ key: c.key, name: cfg[c.key] && cfg[c.key].name || c.name, ready: !!(cfg[c.key] && cfg[c.key].folder) })) });
  }
  if (path === "/charts/list") {
    const cfg = await driveCfg(env), c = cfg[body.cat];
    if (!c || !c.folder) return json({ error: "Mục này chưa có hải đồ" }, 404);
    try { return json({ name: c.name, files: await addSizes(env, await listDrive(c.folder, 0)) }); }
    catch (e) { return json({ error: e.message }, 502); }
  }

  if (path === "/admin/api") {
    const a = body.action;
    const pw = await env.LIC.get("admin:pw", "json");
    if (a === "status") return json({ hasPw: !!pw });
    if (a === "setpw") {
      if (pw) return json({ error: "Đã có mật khẩu" }, 403);
      if (!body.newpw || String(body.newpw).length < 6) return json({ error: "Mật khẩu tối thiểu 6 ký tự" }, 400);
      const salt = b32(crypto.getRandomValues(new Uint8Array(10)));
      await env.LIC.put("admin:pw", JSON.stringify({ salt, h: await hashPw(String(body.newpw), salt) }));
      return json({ ok: true });
    }
    const failK = "afail:" + ip, fails = +(await env.LIC.get(failK) || 0);
    if (fails >= 10) return json({ error: "Sai mật khẩu quá nhiều lần, thử lại sau 30 phút" }, 429);
    if (!pw || await hashPw(String(body.pw || ""), pw.salt) !== pw.h) { await env.LIC.put(failK, String(fails + 1), { expirationTtl: 1800 }); return json({ error: "Sai mật khẩu" }, 401); }
    if (a === "login") return json({ ok: true });
    if (a === "chpw") {
      if (!body.newpw || String(body.newpw).length < 6) return json({ error: "Mật khẩu tối thiểu 6 ký tự" }, 400);
      const salt = b32(crypto.getRandomValues(new Uint8Array(10)));
      await env.LIC.put("admin:pw", JSON.stringify({ salt, h: await hashPw(String(body.newpw), salt) }));
      return json({ ok: true });
    }
    if (a === "newkey") {
      const days = Math.max(1, Math.min(3650, parseInt(body.days, 10) || 365));
      const forId = body.id ? String(body.id).toUpperCase().replace(/[^0-9A-Z]/g, "") : null;
      if (forId && !validId(forId)) return json({ error: "ID máy không hợp lệ (dạng AXXX-XXXX-XXXX)" }, 400);
      let key; do { key = randKey(); } while (await env.LIC.get("k:" + key));
      await env.LIC.put("k:" + key, JSON.stringify({ days, note: String(body.note || "").slice(0, 200), created: now, forId }));
      return json({ key, days });
    }
    if (a === "grant") {   // cấp thẳng giấy phép cho một ID máy (khách không có mạng): trả về mã dài
      const id = String(body.id || "").toUpperCase().replace(/[^0-9A-Z]/g, "");
      if (!validId(id)) return json({ error: "ID máy không hợp lệ (dạng AXXX-XXXX-XXXX)" }, 400);
      const days = Math.max(1, Math.min(3650, parseInt(body.days, 10) || 365));
      const l = await env.LIC.get("l:" + id, "json");
      const exp = Math.max(now, l && l.exp || 0) + days * 86400000;
      await env.LIC.put("l:" + id, JSON.stringify({ exp, days, key: "(cấp trực tiếp)", note: String(body.note || (l && l.note) || "").slice(0, 200), at: now }));
      return json({ code: await issue(env, id, 2, exp), exp });
    }
    if (a === "revoke") {
      const key = normKey(body.key); const k = key && await env.LIC.get("k:" + key, "json");
      if (!k) return json({ error: "Không có mã này" }, 404);
      if (k.id) return json({ error: "Mã đã kích hoạt, không thu hồi được" }, 409);
      k.revoked = true; await env.LIC.put("k:" + key, JSON.stringify(k)); return json({ ok: true });
    }
    if (a === "drivecfg") {
      if (body.set) {
        const cfg = {};
        for (const c of DRIVE_CATS) {
          const v = body.set[c.key] || {};
          const m = String(v.folder || "").match(/[-\w]{20,}/);
          cfg[c.key] = { name: String(v.name || c.name).slice(0, 60), folder: m ? m[0] : "" };
        }
        await env.LIC.put("cfg:drive", JSON.stringify(cfg));
      }
      const cfg = await driveCfg(env), out = {};
      for (const c of DRIVE_CATS) {
        out[c.key] = { ...cfg[c.key] };
        if (body.test && cfg[c.key].folder) { try { out[c.key].files = (await listDrive(cfg[c.key].folder, 0)).length; } catch (e) { out[c.key].err = e.message; } }
      }
      return json({ cfg: out });
    }
    if (a === "list") {
      const out = { keys: [], lic: [], trials: [] };
      for (const [pre, arr] of [["k:", out.keys], ["l:", out.lic], ["t:", out.trials]]) {
        let cursor;
        do {
          const r = await env.LIC.list({ prefix: pre, cursor, limit: 1000 });
          for (const k of r.keys) { const v = await env.LIC.get(k.name, "json"); if (v) arr.push({ name: k.name.slice(2), ...v }); }
          cursor = r.list_complete ? null : r.cursor;
        } while (cursor && arr.length < 3000);
      }
      return json(out);
    }
    return json({ error: "Không rõ thao tác" }, 400);
  }
  return json({ error: "Không có" }, 404);
}

const DRIVE_CATS = [{ key: "noidia", name: "Hải đồ nội địa" }, { key: "dna", name: "Hải đồ Đông Nam Á" }, { key: "chaua", name: "Hải đồ châu Á" }];
async function driveCfg(env) {
  const c = await env.LIC.get("cfg:drive", "json") || {};
  for (const d of DRIVE_CATS) c[d.key] = c[d.key] || { name: d.name, folder: "" };
  return c;
}
const unesc = s => s.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#(\d+);/g, (m, n) => String.fromCharCode(+n));
// Đọc danh sách tệp của thư mục Drive công khai (kể cả thư mục con 1 cấp)
async function listDrive(folder, depth) {
  const r = await fetch("https://drive.google.com/embeddedfolderview?id=" + encodeURIComponent(folder) + "&hl=vi", { headers: { "User-Agent": "Mozilla/5.0 HaiDoVN" } });
  const h = await r.text();
  if (!r.ok || h.indexOf("flip-entries") < 0) throw new Error("Không đọc được thư mục Drive (cần chia sẻ: Bất kỳ ai có đường liên kết)");
  const out = [], re = /<div class="flip-entry" id="entry-([-\w]+)"[\s\S]*?<a href="([^"]+)"[\s\S]*?<div class="flip-entry-title">([^<]*)<\/div>[\s\S]*?<div class="flip-entry-last-modified"><div>([^<]*)<\/div>/g;
  let m;
  while ((m = re.exec(h))) {
    const id = m[1], href = m[2], name = unesc(m[3]).trim(), mod = unesc(m[4]).trim();
    if (/\/folders\//.test(href)) { if (depth < 1) for (const f of await listDrive(id, depth + 1)) out.push({ ...f, dir: name }); continue; }
    if (/\.(mbtiles|kap|sqlitedb)$/i.test(name)) out.push({ id, name, mod });
  }
  return out;
}

// Kích thước từng tệp: hỏi Drive 1 byte đầu (Content-Range cho biết tổng), lưu đệm 7 ngày
async function fileSize(id) {
  const r = await fetch("https://drive.usercontent.google.com/download?id=" + encodeURIComponent(id) + "&export=download&confirm=t",
    { headers: { "Range": "bytes=0-0", "User-Agent": "Mozilla/5.0 HaiDoVN" }, redirect: "follow" });
  const ct = r.headers.get("content-type") || "", cr = r.headers.get("content-range") || "";
  try { r.body && r.body.cancel(); } catch (e) {}
  if (ct.startsWith("text/html")) return -1;
  const m = cr.match(/\/(\d+)/);
  if (m) return +m[1];
  const l = +(r.headers.get("content-length") || 0);
  return r.status === 200 && l > 1 ? l : -1;
}
async function addSizes(env, files) {
  let asked = 0;
  await Promise.all(files.map(async f => {
    const c = await env.LIC.get("sz:" + f.id);
    if (c != null) { f.size = +c; return; }
    if (asked++ >= 40) return;                     // giới hạn số lần hỏi trong một lượt
    try { f.size = await fileSize(f.id); } catch (e) { f.size = -1; }
    if (f.size > 0) await env.LIC.put("sz:" + f.id, String(f.size), { expirationTtl: 7 * 86400 });
  }));
  return files;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (url.pathname === "/" || url.pathname === "/admin") return new Response(ADMIN_HTML, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
    try { return await api(req, env, url.pathname); }
    catch (e) { return json({ error: "Lỗi máy chủ: " + e.message }, 500); }
  }
};

const ADMIN_HTML = `<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quản lý bản quyền – Hải đồ VN</title>
<style>body{font-family:system-ui,sans-serif;margin:0;background:#0e2233;color:#e8f1f7}main{max-width:760px;margin:auto;padding:16px}
h1{font-size:20px}h2{font-size:16px;margin-top:26px;border-bottom:1px solid #2a4a62;padding-bottom:6px}
input,select,button{font-size:16px;padding:10px;border-radius:8px;border:1px solid #2f5672;background:#0a1a27;color:#fff}
button{background:#1976d2;border:0;cursor:pointer}button.s{background:#2a4a62}.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.row>*{flex:1 1 160px}.out{background:#132d42;border-radius:8px;padding:12px;margin:10px 0;word-break:break-all;font-family:ui-monospace,monospace}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid #22405a;padding:6px 4px;text-align:left;vertical-align:top}
.ok{color:#7ee787}.bad{color:#ff8a80}.muted{color:#8fb3c9;font-size:13px}.hide{display:none}</style></head><body><main>
<h1>🔑 Quản lý bản quyền – Hải đồ VN</h1>
<div id="login" class="hide"><p id="lmsg" class="muted"></p><div class="row"><input id="pw" type="password" placeholder="Mật khẩu quản lý"><button onclick="login()">Vào</button></div></div>
<div id="app" class="hide">
<h2>Tạo mã kích hoạt cho khách</h2>
<div class="row"><select id="days"><option value="365">365 ngày (1 năm)</option><option value="730">730 ngày (2 năm)</option><option value="180">180 ngày</option><option value="90">90 ngày</option><option value="30">30 ngày</option></select>
<input id="note" placeholder="Ghi chú: tên khách, SĐT, tàu…"></div>
<div class="row"><input id="forid" placeholder="ID máy của khách (không bắt buộc)"><button onclick="newKey()">Tạo mã</button></div>
<p class="muted">Gửi mã ngắn HD-… cho khách qua Zalo. Khách mở app ▸ Bản quyền ▸ nhập mã (cần mạng 1 lần). Mã chỉ dùng được cho 1 máy; nhập mã mới khi còn hạn thì thời gian được cộng nối tiếp.</p>
<div id="kout"></div>
<h2>Cấp thẳng cho máy không có mạng</h2>
<div class="row"><input id="gid" placeholder="ID máy (AXXX-XXXX-XXXX)"><select id="gdays"><option value="365">365 ngày</option><option value="730">730 ngày</option><option value="180">180 ngày</option><option value="30">30 ngày</option></select><button onclick="grant()">Cấp mã dài</button></div>
<p class="muted">Tạo mã dài (dán vào app, không cần mạng).</p><div id="gout"></div>
<h2>Kho hải đồ trên Google Drive</h2>
<p class="muted">Dán đường liên kết thư mục Google Drive cho từng mục. Thư mục phải chia sẻ <b>"Bất kỳ ai có đường liên kết – Người xem"</b>. App sẽ tải tất cả tệp .mbtiles / .kap / .sqlitedb trong thư mục (kể cả thư mục con). Thêm/bớt tệp trong thư mục là app thấy ngay, không cần cập nhật app.</p>
<div id="drv"></div><div class="row"><button onclick="saveDrv()">Lưu</button><button class="s" onclick="loadDrv(true)">Kiểm tra thư mục</button></div>
<h2>Danh sách <button class="s" style="font-size:13px;padding:6px 10px" onclick="list()">Tải lại</button></h2>
<div id="lst" class="muted">…</div>
<h2>Đổi mật khẩu</h2><div class="row"><input id="npw" type="password" placeholder="Mật khẩu mới (≥ 6 ký tự)"><button class="s" onclick="chpw()">Đổi</button></div>
</div>
<script>
const $=id=>document.getElementById(id); let PW=sessionStorage.getItem('pw')||'';
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function call(action,o={}){ const r=await fetch('/admin/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,pw:PW,...o})}); return r.json(); }
const fmt=t=>t?new Date(t).toLocaleString('vi-VN',{hour12:false}):'';
const fid=s=>s&&s.length===12?s.slice(0,4)+'-'+s.slice(4,8)+'-'+s.slice(8):s;
async function start(){ const s=await call('status'); $('login').classList.remove('hide');
  if(!s.hasPw){ $('lmsg').textContent='Lần đầu: đặt mật khẩu quản lý (ít nhất 6 ký tự).'; $('pw').placeholder='Đặt mật khẩu mới'; window.first=true; }
  else if(PW){ const r=await call('login'); if(r.ok) return show(); } }
async function login(){ PW=$('pw').value; if(window.first){ const r=await call('setpw',{newpw:PW}); if(r.error) return alert(r.error); window.first=false; }
  const r=await call('login'); if(r.error){ $('lmsg').textContent=r.error; return; } sessionStorage.setItem('pw',PW); show(); }
function show(){ $('login').classList.add('hide'); $('app').classList.remove('hide'); list(); loadDrv(); }
const CATS=[['noidia','Hải đồ nội địa'],['dna','Hải đồ Đông Nam Á'],['chaua','Hải đồ châu Á']];
async function loadDrv(test){ const r=await call('drivecfg',{test:!!test}); if(r.error) return alert(r.error);
  $('drv').innerHTML=CATS.map(([k])=>{ const c=r.cfg[k]||{}; return '<div class="row"><input id="dn_'+k+'" value="'+esc(c.name)+'" style="flex:0 1 200px"><input id="df_'+k+'" placeholder="Link thư mục Google Drive" value="'+(c.folder?'https://drive.google.com/drive/folders/'+c.folder:'')+'"></div>'+(test&&c.folder?'<p class="'+(c.err?'bad':'ok')+'" style="margin:0 0 8px">'+(c.err?esc(c.err):'Thấy '+c.files+' tệp hải đồ')+'</p>':''); }).join(''); }
async function saveDrv(){ const set={}; CATS.forEach(([k])=>set[k]={name:$('dn_'+k).value,folder:$('df_'+k).value}); const r=await call('drivecfg',{set,test:true}); if(r.error) return alert(r.error); await loadDrv(true); }
async function newKey(){ const r=await call('newkey',{days:$('days').value,note:$('note').value,id:$('forid').value}); if(r.error) return alert(r.error);
  $('kout').innerHTML='<div class="out" style="font-size:22px">'+r.key+'</div><div class="row"><button class="s" onclick="navigator.clipboard.writeText(\\''+r.key+'\\')">Chép mã</button></div>'; $('note').value=''; $('forid').value=''; list(); }
async function grant(){ const r=await call('grant',{id:$('gid').value,days:$('gdays').value}); if(r.error) return alert(r.error);
  $('gout').innerHTML='<div class="out">'+r.code+'</div><p class="muted">Hết hạn: '+fmt(r.exp)+'</p><button class="s" onclick="navigator.clipboard.writeText(\\''+r.code+'\\')">Chép mã dài</button>'; list(); }
async function chpw(){ const r=await call('chpw',{newpw:$('npw').value}); if(r.error) return alert(r.error); PW=$('npw').value; sessionStorage.setItem('pw',PW); $('npw').value=''; alert('Đã đổi mật khẩu'); }
async function revoke(k){ if(!confirm('Thu hồi mã '+k+'?')) return; const r=await call('revoke',{key:k}); if(r.error) alert(r.error); list(); }
async function list(){ const r=await call('list'); if(r.error){ $('lst').textContent=r.error; return; } const now=Date.now();
  const lic={}; r.lic.forEach(l=>lic[l.name]=l);
  let h='<h3>Mã kích hoạt ('+r.keys.length+')</h3><table><tr><th>Mã</th><th>Số ngày</th><th>Ghi chú</th><th>Trạng thái</th></tr>';
  r.keys.sort((a,b)=>b.created-a.created).forEach(k=>{ h+='<tr><td>'+k.name+'</td><td>'+k.days+'</td><td>'+esc(k.note)+'</td><td>'+(k.revoked?'<span class="bad">đã thu hồi</span>':k.id?'<span class="ok">đã dùng</span> máy '+fid(k.id)+'<br>hết hạn '+fmt(k.exp):'chưa dùng'+(k.forId?' (cho '+fid(k.forId)+')':'')+' <a href="#" onclick="revoke(\\''+k.name+'\\');return false">thu hồi</a>')+'</td></tr>'; });
  h+='</table><h3>Máy có bản quyền ('+r.lic.length+')</h3><table><tr><th>ID máy</th><th>Hết hạn</th><th>Còn</th><th>Ghi chú</th></tr>';
  r.lic.sort((a,b)=>a.exp-b.exp).forEach(l=>{ const d=Math.ceil((l.exp-now)/86400000); h+='<tr><td>'+fid(l.name)+'</td><td>'+fmt(l.exp)+'</td><td class="'+(d>0?'ok':'bad')+'">'+(d>0?d+' ngày':'hết hạn')+'</td><td>'+esc(l.note)+'</td></tr>'; });
  h+='</table><h3>Máy dùng thử ('+r.trials.length+')</h3><table><tr><th>ID máy</th><th>Bắt đầu</th><th>Loại</th><th>Trạng thái</th></tr>';
  r.trials.sort((a,b)=>b.first-a.first).forEach(t=>{ const e=t.first+${TRIAL_DAYS}*86400000; h+='<tr><td>'+fid(t.name)+'</td><td>'+fmt(t.first)+'</td><td>'+esc(t.plat)+' '+esc(t.ver)+'</td><td>'+(lic[t.name]?'<span class="ok">đã mua</span>':e>now?'còn '+Math.ceil((e-now)/86400000)+' ngày':'<span class="bad">hết thử</span>')+'</td></tr>'; });
  $('lst').innerHTML=h+'</table>'; }
$('pw').addEventListener('keydown',e=>{ if(e.key==='Enter') login(); });
start();
</script></main></body></html>`;
