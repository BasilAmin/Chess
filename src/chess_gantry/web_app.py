from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import os
import threading
from typing import Any, Mapping, Optional
import webbrowser

from .clerk_auth import ClerkSettings, ClerkVerifier, render_dashboard
from .clerk_auth import SESSION_COOKIE as CLERK_SESSION_COOKIE
from .board_sensor import SyntheticBoardSensor
from .config import AppConfig
from .controller import GantryController
from .errors import GantryError, ValidationError
from .live_game import LiveGameManager
from .operations import OperationManager, operation_catalog
from .reed_switch import MCP23017ReedSwitch, SimulatedReedSwitch
from .service import GantryService
from .vision import VisionManager


def _lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    return "127.0.0.1"


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chess Gantry Controller</title>
<style>
:root{color-scheme:dark;--bg:#0b0e14;--card:#151a24;--card2:#1d2431;--line:#30394a;--text:#eef3ff;--muted:#9eabc2;--accent:#67e8b5;--danger:#ff6b7a;--warn:#f4c66d;font-family:Inter,system-ui,sans-serif}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#16213b,transparent 32rem),var(--bg);color:var(--text)}main{width:min(1120px,calc(100% - 30px));margin:auto;padding:34px 0 60px}header{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}h1{margin:0;font-size:clamp(2rem,5vw,3.2rem);letter-spacing:-.045em}h2{font-size:1.05rem;margin:0 0 16px}p{color:var(--muted);line-height:1.55}.subtitle{max-width:720px;margin:8px 0 0}.pill{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:9px 12px;white-space:nowrap;color:var(--muted)}.pill.good{color:var(--accent)}.pill.bad{color:var(--danger)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{border:1px solid var(--line);border-radius:17px;padding:20px;background:rgba(21,26,36,.96);box-shadow:0 20px 55px rgba(0,0,0,.18)}.wide{grid-column:1/-1}.fields{display:grid;grid-template-columns:1fr 1fr;gap:11px}.three{grid-template-columns:1fr 1fr 1fr}label{display:block;font-size:.8rem;color:var(--muted);margin-bottom:6px}input,select,textarea{width:100%;border:1px solid var(--line);border-radius:10px;background:var(--card2);color:var(--text);padding:11px 12px;font:inherit;outline:none}textarea{min-height:190px;resize:vertical;font:13px/1.5 ui-monospace,monospace}input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(103,232,181,.12)}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:14px}button{border:1px solid var(--line);border-radius:10px;background:var(--card2);color:var(--text);padding:10px 14px;font-weight:700;cursor:pointer}button:hover:not(:disabled){border-color:#64718f;transform:translateY(-1px)}button:disabled{opacity:.38;cursor:not-allowed}.primary{background:var(--accent);border-color:var(--accent);color:#07130f}.danger{background:#421b24;border-color:#7d3040;color:#ffdce2}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}.metric{border:1px solid var(--line);background:var(--card2);border-radius:11px;padding:12px}.metric span{display:block;color:var(--muted);font-size:.74rem;margin-bottom:4px}.metric strong{font-size:1.05rem}.notice{border-left:3px solid var(--warn);background:#292318;color:#f2dba9;padding:10px 12px;margin-top:14px;font-size:.84rem;line-height:1.45}.safe{border-left-color:var(--accent);background:#162a24;color:#c9f7e4}pre{margin:0;min-height:130px;max-height:310px;overflow:auto;border:1px solid var(--line);border-radius:11px;background:#080b10;padding:12px;color:#bcc7dc;white-space:pre-wrap;font:12px/1.55 ui-monospace,monospace}.split{display:grid;grid-template-columns:1fr 1fr;gap:12px}.small{font-size:.8rem;color:var(--muted)}.check{display:flex;gap:8px;align-items:center;margin-top:12px;color:var(--muted);font-size:.85rem}.check input{width:auto}.locked{color:var(--danger)}
@media(max-width:780px){header{display:block}.pill{display:inline-block;margin-top:14px}.grid,.split{grid-template-columns:1fr}.wide{grid-column:auto}.three{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr}}
.ops{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.op-category{grid-column:1/-1;margin:15px 0 1px;padding-top:12px;border-top:1px solid var(--line);font-size:.82rem;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}.op-category:first-child{margin-top:0;border-top:0;padding-top:0}.op{border:1px solid var(--line);border-radius:13px;padding:14px;background:var(--card2)}.op h3{margin:0 0 7px;font-size:.95rem}.op p{font-size:.8rem;margin:0;min-height:42px}.op .tag{display:inline-block;font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:8px}.op.physical{border-color:#664c28}.op.physical .tag{color:var(--warn)}.taskbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.tasklog{height:280px}.position-box{border:1px solid #426858;background:#101d1a;border-radius:14px;padding:15px;margin-top:14px}.position-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}.position-value{font:700 1.35rem/1.1 ui-monospace,monospace;color:var(--accent)}.jog-layout{display:grid;grid-template-columns:180px 1fr;gap:18px;align-items:center}.jog-pad{display:grid;grid-template-columns:repeat(3,52px);grid-template-rows:repeat(3,52px);gap:6px;justify-content:center}.jog-pad button{font-size:1.35rem;padding:0}.jog-up{grid-column:2}.jog-left{grid-column:1;grid-row:2}.jog-home{grid-column:2;grid-row:2}.jog-right{grid-column:3;grid-row:2}.jog-down{grid-column:2;grid-row:3}.live-game{border-color:#5f4d90;background:linear-gradient(135deg,rgba(68,45,112,.45),rgba(21,26,36,.96))}.live-status{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:12px 0}.live-status>div{padding:10px;border:1px solid var(--line);border-radius:10px;background:rgba(0,0,0,.18)}.reed-state{font:800 2rem/1 ui-monospace,monospace;color:var(--muted);margin:10px 0}.reed-state.closed{color:var(--accent)}.reed-state.error{color:var(--danger)}.sensor-card{border-color:#356985;background:linear-gradient(135deg,rgba(24,69,92,.42),rgba(21,26,36,.98))}.sensor-layout{display:grid;grid-template-columns:minmax(300px,480px) 1fr;gap:20px}.sensor-board{display:grid;grid-template-columns:repeat(8,1fr);aspect-ratio:1;max-width:480px;border:2px solid #7797a8}.sensor-cell{border:0;border-radius:0;padding:0;min-width:0;font:700 clamp(.76rem,2.2vw,1.25rem)/1 ui-monospace,monospace;position:relative}.sensor-cell.light{background:#b9cad1;color:#102027}.sensor-cell.dark{background:#547285;color:#f2fbff}.sensor-cell.occupied::after{content:'';position:absolute;inset:16%;border-radius:50%;border:3px solid currentColor}.sensor-cell.changed{box-shadow:inset 0 0 0 4px var(--warn)}.sensor-rank{position:absolute;left:3px;top:3px;font-size:.55rem}.sensor-file{position:absolute;right:3px;bottom:3px;font-size:.55rem}.sensor-status{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px}.sensor-status>div{padding:9px;border:1px solid var(--line);border-radius:9px;background:rgba(0,0,0,.16)}.sensor-events{height:190px}.piece-list{max-height:190px;overflow:auto;display:grid;grid-template-columns:repeat(2,1fr);gap:4px;font:12px ui-monospace,monospace}.write-row{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end}.switch{display:flex;align-items:center;gap:8px;color:var(--text);margin:0}.switch input{width:auto}@media(max-width:900px){.ops{grid-template-columns:1fr 1fr}.sensor-layout{grid-template-columns:1fr}}@media(max-width:700px){.jog-layout{grid-template-columns:1fr}.position-grid{grid-template-columns:1fr 1fr}}@media(max-width:600px){.ops{grid-template-columns:1fr}.sensor-status{grid-template-columns:1fr 1fr}}
.vision-card{border-color:#6b508d;background:linear-gradient(135deg,rgba(74,46,108,.48),rgba(21,26,36,.96))}.vision-preview{width:100%;aspect-ratio:16/9;object-fit:contain;border:1px solid var(--line);border-radius:12px;background:#080a0f}.vision-list{max-height:260px}.vision-error{color:var(--danger)}
</style>
</head>
<body><main>
<header><div><h1>Chess Gantry Controller</h1><p class="subtitle">One local interface for serial diagnostics, coupled outer X/Y motion, independent inner Z motion, and validated chess move JSON.</p></div><div id="pill" class="pill">Starting…</div></header>
<section class="grid">
<div class="card"><h2>1. Serial connection</h2><div class="fields"><div><label for="port">USB port</label><select id="port"><option value="">Auto-detect</option></select></div><div><label for="baud">Baud</label><select id="baud"><option value="">Auto / config</option><option>115200</option><option>250000</option></select></div></div><div class="actions"><button id="connect" class="primary">Connect</button><button id="refresh">Refresh ports</button><button id="disconnect">Disconnect</button></div><p id="firmware" class="small">No controller identified.</p></div>
<div class="card"><h2>2. Machine state</h2><div class="metrics"><div class="metric"><span>Logical inner</span><strong id="xread">—</strong></div><div class="metric"><span>Logical outer</span><strong id="yread">—</strong></div><div class="metric"><span>Initialized</span><strong id="homed">No</strong></div><div class="metric"><span>Board rev.</span><strong id="revision">—</strong></div></div><div class="position-box"><label>Live Marlin position</label><div class="position-grid"><div><span class="small">Outer X</span><div id="machineX" class="position-value">—</div></div><div><span class="small">Outer Y</span><div id="machineY" class="position-value">—</div></div><div><span class="small">Inner Z</span><div id="machineZ" class="position-value">—</div></div></div><p id="positionAge" class="small">Connect to read M114.</p></div><div class="actions"><button id="endstops">Check endstops</button><button id="home" class="primary">Home XYZ gantry</button><button id="stop" class="danger">Emergency stop</button></div><div class="notice">Physical X/Y align independently on their endstops. The inner motor is plugged into E but firmware maps it as logical Z and homes it against the Z switch.</div></div>
<div class="card"><h2>3. MCP23017 reed switch</h2><p class="small">Live wiring test for I2C bus 1, address 0x20, pin GPB0. The input uses the MCP23017 pull-up and closes to ground.</p><div id="reedState" class="reed-state">UNKNOWN</div><div class="metrics"><div class="metric"><span>Pin</span><strong>GPB0</strong></div><div class="metric"><span>I2C</span><strong id="reedAddress">1 / 0x20</strong></div><div class="metric"><span>Raw</span><strong id="reedRaw">—</strong></div><div class="metric"><span>Transitions</span><strong id="reedTransitions">0</strong></div></div><div class="actions"><button id="reedRefresh" class="primary">Read reed now</button></div><pre id="reedLog">Waiting for the first I2C read.</pre></div>
<div class="card wide"><h2>4. Keyboard jog and manual coordinates</h2><div class="jog-layout"><div class="jog-pad"><button id="jogUp" class="jog-up" title="Arrow Up">↑</button><button id="jogLeft" class="jog-left" title="Arrow Left">←</button><button id="jogHome" class="jog-home" title="Home first">⌂</button><button id="jogRight" class="jog-right" title="Arrow Right">→</button><button id="jogDown" class="jog-down" title="Arrow Down">↓</button></div><div><div class="fields"><div><label for="jogStep">Jog step (mm)</label><select id="jogStep"><option>0.5</option><option selected>1</option><option>5</option><option>10</option></select></div><div><label for="jogFeed">Jog feed (mm/min)</label><input id="jogFeed" type="number" step="10" value="600"></div></div><p id="keyboardState" class="small">Arrow Left/Right move the inner gantry; Up/Down move the outer gantry.</p></div></div><hr><div class="fields three"><div><label for="xmm">Logical inner (mm)</label><input id="xmm" type="number" step="0.1" value="10"></div><div><label for="ymm">Logical outer (mm)</label><input id="ymm" type="number" step="0.1" value="10"></div><div><label for="feed">Feed (mm/min)</label><input id="feed" type="number" step="10" value="600"></div></div><div class="actions"><button id="move" class="primary">Move to coordinate</button><button id="refreshPosition">Read M114 now</button></div><p id="limits" class="small">Workspace loading…</p></div>
<div class="card wide"><h2>4. Chess move JSON</h2><div class="split"><div><label for="movejson">Incoming move</label><textarea id="movejson">{
  "event_id": "web-test-001",
  "position": "white_pawn_e",
  "px": 4,
  "py": 1,
  "nx": 4,
  "ny": 3
}</textarea><div class="actions"><button id="plan" class="primary">Plan only</button><button id="execute">Execute move</button></div><p id="lockState" class="small locked"></p></div><div><label>Plan / generated G-code</label><pre id="planout">No plan yet.</pre></div></div></div>
<div class="card wide live-game"><div class="taskbar"><div><h2>5. Live Lichess TV game</h2><p class="small">Enter a new public game ID before White's first move. The server creates fresh standard state, homes once, and streams every new Lichess move to this computer's serial port.</p></div><button id="liveStop" class="danger" disabled>Stop live game</button></div><div class="fields"><div><label for="liveGameId">Lichess game ID</label><input id="liveGameId" maxlength="12" placeholder="6RkOwfp1"></div><div><label>Session</label><p class="small">Starting live play assumes the physical board is in the standard position.</p></div></div><div class="actions"><button id="liveStart" class="primary">Start immediate live play</button></div><div class="live-status"><div><span class="small">State</span><strong id="liveState">Idle</strong></div><div><span class="small">Executed</span><strong id="liveCount">0</strong></div><div><span class="small">Last event</span><strong id="liveLast">—</strong></div></div><pre id="liveLog">No live game started in this server session.</pre><p class="small">Nearest-home square is h1 on White's side. Start is rejected if the Lichess game already contains moves. Captures stop the follower while physical capture storage is disabled.</p></div>
<div class="card wide sensor-card"><div class="taskbar"><div><h2>6. Synthetic 8 x 8 sensor lab</h2><p class="small">Server-side occupancy sampling runs at 300 Hz. Click cells or load a dataset to imitate hand-moved pieces. Legal moves update tracked piece identities and can optionally be submitted to Lichess.</p></div><button id="sensorReset">Reset standard board</button></div><div class="sensor-layout"><div><div id="sensorBoard" class="sensor-board"></div><div class="actions"><select id="sensorDataset"></select><button id="sensorRunDataset" class="primary">Run dataset</button><button id="sensorRetry">Retry move write</button></div></div><div><div class="sensor-status"><div><span class="small">Detector</span><strong id="sensorState">ready</strong></div><div><span class="small">Turn</span><strong id="sensorTurn">white</strong></div><div><span class="small">Last move</span><strong id="sensorMove">—</strong></div><div><span class="small">Sampling</span><strong id="sensorRate">300 Hz</strong></div></div><div class="write-row"><div><label for="sensorGameId">Lichess Board API game ID</label><input id="sensorGameId" maxlength="12" placeholder="optional"></div><label class="switch"><input id="sensorWrite" type="checkbox"> Write inferred moves</label></div><div class="actions"><button id="sensorSaveWrite">Save Lichess settings</button><button id="sensorApplyMatrix">Apply matrix JSON</button></div><textarea id="sensorMatrixInput" spellcheck="false"></textarea><label>Tracked pieces</label><div id="sensorPieces" class="piece-list"></div><label>Sensor events</label><pre id="sensorEvents" class="sensor-events">No sensor events yet.</pre><p id="sensorError" class="small"></p></div></div></div>
<div class="card wide vision-card"><div class="taskbar"><div><h2>7. Overhead exact-piece vision</h2><p class="small">Unique ArUco MIP tags identify every piece. Four fixed references register one top-down camera to the board. Stable legal positions are accepted; occlusion, ambiguity, illegal edits, and sensor disagreement are rejected.</p></div><button id="visionReset">Reset tracker</button></div><div class="sensor-layout"><div><img id="visionPreview" class="vision-preview" alt="Authenticated overhead camera preview"><p class="small">Keep all four board references and every piece cap visible. The preview endpoint requires Clerk authentication.</p><label>Last exact move transition</label><pre id="visionMoveDetail">No accepted vision move yet.</pre></div><div><div class="sensor-status"><div><span class="small">Detector</span><strong id="visionState">disabled</strong></div><div><span class="small">Turn</span><strong id="visionTurn">white</strong></div><div><span class="small">Last move</span><strong id="visionMove">—</strong></div><div><span class="small">Pieces</span><strong id="visionPiecesCount">0 / 32</strong></div></div><div class="fields"><div><label for="visionSource">Camera source</label><input id="visionSource" placeholder="demo, /dev/video0, stream URL, or snapshot:URL"></div><div><label for="visionGameId">Lichess Board API game ID</label><input id="visionGameId" maxlength="12" placeholder="optional"></div></div><div class="write-row"><label class="switch"><input id="visionFusion" type="checkbox"> Require occupancy agreement</label><label class="switch"><input id="visionWrite" type="checkbox"> Write moves to Lichess</label></div><div class="actions"><button id="visionStart" class="primary">Start vision</button><button id="visionStop">Stop vision</button><button id="visionSave">Save fusion and Lichess</button><button id="visionRetry">Retry failed write once</button></div><p id="visionError" class="small vision-error"></p><label>All configured pieces: expected versus observed square and x/y coordinate</label><div id="visionPieces" class="piece-list vision-list"></div><label>Vision events</label><pre id="visionEvents" class="sensor-events">No vision events yet.</pre></div></div></div>
<div class="card wide"><div class="taskbar"><div><h2>8. Operations dashboard</h2><p class="small">Allowlisted tests, simulations, hardware demos, state tools, and Lichess workflows. Only one task can run at a time.</p></div><button id="taskStop" class="danger" disabled>Stop task</button></div><div id="ops" class="ops"><p>Loading operations…</p></div></div>
<div class="card wide"><div class="taskbar"><h2>Task output</h2><strong id="taskState" class="small">Idle</strong></div><pre id="tasklog" class="tasklog">No dashboard task has run.</pre></div>
<div class="card"><h2>Board state</h2><div class="actions"><button id="boardRefresh">Refresh state</button></div><pre id="boardout">Loading…</pre></div>
<div class="card"><h2>Activity</h2><pre id="log">Page ready.</pre></div>
</section></main>
<script>
const $=id=>document.getElementById(id);let state={},busy=false,jogBusy=false,taskData={run:null,logs:''},operations=[],lastPositionRead=0,liveData={status:{state:'idle',executed_count:0,last_event_id:null},logs:''},reedLast=null,reedTransitions=0,reedError='',sensorData=null,sensorEditing=false,visionData=null,visionPreviewSequence=-1;
function log(msg){const e=$('log');e.textContent+=`\n[${new Date().toLocaleTimeString()}] ${msg}`;e.scrollTop=e.scrollHeight}
async function api(path,options={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...options});const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.error||`HTTP ${r.status}`);return d}
function liveRunning(){return ['starting','homing','following','executing'].includes(liveData.status?.state)}
function taskRunning(){return (!!taskData.run&&['starting','running','stopping'].includes(taskData.run.state))||liveRunning()}
function render(s){state=s||{};const c=!!s.connected;const task=taskRunning();$('pill').className=`pill ${c?'good':(s.last_error?'bad':'')}`;$('pill').textContent=c?`${s.port} · ${s.baudrate}`:'Disconnected';$('firmware').textContent=s.firmware||s.last_error||'No controller identified.';$('xread').textContent=s.position_mm?.x==null?'—':`${s.position_mm.x.toFixed(2)} mm`;$('yread').textContent=s.position_mm?.y==null?'—':`${s.position_mm.y.toFixed(2)} mm`;const m=s.machine_position_mm||{};$('machineX').textContent=m.x==null?'—':m.x.toFixed(2);$('machineY').textContent=m.y==null?'—':m.y.toFixed(2);$('machineZ').textContent=m.z==null?'—':m.z.toFixed(2);$('positionAge').textContent=m.x==null?'No M114 position received.':`Updated ${new Date(lastPositionRead||Date.now()).toLocaleTimeString()} · millimetres`;$('homed').textContent=s.homed?'Yes':'No';$('revision').textContent=s.board_revision??'—';const w=s.workspace_mm||{};$('limits').textContent=`Logical workspace: inner ${w.min_x??'?'}–${w.max_x??'?'} mm, outer ${w.min_y??'?'}–${w.max_y??'?'} mm. Manual feed limit: ${s.max_manual_feed_mm_min??'?'} mm/min.`;$('lockState').textContent=s.calibrated?'Hardware execution is unlocked by config.':'Chess execution is locked: safety.calibrated is false.';$('connect').disabled=busy||c||task;$('disconnect').disabled=busy||!c||task;$('refresh').disabled=busy||task;$('endstops').disabled=busy||!c||task;$('home').disabled=busy||!c||task;$('move').disabled=busy||jogBusy||!c||!s.homed||task;$('refreshPosition').disabled=busy||jogBusy||!c||task;for(const id of ['jogUp','jogDown','jogLeft','jogRight'])$(id).disabled=busy||jogBusy||!c||!s.homed||task;$('jogHome').disabled=busy||!c||task;$('execute').disabled=busy||!c||!s.calibrated||task;$('plan').disabled=busy||task;$('stop').disabled=!c&&!task}
async function status(){try{render((await api('/api/status')).status)}catch(e){log(`Status error: ${e.message}`)}}
async function ports(){try{const selected=$('port').value;const d=await api('/api/ports');$('port').innerHTML='<option value="">Auto-detect</option>';for(const p of d.ports){const o=document.createElement('option');o.value=p.device;o.textContent=`${p.device} — ${p.description}${p.likely_printer?' ★':''}`;$('port').appendChild(o)}if([...$('port').options].some(o=>o.value===selected))$('port').value=selected;log(`Found ${d.ports.length} serial port(s).`)}catch(e){log(`Port scan: ${e.message}`)}}
async function action(label,fn){if(busy)return;busy=true;render(state);log(label);try{const d=await fn();if(d.status)render(d.status);return d}catch(e){log(`ERROR: ${e.message}`);await status()}finally{busy=false;render(state)}}
$('connect').onclick=()=>action('Connecting and verifying Marlin with M115…',async()=>{const d=await api('/api/connect',{method:'POST',body:JSON.stringify({port:$('port').value||null,baudrate:$('baud').value?Number($('baud').value):null})});log(`Connected to ${d.status.port}.`);return d});
async function autoConnect(){if(state.connected||taskRunning())return;log('Auto-connecting and verifying Marlin with M115…');busy=true;render(state);try{const d=await api('/api/connect',{method:'POST',body:JSON.stringify({port:null,baudrate:null})});log(`Connected to ${d.status.port} at ${d.status.baudrate}.`);render(d.status)}catch(e){log(`Auto-connect failed: ${e.message}`);log('Fix the reported problem, then press Connect.');await status()}finally{busy=false;render(state)}}
$('disconnect').onclick=()=>action('Disconnecting…',()=>api('/api/disconnect',{method:'POST',body:'{}'}));$('refresh').onclick=ports;
$('endstops').onclick=()=>action('Checking endstops…',async()=>{const d=await api('/api/endstops',{method:'POST',body:'{}'});log(d.lines.join('\n'));return d});
$('home').onclick=()=>action('Running firmware XYZ homing…',()=>api('/api/home',{method:'POST',body:JSON.stringify({confirm_motion:true})}));
$('move').onclick=()=>action('Sending manual coordinate move…',()=>api('/api/move',{method:'POST',body:JSON.stringify({x_mm:Number($('xmm').value),y_mm:Number($('ymm').value),feed_mm_min:Number($('feed').value),confirm_motion:true})}));
$('jogHome').onclick=$('home').onclick;
async function readPosition(){if(!state.connected||busy||jogBusy||taskRunning())return;try{const d=await api('/api/position',{method:'POST',body:'{}'});lastPositionRead=Date.now();render(d.status)}catch(e){log(`Position read: ${e.message}`)}}
async function jog(dx,dy){if(jogBusy||busy||taskRunning())return;jogBusy=true;render(state);try{const d=await api('/api/jog',{method:'POST',body:JSON.stringify({delta_x_mm:dx,delta_y_mm:dy,feed_mm_min:Number($('jogFeed').value),confirm_motion:true})});lastPositionRead=Date.now();render(d.status)}catch(e){log(`Jog blocked: ${e.message}`);await status()}finally{jogBusy=false;render(state)}}
function jogStep(){return Number($('jogStep').value)}
$('jogLeft').onclick=()=>jog(-jogStep(),0);$('jogRight').onclick=()=>jog(jogStep(),0);$('jogUp').onclick=()=>jog(0,jogStep());$('jogDown').onclick=()=>jog(0,-jogStep());$('refreshPosition').onclick=readPosition;
document.addEventListener('keydown',event=>{if(event.repeat||event.ctrlKey||event.metaKey||event.altKey)return;const tag=event.target?.tagName?.toLowerCase();if(['input','textarea','select','button'].includes(tag)||event.target?.isContentEditable)return;const step=jogStep();const moves={ArrowLeft:[-step,0],ArrowRight:[step,0],ArrowUp:[0,step],ArrowDown:[0,-step]};const move=moves[event.key];if(!move)return;event.preventDefault();jog(move[0],move[1])});
function moveObject(){let obj;try{obj=JSON.parse($('movejson').value)}catch(e){throw new Error(`Move JSON: ${e.message}`)}return obj}
$('plan').onclick=()=>action('Planning without moving hardware…',async()=>{const d=await api('/api/plan',{method:'POST',body:JSON.stringify({move:moveObject()})});$('planout').textContent=JSON.stringify(d.summary,null,2)+'\n\n'+d.gcode;log('Plan generated; board state was not changed.');return d});
$('execute').onclick=()=>action('Executing validated chess move…',async()=>{const d=await api('/api/execute',{method:'POST',body:JSON.stringify({move:moveObject(),confirm_motion:true})});$('planout').textContent=JSON.stringify(d.summary,null,2)+'\n\n'+d.gcode;await board();log('Move completed and board state committed.');return d});
$('stop').onclick=()=>action('EMERGENCY STOP…',()=>api('/api/stop',{method:'POST',body:'{}'}));
async function board(){try{$('boardout').textContent=JSON.stringify((await api('/api/board')).board_state,null,2)}catch(e){$('boardout').textContent=`ERROR: ${e.message}`}}$('boardRefresh').onclick=board;
function renderReed(value){const element=$('reedState');element.textContent=value.state.toUpperCase();element.className=`reed-state ${value.closed?'closed':''}`;$('reedAddress').textContent=`${value.bus} / ${value.address}`;$('reedRaw').textContent=value.raw_high?'HIGH':'LOW';if(reedLast!==null&&reedLast!==value.closed){reedTransitions++;const event=`[${new Date().toLocaleTimeString()}] ${value.closed?'CLOSED':'OPENED'} GPB0`;const logElement=$('reedLog');logElement.textContent+=`\n${event}`;logElement.scrollTop=logElement.scrollHeight}$('reedTransitions').textContent=reedTransitions;reedLast=value.closed;reedError=''}
async function reedStatus(){try{const data=await api('/api/reed',{method:'POST',body:'{}'});renderReed(data.reed)}catch(error){if(error.message!==reedError){$('reedLog').textContent+=`\n[${new Date().toLocaleTimeString()}] ERROR: ${error.message}`;reedError=error.message}$('reedState').textContent='ERROR';$('reedState').className='reed-state error'}}
$('reedRefresh').onclick=reedStatus;
function buildSensorBoard(matrix,expected){const root=$('sensorBoard');root.innerHTML='';for(let row=0;row<8;row++)for(let column=0;column<8;column++){const button=document.createElement('button');const occupied=matrix[row][column]===1;const changed=matrix[row][column]!==expected[row][column];button.className=`sensor-cell ${(row+column)%2?'dark':'light'} ${occupied?'occupied':''} ${changed?'changed':''}`;button.dataset.row=row;button.dataset.column=column;button.title=`${'abcdefgh'[column]}${8-row}: ${occupied?'occupied':'empty'}`;button.innerHTML=`<span class="sensor-rank">${column===0?8-row:''}</span><span class="sensor-file">${row===7?'abcdefgh'[column]:''}</span>${occupied?'1':'0'}`;button.onclick=()=>toggleSensorCell(row,column);root.appendChild(button)}}
async function toggleSensorCell(row,column){if(!sensorData)return;const matrix=sensorData.matrix.map(values=>values.slice());matrix[row][column]=matrix[row][column]?0:1;sensorEditing=true;await setSensorMatrix(matrix);sensorEditing=false}
async function setSensorMatrix(matrix){try{sensorData=(await api('/api/sensor/matrix',{method:'POST',body:JSON.stringify({matrix})})).sensor;renderSensor(sensorData)}catch(error){$('sensorError').textContent=error.message}}
function renderSensor(data){sensorData=data;$('sensorState').textContent=data.state;$('sensorTurn').textContent=data.turn;$('sensorMove').textContent=data.last_move||'—';$('sensorRate').textContent=`${data.sample_hz_observed.toFixed(1)} / ${data.sample_hz_target.toFixed(0)} Hz`;$('sensorError').textContent=data.error||`FEN: ${data.fen}`;buildSensorBoard(data.matrix,data.expected_matrix);if(!sensorEditing)$('sensorMatrixInput').value=JSON.stringify(data.matrix);$('sensorPieces').innerHTML=data.pieces.map(piece=>`<span>${piece.square} ${piece.symbol} ${piece.color} ${piece.type}</span>`).join('');$('sensorEvents').textContent=data.events.length?data.events.map(event=>`[${new Date(event.timestamp).toLocaleTimeString()}] ${event.kind.toUpperCase()} ${event.message}`).join('\n'):'No sensor events yet.';$('sensorGameId').value=data.game_id||'';$('sensorWrite').checked=data.write_lichess;const select=$('sensorDataset');if(!select.options.length)for(const dataset of data.datasets){const option=document.createElement('option');option.value=dataset.id;option.textContent=dataset.label;select.appendChild(option)}}
async function sensorStatus(){try{renderSensor((await api('/api/sensor/status')).sensor)}catch(error){$('sensorError').textContent=error.message}}
$('sensorReset').onclick=async()=>renderSensor((await api('/api/sensor/reset',{method:'POST',body:'{}'})).sensor);
$('sensorRunDataset').onclick=async()=>renderSensor((await api('/api/sensor/dataset',{method:'POST',body:JSON.stringify({dataset:$('sensorDataset').value})})).sensor);
$('sensorSaveWrite').onclick=async()=>renderSensor((await api('/api/sensor/lichess',{method:'POST',body:JSON.stringify({game_id:$('sensorGameId').value.trim(),write:$('sensorWrite').checked})})).sensor);
$('sensorApplyMatrix').onclick=async()=>{try{await setSensorMatrix(JSON.parse($('sensorMatrixInput').value))}catch(error){$('sensorError').textContent=`Matrix JSON: ${error.message}`}};
$('sensorRetry').onclick=async()=>renderSensor((await api('/api/sensor/retry',{method:'POST',body:'{}'})).sensor);
function renderVision(data){visionData=data;$('visionState').textContent=data.running?data.state:(data.enabled?'connecting':'disabled');$('visionTurn').textContent=data.turn;$('visionMove').textContent=data.last_move||'—';$('visionPiecesCount').textContent=`${data.observed_piece_count} / ${data.expected_piece_count}`;$('visionError').textContent=data.source_error||data.error||`Reference error ${data.reference_error??'—'} · smallest tag ${data.smallest_marker_px??'—'} px · ${data.frame_hz_observed.toFixed(1)} fps · ${data.piece_disagreements} disagreement(s)`;$('visionSource').value=data.source||'';$('visionGameId').value=data.game_id||'';$('visionWrite').checked=data.write_lichess;$('visionFusion').checked=data.fusion_enabled;$('visionMoveDetail').textContent=data.last_move_detail?JSON.stringify(data.last_move_detail,null,2):'No accepted vision move yet.';$('visionPieces').innerHTML=data.piece_comparison.map(piece=>{const expected=piece.expected_square?`${piece.expected_square} (${piece.expected_coordinate.x},${piece.expected_coordinate.y})`:'off board';const observed=piece.observed_square?`${piece.observed_square} (${piece.observed_coordinate.x},${piece.observed_coordinate.y})`:'not seen';return `<span title="camera row ${piece.camera_cell?.row??'—'}, column ${piece.camera_cell?.column??'—'} · marker ${piece.marker_side_px??'—'} px">#${piece.marker_id} ${piece.color} ${piece.type} · ${piece.piece_id} · expected ${expected} · observed ${observed} · ${piece.state}</span>`}).join('');$('visionEvents').textContent=data.events.length?data.events.map(event=>`[${new Date(event.timestamp).toLocaleTimeString()}] ${event.kind.toUpperCase()} ${event.message}`).join('\n'):'No vision events yet.';if(data.frames>visionPreviewSequence&&data.running){visionPreviewSequence=data.frames;$('visionPreview').src=`/api/vision/frame?t=${Date.now()}`}}
async function visionStatus(){try{renderVision((await api('/api/vision/status')).vision)}catch(error){$('visionError').textContent=error.message}}
$('visionStart').onclick=async()=>renderVision((await api('/api/vision/configure',{method:'POST',body:JSON.stringify({source:$('visionSource').value.trim(),enabled:true})})).vision);
$('visionStop').onclick=async()=>renderVision((await api('/api/vision/configure',{method:'POST',body:JSON.stringify({source:$('visionSource').value.trim(),enabled:false})})).vision);
$('visionReset').onclick=async()=>renderVision((await api('/api/vision/reset',{method:'POST',body:'{}'})).vision);
$('visionRetry').onclick=async()=>renderVision((await api('/api/vision/retry',{method:'POST',body:'{}'})).vision);
$('visionSave').onclick=async()=>{let value=await api('/api/vision/fusion',{method:'POST',body:JSON.stringify({enabled:$('visionFusion').checked})});renderVision(value.vision);value=await api('/api/vision/lichess',{method:'POST',body:JSON.stringify({game_id:$('visionGameId').value.trim(),write:$('visionWrite').checked})});renderVision(value.vision)};
function renderLive(){const s=liveData.status||{};$('liveState').textContent=s.state||'idle';$('liveCount').textContent=s.executed_count??0;$('liveLast').textContent=s.last_event_id||'—';$('liveLog').textContent=liveData.logs||'No live game started in this server session.';$('liveLog').scrollTop=$('liveLog').scrollHeight;$('liveStart').disabled=liveRunning()||taskRunning();$('liveStop').disabled=!liveRunning();render(state)}
async function liveStatus(){try{liveData=await api('/api/live/status');renderLive()}catch(e){log(`Live game status: ${e.message}`)}}
$('liveStart').onclick=async()=>{if(taskRunning()){log('Live play blocked: another task is running.');return}const gameId=$('liveGameId').value.trim();if(!gameId){log('Enter a Lichess game ID.');return}try{liveData=await api('/api/live/start',{method:'POST',body:JSON.stringify({game_id:gameId,confirm_standard_position:true,confirm_motion:true})});renderLive();renderOperations()}catch(e){log(`Live play blocked: ${e.message}`)}};
$('liveStop').onclick=async()=>{try{liveData=await api('/api/live/stop',{method:'POST',body:'{}'});renderLive();renderOperations()}catch(e){log(`Stop live play: ${e.message}`)}};
function renderOperations(){const root=$('ops');root.innerHTML='';let category='';for(const op of operations){if(op.category!==category){category=op.category;const heading=document.createElement('h3');heading.className='op-category';heading.textContent=category;root.appendChild(heading)}const card=document.createElement('div');card.className=`op ${op.physical?'physical':''}`;const tag=document.createElement('span');tag.className='tag';tag.textContent=op.physical?'Physical hardware':(op.long_running?'Managed process':'Safe task');const title=document.createElement('h3');title.textContent=op.title;const desc=document.createElement('p');desc.textContent=op.enabled?op.description:`${op.description} Physical tasks are disabled in demo mode.`;const actions=document.createElement('div');actions.className='actions';const run=document.createElement('button');run.className=op.physical?'danger':'primary';run.textContent=op.long_running?'Start':'Run';run.disabled=!op.enabled||(!!taskData.run&&['starting','running','stopping'].includes(taskData.run.state));run.onclick=async()=>{const confirmations=Object.fromEntries(op.confirmations.map(c=>[c.key,true]));try{taskData=await api('/api/tasks/start',{method:'POST',body:JSON.stringify({operation_id:op.id,confirmations})});renderTask();renderOperations()}catch(e){log(`Task blocked: ${e.message}`)}};actions.appendChild(run);card.append(tag,title,desc,actions);root.appendChild(card)}}
function renderTask(){const r=taskData.run;$('taskState').textContent=r?`${r.title}: ${r.state}`:'Idle';$('tasklog').textContent=taskData.logs||'No dashboard task has run.';$('tasklog').scrollTop=$('tasklog').scrollHeight;$('taskStop').disabled=!r||!['starting','running','stopping'].includes(r.state);render(state)}
async function loadOperations(){try{const d=await api('/api/operations');operations=d.operations;renderOperations()}catch(e){$('ops').textContent=`ERROR: ${e.message}`}}
async function taskStatus(){try{const prior=taskData.run?.state;taskData=await api('/api/tasks/status');renderTask();const next=taskData.run?.state;if(prior!==next)renderOperations()}catch(e){log(`Task status: ${e.message}`)}}
$('taskStop').onclick=async()=>{try{taskData=await api('/api/tasks/stop',{method:'POST',body:'{}'});renderTask();renderOperations()}catch(e){log(`Stop task: ${e.message}`)}};
(async()=>{await ports();await status();await autoConnect();await board();await loadOperations();await taskStatus();await liveStatus();await reedStatus();await sensorStatus();await visionStatus();setInterval(()=>{if(!busy)status();taskStatus();liveStatus();visionStatus()},750);setInterval(readPosition,750);setInterval(reedStatus,250);setInterval(sensorStatus,100)})();
</script></body></html>"""


class RequestHandler(BaseHTTPRequestHandler):
    controller: GantryController

    def _operations(self) -> OperationManager:
        manager = getattr(self.server, "operation_manager", None)
        if manager is None:
            raise ValidationError("operations dashboard is not configured")
        return manager

    def _live_game(self) -> LiveGameManager:
        manager = getattr(self.server, "live_game_manager", None)
        if manager is None:
            raise ValidationError("live game manager is not configured")
        return manager

    def _reed_switch(self) -> Any:
        reader = getattr(self.server, "reed_switch", None)
        if reader is None:
            raise ValidationError("reed switch reader is not configured")
        return reader

    def _sensor(self) -> SyntheticBoardSensor:
        sensor = getattr(self.server, "board_sensor", None)
        if sensor is None:
            raise ValidationError("synthetic board sensor is not configured")
        return sensor

    def _vision(self) -> VisionManager:
        manager = getattr(self.server, "vision_manager", None)
        if manager is None:
            raise ValidationError("vision manager is not configured")
        return manager

    def _send_jpeg(self, payload: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _require_no_task(self, action: str) -> None:
        if self._operations().running() or self._live_game().running():
            raise ValidationError(
                f"{action} is unavailable while a dashboard task is running"
            )

    def _clerk(self) -> Optional[ClerkVerifier]:
        return getattr(self.server, "clerk_verifier", None)

    def _cookie(self, name: str) -> str:
        cookie = self.headers.get("Cookie", "")
        for item in cookie.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                return value
        return ""

    def _same_site(self) -> bool:
        site = self.headers.get("Sec-Fetch-Site", "")
        return site in {"", "same-origin", "same-site", "none"}

    def _authenticated(self) -> bool:
        verifier = self._clerk()
        if verifier is None:
            return True
        if self.command not in SAFE_METHODS and not self._same_site():
            self.log_message("rejected a cross-site %s", self.command)
            return False
        session = self._cookie(CLERK_SESSION_COOKIE)
        if not session:
            return False
        try:
            verifier.verify(session)
        except GantryError as error:
            self.log_message("Clerk denied a request: %s", error)
            return False
        return True

    def _send_unauthorized(self) -> None:
        body = b"Authentication required. Sign in through Clerk on the dashboard page."
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {fmt % args}")

    def _send_json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("invalid request length") from exc
        if length > 262_144:
            raise ValidationError("request is too large")
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValidationError("request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("request body must be a JSON object")
        return value

    @staticmethod
    def _move_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = payload.get("move", payload)
        if not isinstance(value, Mapping):
            raise ValidationError("move must be a JSON object")
        return value

    def do_GET(self) -> None:
        dashboard = self.path == "/" or self.path.startswith("/?")
        if not dashboard and not self._authenticated():
            self._send_unauthorized()
            return
        if dashboard:
            body = getattr(self.server, "dashboard_html", HTML).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            self._send_json({"ok": True, "status": self.controller.status()})
            return
        if self.path == "/api/ports":
            self._send_json(
                {
                    "ok": True,
                    "ports": [
                        item.as_dict() for item in self.controller.available_ports()
                    ],
                }
            )
            return
        if self.path == "/api/board":
            self._send_json({"ok": True, "board_state": self.controller.board_state()})
            return
        if self.path == "/api/pending":
            self._send_json(
                {"ok": True, "pending": self.controller.pending_transaction()}
            )
            return
        if self.path == "/api/operations":
            self._send_json({"ok": True, "operations": self._operations().catalog()})
            return
        if self.path == "/api/tasks/status":
            self._send_json({"ok": True, **self._operations().status()})
            return
        if self.path == "/api/live/status":
            self._send_json({"ok": True, **self._live_game().status()})
            return
        if self.path == "/api/sensor/status":
            self._send_json({"ok": True, "sensor": self._sensor().status()})
            return
        if self.path == "/api/vision/status":
            self._send_json({"ok": True, "vision": self._vision().status()})
            return
        if self.path.startswith("/api/vision/frame"):
            try:
                self._send_jpeg(self._vision().preview_jpeg())
            except GantryError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._authenticated():
            self._send_unauthorized()
            return
        try:
            payload = self._read_json()
            if self.path == "/api/connect":
                self._require_no_task("serial connection")
                port = payload.get("port")
                baudrate = payload.get("baudrate")
                if port is not None and not isinstance(port, str):
                    raise ValidationError("port must be a string or null")
                if baudrate is not None:
                    if isinstance(baudrate, bool):
                        raise ValidationError("baudrate must be an integer or null")
                    try:
                        baudrate = int(baudrate)
                    except (TypeError, ValueError) as exc:
                        raise ValidationError(
                            "baudrate must be an integer or null"
                        ) from exc
                status = self.controller.connect(port=port or None, baudrate=baudrate)
                self._send_json({"ok": True, "status": status})
                return
            if self.path == "/api/disconnect":
                self._require_no_task("serial disconnect")
                self._send_json({"ok": True, "status": self.controller.disconnect()})
                return
            if self.path == "/api/endstops":
                self._require_no_task("endstop reading")
                lines = self.controller.check_endstops()
                self._send_json(
                    {
                        "ok": True,
                        "lines": list(lines),
                        "status": self.controller.status(),
                    }
                )
                return
            if self.path == "/api/reed":
                self._send_json(
                    {"ok": True, "reed": self._reed_switch().read().as_dict()}
                )
                return
            if self.path == "/api/sensor/matrix":
                self._send_json(
                    {
                        "ok": True,
                        "sensor": self._sensor().set_matrix(payload.get("matrix")),
                    }
                )
                return
            if self.path == "/api/sensor/reset":
                self._send_json({"ok": True, "sensor": self._sensor().reset()})
                return
            if self.path == "/api/sensor/dataset":
                dataset = payload.get("dataset")
                if not isinstance(dataset, str):
                    raise ValidationError("dataset must be a string")
                self._send_json(
                    {
                        "ok": True,
                        "sensor": self._sensor().apply_dataset(dataset),
                    }
                )
                return
            if self.path == "/api/sensor/lichess":
                self._send_json(
                    {
                        "ok": True,
                        "sensor": self._sensor().configure_lichess(
                            payload.get("game_id"), payload.get("write") is True
                        ),
                    }
                )
                return
            if self.path == "/api/sensor/retry":
                self._send_json({"ok": True, "sensor": self._sensor().retry()})
                return
            if self.path == "/api/vision/configure":
                source = payload.get("source")
                if not isinstance(source, str):
                    raise ValidationError("camera source must be a string")
                self._send_json(
                    {
                        "ok": True,
                        "vision": self._vision().configure(
                            source, payload.get("enabled") is True
                        ),
                    }
                )
                return
            if self.path == "/api/vision/reset":
                self._send_json({"ok": True, "vision": self._vision().tracker.reset()})
                return
            if self.path == "/api/vision/retry":
                self._send_json({"ok": True, "vision": self._vision().tracker.retry()})
                return
            if self.path == "/api/vision/fusion":
                sensor_matrix = self._sensor().status()["matrix"]
                self._send_json(
                    {
                        "ok": True,
                        "vision": self._vision().tracker.configure_fusion(
                            payload.get("enabled") is True, sensor_matrix
                        ),
                    }
                )
                return
            if self.path == "/api/vision/lichess":
                self._send_json(
                    {
                        "ok": True,
                        "vision": self._vision().tracker.configure_lichess(
                            payload.get("game_id"), payload.get("write") is True
                        ),
                    }
                )
                return
            if self.path == "/api/position":
                if self._operations().running():
                    raise ValidationError(
                        "position polling is unavailable while a dashboard task is running"
                    )
                self._send_json(
                    {"ok": True, "status": self.controller.query_position()}
                )
                return
            if self.path == "/api/home":
                self._require_no_task("homing")
                if payload.get("confirm_motion") is not True:
                    raise ValidationError(
                        "homing requires explicit motion confirmation"
                    )
                self._send_json({"ok": True, "status": self.controller.home_xy()})
                return
            if self.path == "/api/move":
                self._require_no_task("manual movement")
                if payload.get("confirm_motion") is not True:
                    raise ValidationError(
                        "manual movement requires explicit motion confirmation"
                    )
                try:
                    x_mm = float(payload["x_mm"])
                    y_mm = float(payload["y_mm"])
                    feed = float(payload["feed_mm_min"])
                except KeyError as exc:
                    raise ValidationError(f"missing field: {exc.args[0]}") from exc
                except (TypeError, ValueError) as exc:
                    raise ValidationError(
                        "coordinates and feed rate must be numbers"
                    ) from exc
                status = self.controller.move_to_mm(
                    x_mm=x_mm, y_mm=y_mm, feed_mm_min=feed
                )
                self._send_json({"ok": True, "status": status})
                return
            if self.path == "/api/jog":
                if self._operations().running():
                    raise ValidationError(
                        "keyboard jog is unavailable while a dashboard task is running"
                    )
                if payload.get("confirm_motion") is not True:
                    raise ValidationError(
                        "keyboard jog requires explicit motion confirmation"
                    )
                try:
                    delta_x = float(payload["delta_x_mm"])
                    delta_y = float(payload["delta_y_mm"])
                    feed = float(payload["feed_mm_min"])
                except KeyError as exc:
                    raise ValidationError(f"missing field: {exc.args[0]}") from exc
                except (TypeError, ValueError) as exc:
                    raise ValidationError(
                        "jog deltas and feed rate must be numbers"
                    ) from exc
                self._send_json(
                    {
                        "ok": True,
                        "status": self.controller.jog(
                            delta_x_mm=delta_x,
                            delta_y_mm=delta_y,
                            feed_mm_min=feed,
                        ),
                    }
                )
                return
            if self.path == "/api/plan":
                plan = self.controller.plan_move(self._move_payload(payload))
                self._send_json(
                    {
                        "ok": True,
                        "summary": plan.summary(),
                        "gcode": plan.program.text(),
                        "status": self.controller.status(),
                    }
                )
                return
            if self.path == "/api/execute":
                self._require_no_task("chess execution")
                plan = self.controller.execute_move(
                    self._move_payload(payload),
                    confirm_motion=payload.get("confirm_motion") is True,
                )
                self._send_json(
                    {
                        "ok": True,
                        "summary": plan.summary(),
                        "gcode": plan.program.text(),
                        "status": self.controller.status(),
                    }
                )
                return
            if self.path == "/api/stop":
                manager = self._operations()
                if manager.running():
                    self._send_json({"ok": True, **manager.stop()})
                else:
                    self._send_json(
                        {"ok": True, "status": self.controller.emergency_stop()}
                    )
                return
            if self.path == "/api/tasks/start":
                if self._live_game().running():
                    raise ValidationError(
                        "stop the live Lichess game before starting a dashboard task"
                    )
                operation_id = payload.get("operation_id")
                if not isinstance(operation_id, str):
                    raise ValidationError("operation_id must be a string")
                confirmations = payload.get("confirmations", {})
                if not isinstance(confirmations, Mapping):
                    raise ValidationError("confirmations must be an object")
                self._send_json(
                    {
                        "ok": True,
                        **self._operations().start(operation_id, confirmations),
                    }
                )
                return
            if self.path == "/api/tasks/stop":
                self._send_json({"ok": True, **self._operations().stop()})
                return
            if self.path == "/api/live/start":
                if self._operations().running():
                    raise ValidationError(
                        "stop the dashboard task before starting live play"
                    )
                game_id = payload.get("game_id")
                if not isinstance(game_id, str):
                    raise ValidationError("game_id must be a string")
                if self.controller.connected:
                    self.controller.disconnect()
                self._send_json(
                    {
                        "ok": True,
                        **self._live_game().start(
                            game_id,
                            confirm_standard_position=payload.get(
                                "confirm_standard_position"
                            )
                            is True,
                            confirm_motion=payload.get("confirm_motion") is True,
                        ),
                    }
                )
                return
            if self.path == "/api/live/stop":
                self._send_json({"ok": True, **self._live_game().stop()})
                return
            self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except GantryError as exc:
            self._send_json(
                {"ok": False, "error": str(exc), "status": self.controller.status()},
                HTTPStatus.CONFLICT,
            )
        except Exception as exc:
            self._send_json(
                {"ok": False, "error": f"unexpected server error: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


class GantryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    operation_manager: Optional[OperationManager] = None
    live_game_manager: Optional[LiveGameManager] = None
    clerk_verifier: Optional[ClerkVerifier] = None
    dashboard_html: str = HTML
    reed_switch: Any = None
    board_sensor: Optional[SyntheticBoardSensor] = None
    vision_manager: Optional[VisionManager] = None


def run_web_server(
    *,
    config: AppConfig,
    state_path: str,
    journal_path: str,
    audit_path: str,
    host: str = "0.0.0.0",
    port: int = 8000,
    open_browser: bool = True,
    demo: bool = False,
    clerk: Optional[ClerkSettings] = None,
) -> None:
    if not 1 <= port <= 65_535:
        raise ValidationError("web port must be between 1 and 65535")
    clerk_settings = (
        ClerkSettings.require_from_environment() if clerk is None else clerk
    )

    service = GantryService(config, state_path, journal_path, audit_path)
    controller = GantryController(config, service, demo=demo)
    RequestHandler.controller = controller
    server = GantryHTTPServer((host, port), RequestHandler)
    server.clerk_verifier = ClerkVerifier(clerk_settings)
    server.dashboard_html = render_dashboard(HTML, clerk_settings)
    root = Path.cwd().resolve()
    server.operation_manager = OperationManager(
        root,
        controller,
        operation_catalog(
            root,
            (root / "config.json").resolve(),
            Path(state_path).resolve(),
            Path(journal_path).resolve(),
            Path(audit_path).resolve(),
        ),
        allow_physical=not demo,
        allow_development=os.environ.get("CHESS_GANTRY_DISTROLESS") != "1",
    )
    server.live_game_manager = LiveGameManager(root, config, demo=demo)
    i2c_bus = int(os.environ.get("CHESS_GANTRY_I2C_BUS", "1"), 0)
    i2c_address = int(os.environ.get("CHESS_GANTRY_MCP23017_ADDRESS", "0x20"), 0)
    server.reed_switch = (
        SimulatedReedSwitch(bus_number=i2c_bus, address=i2c_address)
        if demo
        else MCP23017ReedSwitch(bus_number=i2c_bus, address=i2c_address)
    )
    server.board_sensor = SyntheticBoardSensor(sample_hz=300.0, stable_samples=3)
    vision_source = os.environ.get("CHESS_GANTRY_CAMERA_SOURCE", "").strip()
    vision_enabled = os.environ.get("CHESS_GANTRY_CAMERA_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    server.vision_manager = VisionManager(
        source=vision_source,
        enabled=vision_enabled,
        occupancy_provider=lambda: server.board_sensor.status()["matrix"],
    )
    every_interface = host in {"0.0.0.0", "::"}
    public_host = os.environ.get("CHESS_GANTRY_PUBLIC_HOST", "").strip()
    display_host = public_host or (_lan_address() if every_interface else host)
    url = f"http://{display_host}:{port}"
    print(f"Chess Gantry Controller running at {url}")
    if public_host and every_interface:
        print(f"Also reachable at http://{_lan_address()}:{port}")
    print(f"Bound to {host}:{port}")
    if every_interface:
        print(
            "EXPOSED: every interface is listening, so anyone who can route to this host"
            " can reach the dashboard, including the public internet behind a port"
            " forward or tunnel."
        )
    print(f"Clerk sign-in is required; frontend API {clerk_settings.frontend_api}")
    print(
        "Every user who can sign up in that Clerk instance can move the gantry."
        " Restrict sign-ups in the Clerk dashboard if that is not what you want."
    )
    print("Press Control-C to stop it.")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping web controller…")
    finally:
        if server.operation_manager is not None and server.operation_manager.running():
            server.operation_manager.stop()
        if server.live_game_manager is not None and server.live_game_manager.running():
            server.live_game_manager.stop()
        if server.board_sensor is not None:
            server.board_sensor.close()
        if server.vision_manager is not None:
            server.vision_manager.close()
        controller.disconnect()
        server.server_close()
