"""
Elevate CNC Controller - Web Edition
Lightweight browser-based G-code sender for FluidNC
Opens automatically at http://localhost:5001
Requires: pip install pyserial
"""

import http.server, socketserver, threading, json, time, queue, re, os
import serial, serial.tools.list_ports, webbrowser
from urllib.parse import urlparse, parse_qs

PORT = 5001

# ── Shared state ─────────────────────────────────────────────
ser         = None
connected   = False
polling     = True
file_lines  = []
file_index  = 0
file_sending= False
file_pause  = False
last_ok_time= time.time()
machine_pos = {"X":0.0,"Y":0.0,"Z":0.0,"A":0.0}
work_pos    = {"X":0.0,"Y":0.0,"Z":0.0,"A":0.0}
machine_state = "Disconnected"
rx_queue    = queue.Queue()
tx_queue    = queue.Queue()
sse_clients = []
sse_lock    = threading.Lock()

# ── SSE broadcast ─────────────────────────────────────────────
def broadcast(event, data):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try: q.put_nowait(msg)
            except: dead.append(q)
        for q in dead: sse_clients.remove(q)

def log(text, tag="recv"):
    broadcast("console", {"text": text, "tag": tag})

# ── Serial threads ────────────────────────────────────────────
def rx_thread():
    buf = ""
    while connected:
        try:
            data = ser.read(64).decode("utf-8", errors="replace")
            if data:
                buf += data
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    rx_queue.put(line.strip())
        except Exception:
            time.sleep(0.05)  # serial glitch — keep running instead of dying

def tx_thread():
    while connected:
        try:
            cmd = tx_queue.get(timeout=0.05)
            if ser and ser.is_open:
                ser.write((cmd + "\n").encode())
        except queue.Empty: pass
        except Exception:
            time.sleep(0.05)

def poll_thread():
    while True:
        time.sleep(1.0)
        if connected and polling and not file_sending:
            try:
                if ser and ser.is_open: ser.write(b"?")
            except: pass

def process_rx():
    while True:
        try:
            line = rx_queue.get(timeout=0.1)
            handle_rx(line)
        except queue.Empty: pass

def handle_rx(line):
    global last_ok_time, file_sending, machine_state
    if not line: return
    if line.startswith("<") and ">" in line:
        parse_status(line)
    elif line.lower().startswith("ok"):
        log("ok\n", "ok")
        last_ok_time = time.time()
        if file_sending and not file_pause:
            send_next_line()
    elif line.lower().startswith("error"):
        log(f"{line}\n", "error")
        if file_sending: stop_file_send()
    elif line.lower().startswith("alarm"):
        log(f"ALARM: {line}\n", "error")
        machine_state = f"ALARM"
        broadcast("status", get_status())
    else:
        log(f"{line}\n", "recv")

def parse_status(line):
    global machine_state, machine_pos, work_pos
    try:
        inner = line[1:line.index(">")]
        parts = inner.split("|")
        machine_state = parts[0] if parts else "?"
        for part in parts[1:]:
            if part.startswith("MPos:"):
                v = part[5:].split(",")
                for i,ax in enumerate(["X","Y","Z","A"]):
                    if i < len(v): machine_pos[ax] = float(v[i])
            elif part.startswith("WPos:"):
                v = part[5:].split(",")
                for i,ax in enumerate(["X","Y","Z","A"]):
                    if i < len(v): work_pos[ax] = float(v[i])
            elif part.startswith("WCO:"):
                v = part[4:].split(",")
                for i,ax in enumerate(["X","Y","Z","A"]):
                    if i < len(v):
                        work_pos[ax] = round(machine_pos[ax] - float(v[i]), 4)
        broadcast("status", get_status())
    except: pass

def get_status():
    return {"state": machine_state, "work": work_pos, "machine": machine_pos,
            "connected": connected}

# ── Commands ──────────────────────────────────────────────────
def send_cmd(cmd):
    if not connected: log("Not connected.\n","warn"); return
    log(f">> {cmd}\n","sent")
    tx_queue.put(cmd)

