/* Hải đồ VN – cầu nối giao diện với phần lõi Windows (thay cho window.Android trên điện thoại) */
(function(){
  const sx=(m,u,body)=>{ const x=new XMLHttpRequest(); x.open(m,u,false); if(body!=null) x.setRequestHeader('Content-Type','text/plain;charset=utf-8'); x.send(body==null?null:body); return x; };
  const post=(u,body)=>fetch(u,{method:'POST',headers:{'Content-Type':'text/plain;charset=utf-8'},body:body==null?'':body}).then(r=>r.text()).catch(()=>'');
  let ac=null, alarmT=0;
  function tone(f,ms){ try{ ac=ac||new AudioContext(); const o=ac.createOscillator(), g=ac.createGain(); o.type='square'; o.frequency.value=f; g.gain.value=0.12; o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime+ms/1000); }catch(e){} }
  window.Android={
    load:k=>{ try{ const x=sx('GET','/api/load?k='+encodeURIComponent(k)); return x.status===200?x.responseText:null; }catch(e){ return null; } },
    save:(k,v)=>{ try{ sx('POST','/api/save?k='+encodeURIComponent(k),v); }catch(e){} },
    listCharts:()=>{ try{ const x=sx('GET','/api/charts'); return x.status===200?x.responseText:'[]'; }catch(e){ return '[]'; } },
    pickMbtiles:()=>{ post('/api/pick_mbtiles'); },
    deleteChart:id=>{ try{ sx('POST','/api/delete_chart?id='+encodeURIComponent(id),''); }catch(e){} },
    importGpx:()=>{ post('/api/import_gpx').then(t=>{ if(t&&window.onGpxImported) onGpxImported(t); }); },
    exportGpx:(n,c)=>{ post('/api/export_gpx?name='+encodeURIComponent(n),c).then(t=>{ if(t&&window.onToast) onToast(t); }); },
    alarm:on=>{ clearInterval(alarmT); if(on){ const f=()=>{ tone(880,350); setTimeout(()=>tone(660,350),420); }; f(); alarmT=setInterval(f,1300); } },
    beep:()=>tone(1000,300),
    aisConnect:(h,p,pr)=>{ post(`/api/ais_connect?host=${encodeURIComponent(h)}&port=${encodeURIComponent(p)}&proto=${encodeURIComponent(pr)}`); },
    aisDisconnect:()=>{ post('/api/ais_disconnect'); },
    connOpen:j=>{ post('/api/conn_open',j); },
    connClose:id=>{ post('/api/conn_close?id='+encodeURIComponent(id)); },
    connSend:(id,t)=>{ post('/api/conn_send?id='+encodeURIComponent(id),t); },
    openUrl:u=>{ post('/api/open_url',u); },
    listSerial:()=>{ try{ const x=sx('GET','/api/serial_ports'); return x.status===200?x.responseText:'[]'; }catch(e){ return '[]'; } },
    driveDownload:j=>{ post('/api/drive_download',j); },
    driveCancel:()=>{ post('/api/drive_cancel'); },
    driveState:()=>{ try{ const x=sx('GET','/api/drive_state'); return x.status===200?x.responseText:'{}'; }catch(e){ return '{}'; } },
    freeSpace:()=>{ try{ const x=sx('GET','/api/freespace'); return x.status===200?+x.responseText:-1; }catch(e){ return -1; } },
    splitMbtiles:mb=>{ post('/api/split?mb='+(mb|0)); },
    splitCancel:()=>{ post('/api/split_cancel'); },
    deviceId:()=>{ try{ const x=sx('GET','/api/deviceid'); return x.status===200?x.responseText:''; }catch(e){ return ''; } },
    ready:()=>{}
  };
  // sự kiện từ phần lõi: NMEA, trạng thái kết nối, thông báo, hải đồ thay đổi
  const early=[]; const run=m=>{ try{ const f=window[m.fn]; if(typeof f==='function') f.apply(null,m.args||[]); }catch(_){} };
  window.addEventListener('load',()=>{ setTimeout(()=>{ while(early.length) run(early.shift()); },300); });
  function listen(){ const es=new EventSource('/api/events');
    es.onmessage=e=>{ let m; try{ m=JSON.parse(e.data); }catch(_){ return; }
      if(document.readyState!=='complete'){ early.push(m); return; } run(m); };
    es.onerror=()=>{ es.close(); setTimeout(listen,3000); }; }
  listen();
  // GPS của máy tính (nếu Windows có dịch vụ vị trí) – GPS tàu qua Wi-Fi/COM vẫn được ưu tiên ở chế độ Tự động
  window.addEventListener('load',()=>{ try{ if(navigator.geolocation) navigator.geolocation.watchPosition(p=>{ if(window.onGps) onGps(p.coords.latitude,p.coords.longitude,p.coords.speed==null?-1:p.coords.speed,(p.coords.heading==null||isNaN(p.coords.heading))?-1:p.coords.heading,p.coords.accuracy,p.timestamp); },()=>{},{enableHighAccuracy:true}); }catch(e){} });
  // phím tắt
  window.addEventListener('keydown',e=>{ const t=e.target&&e.target.tagName; if(t==='INPUT'||t==='SELECT'||t==='TEXTAREA'){ if(e.key==='Escape'&&window.onBack) onBack(); return; }
    if(e.key==='Escape'&&window.onBack) onBack();
    else if((e.key==='+'||e.key==='=')&&window.zoomBy) zoomBy(1);
    else if(e.key==='-'&&window.zoomBy) zoomBy(-1);
    else if((e.key==='f'||e.key==='F')&&window.toggleFollow) toggleFollow(); });
})();