def send_raw(data):
    if ser and ser.is_open: ser.write(data.encode())

# ── File sender ───────────────────────────────────────────────
def send_next_line():
    global file_index, file_sending
    if not file_sending or file_pause: return
    if file_index >= len(file_lines):
        log("File send complete.\n","ok")
        file_sending = False
        broadcast("progress",{"index":file_index,"total":len(file_lines),"pct":100})
        return
    line = file_lines[file_index]
    line = re.sub(r'\(.*?\)','',line)
    line = re.sub(r';.*','',line).strip()
    if not line or line == "%":
        file_index += 1; send_next_line(); return
    line = re.sub(r'(\d+\.)(?=\s|[A-Za-z]|$)',r'\g<1>0',line)
    send_cmd(line)
    file_index += 1
    pct = file_index / len(file_lines) * 100
    broadcast("progress",{"index":file_index,"total":len(file_lines),"pct":pct})

def stop_file_send():
    global file_sending, file_pause
    file_sending = False; file_pause = False
    send_raw("!")
    log("File send stopped.\n","warn")

def watchdog_thread():
    global last_ok_time
    while True:
        time.sleep(1)
        if file_sending and not file_pause:
            elapsed = time.time() - last_ok_time
            if elapsed > 6:
                log(f"WARNING: No 'ok' in {elapsed:.0f}s — machine may be in alarm.\n","warn")
                last_ok_time = time.time()

# ── ESP32 port detection ──────────────────────────────────────
ESP32_VIDS = {0x10C4,0x1A86,0x0403,0x303A}
ESP32_KW   = ["cp210","ch340","ch341","ftdi","esp32","uart","usb serial"]

def score_port(p):
    s = 0
    desc = ((p.description or "")+" "+(p.manufacturer or "")).lower()
    if (p.vid or 0) in ESP32_VIDS: s += 10
    for kw in ESP32_KW:
        if kw in desc: s += 5
    return s

# ── HTTP Handler ──────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silence access log

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type","text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif path == "/api/ports":
            ports = list(serial.tools.list_ports.comports())
            scored = sorted(ports, key=score_port, reverse=True)
            data = [{"port":p.device,"desc":p.description,"score":score_port(p)}
                    for p in ports]
            best = scored[0].device if scored and score_port(scored[0])>0 else ""
            self._json({"ports":data,"best":best})
        elif path == "/api/status":
            self._json(get_status())
        elif path == "/api/stream":
            self._sse()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        global ser, connected, polling, file_lines, file_index
        global file_sending, file_pause, last_ok_time, machine_state
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length",0))
        body = self.rfile.read(length)
        try: data = json.loads(body) if body else {}
        except: data = {}

        if path == "/api/connect":
            port = data.get("port",""); baud = int(data.get("baud",115200))
            try:
                s = serial.Serial(port, baud, timeout=0.1)
            except Exception as e:
                self._json({"ok":False,"error":str(e)}); return
            ser = s
            connected = True; polling = True
            machine_state = "Connected"
            self._json({"ok":True})
            def _post_connect():
                time.sleep(2); ser.flushInput()
                log(f"Connected to {port} @ {baud} baud\n","ok")
                threading.Thread(target=rx_thread,daemon=True).start()
                threading.Thread(target=tx_thread,daemon=True).start()
                time.sleep(0.5); send_raw("\x18")
                time.sleep(1.5); send_cmd("$X")
                log("Auto-unlock sent.\n","warn")
                broadcast("status",get_status())
            threading.Thread(target=_post_connect,daemon=True).start()

        elif path == "/api/disconnect":
            connected = False; polling = False; file_sending = False
            if ser and ser.is_open: ser.close()
            machine_state = "Disconnected"
            log("Disconnected.\n","warn")
            broadcast("status",get_status())
            self._json({"ok":True})

        elif path == "/api/send":
            send_cmd(data.get("cmd",""))
            self._json({"ok":True})

        elif path == "/api/send_raw":
            send_raw(data.get("data",""))
            self._json({"ok":True})

        elif path == "/api/upload":
            # multipart — read raw and extract file content
            ct = self.headers.get("Content-Type","")
            boundary = ct.split("boundary=")[-1].encode()
            parts = body.split(b"--"+boundary)
            content = b""
            fname = "file"
            for part in parts:
                if b"filename=" in part:
                    fname_start = part.find(b"filename=\"")+10
                    fname_end   = part.find(b"\"",fname_start)
                    fname = part[fname_start:fname_end].decode(errors="replace")
                    header_end = part.find(b"\r\n\r\n")
                    if header_end >= 0:
                        content = part[header_end+4:].rstrip(b"\r\n")
            raw = content.decode("utf-8",errors="replace").splitlines()
            file_lines = [l.strip() for l in raw
                          if l.strip() and not l.strip().startswith(";")]
            file_index = 0; file_sending = False; file_pause = False
            log(f"Loaded: {fname} ({len(file_lines)} lines)\n","ok")
            broadcast("progress",{"index":0,"total":len(file_lines),"pct":0,
                                   "filename":fname})
            self._json({"ok":True,"lines":len(file_lines),"filename":fname})

        elif path == "/api/file_start":
            if not file_lines:
                self._json({"ok":False,"error":"No file loaded"}); return
            if not connected:
                self._json({"ok":False,"error":"Not connected"}); return
            file_sending = True; file_pause = False
            file_index = 0; last_ok_time = time.time()
            log(f"Starting file send ({len(file_lines)} lines)...\n","ok")
            send_cmd("$X")
            time.sleep(0.4)
            send_next_line()
            self._json({"ok":True})

        elif path == "/api/file_pause":
            file_pause = not file_pause
            log(f"File send {'Paused' if file_pause else 'Resumed'}.\n","warn")
            send_raw("!")
            if not file_pause:
                send_raw("~"); send_next_line()
            self._json({"ok":True,"paused":file_pause})

        elif path == "/api/file_stop":
            stop_file_send()
            self._json({"ok":True})

        elif path == "/api/diag":
            raw = []
            if ser and ser.is_open:
                ser.write(b"?\n")
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    try:
                        chunk = ser.read(64).decode("utf-8", errors="replace")
                        if chunk: raw.append(chunk)
                    except: break
                    time.sleep(0.05)
            self._json({"raw": raw, "connected": connected,
                        "rx_alive": ser is not None and ser.is_open if ser else False})

        else:
            self.send_response(404); self.end_headers()

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(body))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type","text/event-stream")
        self.send_header("Cache-Control","no-cache")
        self.send_header("Connection","keep-alive")
        self.end_headers()
        q = queue.Queue()
        with sse_lock: sse_clients.append(q)
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except: pass
        finally:
            with sse_lock:
                if q in sse_clients: sse_clients.remove(q)

# ── HTML UI ───────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Elevate CNC Controller</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#07090e;color:#d0dff0;font-family:'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
button{cursor:pointer;border:none;outline:none;font-family:inherit;border-radius:3px;transition:filter .1s}
button:hover{filter:brightness(1.2)}
button:active{filter:brightness(0.85)}
select,input{background:#18243a;color:#d0dff0;border:1px solid #1e3050;padding:4px 6px;border-radius:3px;font-family:inherit;font-size:13px}
/* TOP BAR */
#topbar{background:#111827;display:flex;align-items:center;padding:0 12px;height:52px;gap:10px;border-bottom:1px solid #1e3050;flex-shrink:0}
#logo{font-size:15px;font-weight:700;color:#4d8fff;margin-right:4px}
#logo span{color:#8aaac8;font-weight:400}
.sep{width:1px;background:#1e3050;height:30px;margin:0 4px}
.top-lbl{font-size:10px;font-weight:700;color:#3a5070;text-transform:uppercase}
#connect-btn{padding:8px 18px;font-size:12px;font-weight:700;background:#0d2a52;color:#4d8fff;border-radius:4px}
#connect-btn.on{background:#2a0a0a;color:#f44336}
#status-dot{font-size:13px;color:#3a5070}
.top-action{padding:6px 12px;font-size:11px;font-weight:700;border-radius:3px}
#btn-hold{background:#2e1800;color:#ff9800}
#btn-resume{background:#0a2244;color:#4d8fff}
#btn-clralarm{background:#2e1400;color:#ff7043}
#btn-reset{background:#2e0a0a;color:#f44336}
/* MAIN */
#main{display:flex;flex:1;overflow:hidden;padding:8px;gap:8px}
/* LEFT */
#left{width:400px;flex-shrink:0;display:flex;flex-direction:column;gap:6px;overflow-y:auto}
.sec-title{font-size:10px;font-weight:700;color:#3a5070;text-transform:uppercase;margin-bottom:4px;padding-bottom:4px;border-bottom:1px solid #1e3050}
/* DRO */
#dro{background:#0c1018;border-radius:4px;padding:8px}
.dro-row{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.dro-axis{font-size:13px;font-weight:700;width:24px;height:32px;display:flex;align-items:center;justify-content:center;border-radius:3px}
.dro-val{flex:1;background:#0a0a0a;font-family:'Consolas',monospace;font-size:15px;font-weight:700;color:#00e676;padding:6px 10px;border-radius:3px;text-align:right}
/* JOG */
#jog-panel{background:#0c1018;border-radius:4px;padding:8px}
#step-row{display:flex;gap:4px;align-items:center;margin-bottom:8px;flex-wrap:wrap}
.step-btn{padding:4px 8px;font-size:11px;font-weight:700;background:#18243a;color:#d0dff0;border-radius:3px}
.step-btn.active{background:#4d8fff;color:#000}
#feed-row{display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:11px;color:#3a5070;font-weight:700}
#feed-row input{width:80px}
#jog-grid{display:flex;gap:12px;align-items:flex-start}
#xy-pad{display:grid;grid-template-columns:repeat(3,60px);grid-template-rows:repeat(3,44px);gap:4px}
.jog-btn{font-size:16px;font-weight:700;background:#0e1e3c;color:#4d8fff;border-radius:4px}
.jog-btn.home-btn{background:#252500;color:#ffd740}
#za-pad{display:flex;flex-direction:column;gap:4px}
.za-btn{width:72px;height:40px;font-size:12px;font-weight:700;border-radius:4px}
.za-z{background:#0e1a30;color:#66aaff}
.za-a{background:#1e1400;color:#ffbb44}
/* MACHINE CONTROLS */
#machine{background:#0c1018;border-radius:4px;padding:8px}
.mc-row{display:flex;gap:4px;margin-bottom:4px}
.mc-btn{flex:1;padding:10px 4px;font-size:11px;font-weight:700;border-radius:3px}
.mc-blue{background:#0d1e38;color:#4d8fff}
.mc-green{background:#0e1e10;color:#00e676}
.mc-cyan{background:#0d1e38;color:#44ffcc}
.mc-silver{background:#14182e;color:#8aaac8}
/* RIGHT */
#right{flex:1;display:flex;flex-direction:column;gap:6px;min-width:0}
/* FILE */
#file-panel{background:#0c1018;border-radius:4px;padding:8px;flex-shrink:0}
#file-name{font-size:11px;color:#3a5070;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#prog-bar-outer{background:#18243a;border-radius:3px;height:14px;margin-bottom:4px;overflow:hidden}
#prog-bar-inner{background:#4d8fff;height:100%;width:0%;transition:width .3s;display:flex;align-items:center;justify-content:center;font-size:9px;color:#fff;font-weight:700}
#prog-text{font-size:10px;color:#3a5070;text-align:right;margin-bottom:6px}
#file-open-btn{width:100%;padding:11px;font-size:12px;font-weight:700;background:#18243a;color:#d0dff0;border-radius:3px;margin-bottom:6px}
#file-input{display:none}
#file-btns{display:flex;gap:4px}
#file-btns button{flex:1;padding:12px 4px;font-size:12px;font-weight:700;border-radius:3px}
#btn-send{background:#0a2244;color:#4d8fff}
#btn-pause{background:#2e1800;color:#ff9800}
#btn-stop{background:#2e0a0a;color:#f44336}
/* CONSOLE */
#console-wrap{flex:1;background:#0c1018;border-radius:4px;display:flex;flex-direction:column;min-height:0}
#console-hdr{display:flex;align-items:center;padding:4px 8px;background:#111827;border-radius:4px 4px 0 0;gap:4px}
#console-hdr span{font-size:10px;font-weight:700;color:#3a5070;flex:1}
.con-btn{padding:3px 10px;font-size:10px;background:#18243a;color:#3a5070;border-radius:3px}
#console{flex:1;background:#0a0a0a;font-family:'Consolas',monospace;font-size:11px;padding:8px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;min-height:0}
.t-sent{color:#4488ff}.t-recv{color:#00cc66}.t-warn{color:#ff9800}.t-error{color:#f44336}.t-ok{color:#00e676}
/* CMD BAR */
#cmd-bar{background:#111827;display:flex;align-items:center;padding:6px 10px;gap:8px;border-radius:4px;flex-shrink:0}
#cmd-bar span{color:#4d8fff;font-size:14px;font-family:Consolas}
#cmd-input{flex:1;background:transparent;border:none;color:#d0dff0;font-family:Consolas;font-size:13px;outline:none}
#cmd-send-btn{padding:6px 16px;font-size:11px;font-weight:700;background:#0d2050;color:#4d8fff;border-radius:3px}
</style>
</head>
<body>
<!-- TOP BAR -->
<div id="topbar">
  <div id="logo">ELEVATE <span>CNC</span> <span style="font-size:13px">CONTROLLER</span></div>
  <div class="sep"></div>
  <span class="top-lbl">PORT</span>
  <select id="port-sel" style="width:100px"></select>
  <button onclick="refreshPorts()" style="background:#18243a;color:#3a5070;padding:4px 7px;border-radius:3px">↻</button>
  <div class="sep"></div>
  <span class="top-lbl">BAUD</span>
  <select id="baud-sel" style="width:90px">
    <option>115200</option><option>250000</option><option>9600</option>
    <option>19200</option><option>38400</option><option>57600</option>
  </select>
  <div class="sep"></div>
  <button id="connect-btn" onclick="toggleConnect()">CONNECT</button>
  <div class="sep"></div>
  <span id="status-dot">● Disconnected</span>
  <div style="flex:1"></div>
  <button class="top-action" id="btn-hold" onclick="sendRaw('!')">⏸ HOLD</button>
  <button class="top-action" id="btn-resume" onclick="sendRaw('~')">▶ RESUME</button>
  <button class="top-action" id="btn-clralarm" onclick="sendCmd('$X')">🔓 CLR ALARM</button>
  <button class="top-action" id="btn-reset" onclick="softReset()">⚠ RESET</button>
</div>

<!-- MAIN -->
<div id="main">
  <!-- LEFT -->
  <div id="left">
    <!-- DRO -->
    <div id="dro">
      <div class="sec-title">POSITION ( WORK / MACHINE )</div>
      <div class="dro-row"><div class="dro-axis" style="background:#330000;color:#ff6666">X</div><div class="dro-val" id="dro-X">+0.000 / +0.000</div></div>
      <div class="dro-row"><div class="dro-axis" style="background:#003322;color:#44ddaa">Y</div><div class="dro-val" id="dro-Y">+0.000 / +0.000</div></div>
      <div class="dro-row"><div class="dro-axis" style="background:#001133;color:#66aaff">Z</div><div class="dro-val" id="dro-Z">+0.000 / +0.000</div></div>
      <div class="dro-row"><div class="dro-axis" style="background:#332200;color:#ffbb44">A</div><div class="dro-val" id="dro-A">+0.000 / +0.000</div></div>
    </div>
    <!-- JOG -->
    <div id="jog-panel">
      <div class="sec-title">JOG</div>
      <div id="step-row">
        <span style="font-size:10px;font-weight:700;color:#3a5070">STEP</span>
        <button class="step-btn" onclick="setStep('0.1')">0.1</button>
        <button class="step-btn" onclick="setStep('0.5')">0.5</button>
        <button class="step-btn active" id="step-1.0" onclick="setStep('1.0')">1.0</button>
        <button class="step-btn" onclick="setStep('5.0')">5.0</button>
        <button class="step-btn" onclick="setStep('10.0')">10.0</button>
        <button class="step-btn" onclick="setStep('50.0')">50.0</button>
      </div>
      <div id="feed-row">
        <span>FEED mm/min</span>
        <input type="number" id="jog-feed" value="2000">
      </div>
      <div id="jog-grid">
        <div id="xy-pad">
          <div></div>
          <button class="jog-btn" onclick="jog('Y',1)">▲</button>
          <div></div>
          <button class="jog-btn" onclick="jog('X',-1)">◀</button>
          <button class="jog-btn home-btn" onclick="sendCmd('G0 X0 Y0')">⌂</button>
          <button class="jog-btn" onclick="jog('X',1)">▶</button>
          <div></div>
          <button class="jog-btn" onclick="jog('Y',-1)">▼</button>
          <div></div>
        </div>
        <div id="za-pad">
          <button class="za-btn za-z" onclick="jog('Z',1)">Z ▲</button>
          <button class="za-btn za-z" onclick="jog('Z',-1)">Z ▼</button>
          <button class="za-btn za-a" onclick="jog('A',1)">A ▲</button>
          <button class="za-btn za-a" onclick="jog('A',-1)">A ▼</button>
        </div>
      </div>
    </div>
    <!-- MACHINE CONTROLS -->
    <div id="machine">
      <div class="sec-title">MACHINE CONTROLS</div>
      <div class="mc-row">
        <button class="mc-btn mc-blue" onclick="sendCmd('$H')">🏠 HOME ALL</button>
        <button class="mc-btn mc-blue" onclick="sendCmd('$HZ')">🏠 HOME Z</button>
      </div>
      <div class="mc-row">
        <button class="mc-btn mc-green" onclick="sendCmd('G10 L20 P0 X0')">ZERO X</button>
        <button class="mc-btn mc-green" onclick="sendCmd('G10 L20 P0 Y0')">ZERO Y</button>
      </div>
      <div class="mc-row">
        <button class="mc-btn mc-green" onclick="sendCmd('G10 L20 P0 Z0')">ZERO Z</button>
        <button class="mc-btn mc-green" onclick="sendCmd('G10 L20 P0 A0')">ZERO A</button>
      </div>
      <div class="mc-row">
        <button class="mc-btn mc-cyan" onclick="sendCmd('G10 L20 P0 X0 Y0 Z0 A0')">ZERO ALL</button>
        <button class="mc-btn mc-cyan" onclick="sendCmd('G0 X0 Y0')">GO WCS 0</button>
      </div>
      <div class="mc-row">
        <button class="mc-btn mc-silver" onclick="sendCmd('$X')">UNLOCK $X</button>
      </div>
    </div>
  </div>

  <!-- RIGHT -->
  <div id="right">
    <!-- FILE -->
    <div id="file-panel">
      <div class="sec-title">FILE SENDER</div>
      <div id="file-name">No file loaded</div>
      <div id="prog-bar-outer"><div id="prog-bar-inner">0%</div></div>
      <div id="prog-text">0 / 0 lines</div>
      <input type="file" id="file-input" accept=".nc,.gcode,.gc,.tap,.txt" onchange="uploadFile(this)">
      <button id="file-open-btn" onclick="document.getElementById('file-input').click()">📂  OPEN FILE</button>
      <div id="file-btns">
        <button id="btn-send" onclick="fileSend()">▶  SEND</button>
        <button id="btn-pause" onclick="filePause()">⏸  PAUSE</button>
        <button id="btn-stop" onclick="fileStop()">■  STOP</button>
      </div>
    </div>
    <!-- CONSOLE -->
    <div id="console-wrap">
      <div id="console-hdr">
        <span>CONSOLE</span>
        <button class="con-btn" onclick="sendRaw('?')">?</button>
        <button class="con-btn" onclick="sendCmd('$$')">$$</button>
        <button class="con-btn" onclick="sendCmd('$I')">$I</button>
        <button class="con-btn" onclick="diagTest()" style="color:#ffbb44">DIAG</button>
        <button class="con-btn" onclick="clearConsole()">CLEAR</button>
      </div>
      <div id="console"></div>
    </div>
    <!-- CMD BAR -->
    <div id="cmd-bar">
      <span>▷</span>
      <input id="cmd-input" placeholder="Enter G-code command..." onkeydown="if(event.key==='Enter')sendManual()">
      <button id="cmd-send-btn" onclick="sendManual()">SEND</button>
    </div>
  </div>
</div>

<script>
let step = '1.0';
let isConnected = false;

// ── SSE ──────────────────────────────────────────────────────
function startSSE() {
  const es = new EventSource('/api/stream');
  es.addEventListener('console', e => {
    const d = JSON.parse(e.data);
    appendConsole(d.text, d.tag);
  });
  es.addEventListener('status', e => {
    const d = JSON.parse(e.data);
    updateStatus(d);
  });
  es.addEventListener('progress', e => {
    const d = JSON.parse(e.data);
    updateProgress(d);
  });
  es.onerror = () => setTimeout(startSSE, 2000);
}

// ── Console ───────────────────────────────────────────────────
function appendConsole(text, tag) {
  const el = document.getElementById('console');
  const span = document.createElement('span');
  span.className = 't-' + tag;
  span.textContent = text;
  el.appendChild(span);
  el.scrollTop = el.scrollHeight;
}
function clearConsole() { document.getElementById('console').innerHTML = ''; }

// ── Status ────────────────────────────────────────────────────
function updateStatus(d) {
  const dot = document.getElementById('status-dot');
  const state = d.state || '';
  let color = '#3a5070';
  if (state.includes('Idle')) color = '#00e676';
  else if (state.includes('Run')) color = '#ff9800';
  else if (state.includes('Alarm') || state.includes('ALARM')) color = '#f44336';
  else if (d.connected) color = '#4d8fff';
  dot.textContent = '● ' + state;
  dot.style.color = color;

  for (const ax of ['X','Y','Z','A']) {
    const w = (d.work[ax] >= 0 ? '+' : '') + d.work[ax].toFixed(3);
    const m = (d.machine[ax] >= 0 ? '+' : '') + d.machine[ax].toFixed(3);
    document.getElementById('dro-'+ax).textContent = w + '   /   ' + m;
  }
}

// ── Progress ──────────────────────────────────────────────────
function updateProgress(d) {
  const pct = Math.round(d.pct);
  document.getElementById('prog-bar-inner').style.width = pct + '%';
  document.getElementById('prog-bar-inner').textContent = pct + '%';
  document.getElementById('prog-text').textContent =
    (d.index||0) + ' / ' + (d.total||0) + ' lines';
  if (d.filename) document.getElementById('file-name').textContent = d.filename;
}

// ── Ports ─────────────────────────────────────────────────────
async function refreshPorts() {
  const r = await fetch('/api/ports');
  const d = await r.json();
  const sel = document.getElementById('port-sel');
  sel.innerHTML = '';
  for (const p of d.ports) {
    const opt = document.createElement('option');
    opt.value = p.port;
    opt.textContent = p.port;
    sel.appendChild(opt);
  }
  if (d.best) sel.value = d.best;
}

// ── Connect ───────────────────────────────────────────────────
async function toggleConnect() {
  if (isConnected) {
    await fetch('/api/disconnect', {method:'POST'});
    isConnected = false;
    document.getElementById('connect-btn').textContent = 'CONNECT';
    document.getElementById('connect-btn').classList.remove('on');
    document.getElementById('status-dot').textContent = '● Disconnected';
    document.getElementById('status-dot').style.color = '#3a5070';
  } else {
    const port = document.getElementById('port-sel').value;
    const baud = document.getElementById('baud-sel').value;
    document.getElementById('status-dot').textContent = '● Connecting...';
    document.getElementById('status-dot').style.color = '#ff9800';
    const r = await fetch('/api/connect',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({port,baud})});
    const d = await r.json();
    if (d.ok) {
      isConnected = true;
      document.getElementById('connect-btn').textContent = 'DISCONNECT';
      document.getElementById('connect-btn').classList.add('on');
    } else {
      alert('Connection failed: ' + d.error);
      document.getElementById('status-dot').textContent = '● Disconnected';
      document.getElementById('status-dot').style.color = '#3a5070';
    }
  }
}

// ── Commands ──────────────────────────────────────────────────
async function sendCmd(cmd) {
  await fetch('/api/send',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cmd})});
}
async function sendRaw(data) {
  await fetch('/api/send_raw',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({data})});
}
async function softReset() {
  if (confirm('Send soft reset?'))
    await fetch('/api/send_raw',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({data:'\x18'})});
}
function sendManual() {
  const inp = document.getElementById('cmd-input');
  const cmd = inp.value.trim();
  if (cmd) { sendCmd(cmd); inp.value = ''; }
}

// ── Jog ───────────────────────────────────────────────────────
function setStep(val) {
  step = val;
  document.querySelectorAll('.step-btn').forEach(b => {
    b.classList.toggle('active', b.textContent === val);
  });
}
function jog(axis, dir) {
  const feed = document.getElementById('jog-feed').value || '2000';
  const dist = (parseFloat(step) * dir).toFixed(4);
  sendCmd(`$J=G21 G91 ${axis}${dist} F${feed}`);
}

// ── File ──────────────────────────────────────────────────────
async function uploadFile(input) {
  if (!input.files.length) return;
  const fd = new FormData();
  fd.append('file', input.files[0]);
  await fetch('/api/upload',{method:'POST',body:fd});
}
async function fileSend() {
  const r = await fetch('/api/file_start',{method:'POST'});
  const d = await r.json();
  if (!d.ok) alert(d.error);
}
async function filePause() { await fetch('/api/file_pause',{method:'POST'}); }
async function fileStop()  { await fetch('/api/file_stop',{method:'POST'}); }

// ── Diagnostics ───────────────────────────────────────────────
async function diagTest() {
  appendConsole('--- DIAG: sending ? and reading raw for 2s ---\n', 'warn');
  const r = await fetch('/api/diag', {method:'POST'});
  const d = await r.json();
  appendConsole('Raw received: ' + JSON.stringify(d.raw) + '\n', 'warn');
  appendConsole('rx_alive: ' + d.rx_alive + '  connected: ' + d.connected + '\n', 'warn');
}

// ── Init ──────────────────────────────────────────────────────
window.onload = async () => {
  await refreshPorts();
  startSSE();
  // Sync connected state from server before auto-connecting
  const st = await fetch('/api/status').then(r=>r.json());
  if (st.connected) {
    isConnected = true;
    document.getElementById('connect-btn').textContent = 'DISCONNECT';
    document.getElementById('connect-btn').classList.add('on');
  } else {
    const sel = document.getElementById('port-sel');
    if (sel.value) await toggleConnect();
  }
};
</script>
</body>
</html>"""

# ── Start server ──────────────────────────────────────────────
class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    threading.Thread(target=process_rx, daemon=True).start()
    threading.Thread(target=poll_thread, daemon=True).start()
    threading.Thread(target=watchdog_thread, daemon=True).start()

    server = ThreadedServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Elevate CNC Controller running at {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
