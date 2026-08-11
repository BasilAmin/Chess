from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional
import json
import os
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlsplit

from .clerk_auth import SESSION_COOKIE, ClerkSettings, ClerkVerifier, render_dashboard
from .config import AppConfig
from .controller import GantryController
from .errors import GantryError, ValidationError
from .game_coordinator import GameCoordinator
from .lichess_oauth import LichessOAuth
from .models import BoardState
from .serial_link import discover_serial_ports
from .service import GantryService
from .vision import DEFAULT_PHONE_SOURCE, SolVisionManager


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def web_bind_error(host: str, port: int, exc: OSError) -> ValidationError:
    if getattr(exc, "errno", None) == 98:
        return ValidationError(
            f"web address {host}:{port} is already in use; stop the existing "
            f"Chess Gantry server or use --web-port {port + 1}"
        )
    return ValidationError(f"could not bind web address {host}:{port}: {exc}")


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chess Gantry</title><style>
:root{color-scheme:dark;--ink:#edf4f8;--muted:#91a2ae;--ground:#071013;--panel:#0d191d;--raised:#132329;--line:#22373e;--mint:#72efc1;--amber:#ffc968;--red:#ff6677;--blue:#77b9ff;--shadow:0 20px 60px #0008;font-family:"IBM Plex Sans",Inter,system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#0b191d 0,#061013 48%,#080c10 100%);color:var(--ink);min-height:100vh}button,input,select{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.38}.shell{max-width:1460px;margin:auto;padding:24px}.mast{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;padding:8px 0 23px;border-bottom:1px solid var(--line)}.eyebrow{color:var(--mint);font:700 .72rem/1 monospace;letter-spacing:.16em;text-transform:uppercase}.mast h1{font-size:clamp(2.2rem,5vw,4.7rem);letter-spacing:-.07em;line-height:.9;margin:10px 0}.mast p{margin:0;color:var(--muted);max-width:650px}.master-status{display:flex;align-items:center;gap:9px;border:1px solid var(--line);background:#101e22;padding:10px 14px;border-radius:999px;white-space:nowrap}.dot{width:9px;height:9px;background:var(--amber);border-radius:50%;box-shadow:0 0 18px currentColor}.dot.good{background:var(--mint)}.dot.bad{background:var(--red)}.readiness{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin:18px 0}.ready-item{background:#0d191d;padding:13px 16px}.ready-item span{display:block;color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.1em}.ready-item strong{display:block;margin-top:5px}.blocker{display:none;grid-template-columns:1fr auto;gap:16px;align-items:center;background:#2a1719;border:1px solid #74313b;border-radius:14px;padding:16px;margin-bottom:18px}.blocker.show{display:grid}.blocker h2{margin:0;color:#ffbdc4}.blocker p{margin:6px 0 0;color:#eab5bb}.layout{display:grid;grid-template-columns:340px minmax(0,1fr);gap:18px}.rail{display:flex;flex-direction:column;gap:14px}.card{background:linear-gradient(180deg,#101e22,#0b161a);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}.card-pad{padding:17px}.section-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:15px}.step{color:var(--mint);font:700 .65rem/1 monospace;letter-spacing:.14em;text-transform:uppercase}.section-head h2{font-size:1.05rem;margin:5px 0 0}.badge{font:700 .72rem/1 monospace;padding:7px 8px;border:1px solid var(--line);border-radius:8px;color:var(--muted)}.badge.good{color:var(--mint);border-color:#28644f}.badge.bad{color:#ff9baa;border-color:#70303a}.field{margin-top:11px}.field label{display:block;color:var(--muted);font-size:.75rem;margin:0 0 6px}.control{width:100%;background:#14252a;color:var(--ink);border:1px solid #2a424a;border-radius:9px;padding:10px 11px}.control:focus{outline:2px solid #46cfa0;outline-offset:1px}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.btn{border:1px solid #2c464f;background:#172a30;color:var(--ink);border-radius:9px;padding:10px 12px;font-weight:750}.btn.primary{background:var(--mint);border-color:var(--mint);color:#06120e}.btn.danger{background:#411a21;border-color:#76313b;color:#ffd5da}.btn.ghost{background:transparent}.hint{font-size:.75rem;color:var(--muted);line-height:1.45;margin:10px 0 0}.workspace{min-width:0;display:flex;flex-direction:column;gap:18px}.vision-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(330px,.65fr);gap:1px;background:var(--line);border-radius:16px;overflow:hidden;border:1px solid var(--line)}.vision-pane,.board-pane{background:#0a1417;padding:16px;min-width:0}.pane-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:11px}.pane-title h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.1em;margin:0;color:#b7c6ce}.camera-wrap{position:relative;background:#030708;border-radius:11px;overflow:hidden;aspect-ratio:16/9;display:grid;place-items:center}.camera{width:100%;height:100%;object-fit:contain}.camera-empty{position:absolute;color:#6f818a;text-align:center;padding:30px;pointer-events:none}.camera-empty strong{display:block;color:#a7b6bd;margin-bottom:5px}.scan{position:absolute;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--mint),transparent);box-shadow:0 0 12px var(--mint);animation:scan 2.8s linear infinite;opacity:0}.scanning .scan{opacity:.8}@keyframes scan{from{top:8%}to{top:92%}}.camera-note{padding:10px 2px 0;color:var(--muted);font-size:.76rem;min-height:28px}.board-shell{width:min(100%,560px);margin:auto;display:grid;grid-template-columns:20px 1fr;grid-template-rows:1fr 20px}.ranks,.files{color:#80919a;font:600 .65rem/1 monospace}.ranks{display:grid;grid-template-rows:repeat(8,1fr);place-items:center}.files{display:grid;grid-template-columns:repeat(8,1fr);place-items:center}.board{display:grid;grid-template-columns:repeat(8,1fr);aspect-ratio:1;border:2px solid #30464e;box-shadow:0 16px 40px #0008}.square{display:grid;place-items:center;font-size:clamp(1.6rem,4vw,3.5rem);line-height:1}.light{background:#c0d1d1;color:#152426}.dark{background:#527780;color:#f3f9f8}.unknown{color:#8f2f3a}.status-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stat{background:#102025;border:1px solid var(--line);border-radius:11px;padding:13px}.stat span{display:block;color:var(--muted);font-size:.7rem;text-transform:uppercase;letter-spacing:.09em}.stat strong{display:block;margin-top:7px;overflow-wrap:anywhere}.game-strip{display:grid;grid-template-columns:1fr 1fr;gap:18px}.summary{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.summary div{background:#122228;border:1px solid var(--line);border-radius:10px;padding:12px}.summary small{color:var(--muted);display:block}.summary strong{display:block;margin-top:5px}.piece-list{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;max-height:220px;overflow:auto}.piece{background:#122228;border-left:3px solid var(--blue);border-radius:7px;padding:8px;font:600 .7rem/1.35 monospace}.logs{margin:0;background:#050b0d;border:1px solid #17282d;border-radius:10px;padding:13px;white-space:pre-wrap;min-height:140px;max-height:280px;overflow:auto;color:#a9bac2;font:12px/1.55 monospace}.recovery{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end}.recovery input{min-width:230px}.toast{position:fixed;right:22px;bottom:22px;max-width:440px;background:#18282d;border:1px solid #3a5660;box-shadow:var(--shadow);border-radius:12px;padding:13px 15px;transform:translateY(30px);opacity:0;pointer-events:none;transition:.2s}.toast.show{transform:none;opacity:1}.toast.error{border-color:#7c3540;color:#ffd1d7}@media(max-width:1040px){.layout{grid-template-columns:1fr}.rail{display:grid;grid-template-columns:repeat(2,1fr)}.vision-grid{grid-template-columns:1fr}.game-strip{grid-template-columns:1fr}}@media(max-width:680px){.shell{padding:14px}.mast{align-items:flex-start;flex-direction:column}.readiness{grid-template-columns:1fr 1fr}.rail{display:flex}.status-grid{grid-template-columns:1fr 1fr}.piece-list{grid-template-columns:1fr 1fr}.blocker,.recovery{grid-template-columns:1fr}.row .btn{flex:1}.vision-pane,.board-pane{padding:10px}}
</style></head><body><main class="shell"><header class="mast"><div><div class="eyebrow">Physical chess control system</div><h1>Chess Gantry</h1><p>Frame the board. Confirm Sol. Start the game. Remote moves are planned, journaled, and acknowledged by Marlin.</p></div><div class="master-status"><i id="masterDot" class="dot"></i><strong id="masterText">Loading system</strong></div></header>
<section class="readiness"><div class="ready-item"><span>Phone camera</span><strong id="readyCamera">Checking</strong></div><div class="ready-item"><span>OpenAI Sol</span><strong id="readySol">Checking</strong></div><div class="ready-item"><span>Motion</span><strong id="readyMotion">Checking</strong></div><div class="ready-item"><span>Lichess</span><strong id="readyLichess">Checking</strong></div></section>
<section id="blocker" class="blocker"><div><h2>Physical recovery required</h2><p id="blockerText">A pending transaction blocks games and movement.</p></div><button id="recoveryFocus" class="btn danger">Resolve below</button></section>
<div class="layout"><aside class="rail">
<section class="card card-pad"><div class="section-head"><div><div class="step">Step 01</div><h2>Phone and Sol</h2></div><span id="cameraBadge" class="badge">Idle</span></div><div class="field"><label for="cameraSource">Camera source</label><input id="cameraSource" class="control" value="browser:http://192.168.100.88:8080"></div><div class="field"><label for="rotation">Rotate phone image</label><select id="rotation" class="control"><option value="0">0°</option><option value="90">90° clockwise</option><option value="180">180°</option><option value="270">90° counterclockwise</option></select></div><div class="field"><label for="orientation">Board orientation after rotation</label><select id="orientation" class="control"><option value="white_bottom">White nearest bottom</option><option value="black_bottom">Black nearest bottom</option></select></div><div class="row"><button id="cameraTest" class="btn">Test phone</button><button id="cameraStart" class="btn primary">Start recognition</button><button id="calibrate" class="btn">Calibrate 4 corners</button><button id="clearCalibration" class="btn ghost">Clear crop</button><button id="cameraPopout" class="btn">Pop out</button><button id="cameraStop" class="btn ghost">Stop</button></div><p id="calibrationHint" class="hint">Start the phone camera server, then test. For plan view click: top-left, top-right, bottom-right, bottom-left.</p></section>
<section class="card card-pad"><div class="section-head"><div><div class="step">Step 02</div><h2>Motion controller</h2></div><span id="motionBadge" class="badge">Offline</span></div><div class="field"><label for="serialPort">Serial port</label><select id="serialPort" class="control"><option value="">Auto-detect USB Marlin</option></select></div><div class="field"><label for="baudrate">Baud rate</label><select id="baudrate" class="control"><option value="">Auto: 115200 then 250000</option><option value="115200">115200</option><option value="250000">250000</option></select></div><div class="summary"><div><small>Active port</small><strong id="port">—</strong></div><div><small>Homed</small><strong id="homed">No</strong></div><div><small>Firmware</small><strong id="firmware">—</strong></div><div><small>Revision</small><strong id="revision">0</strong></div></div><div class="row"><button id="scanPorts" class="btn">Scan ports</button><button id="connect" class="btn primary">Connect</button><button id="home" class="btn">Home XYZ</button><button id="disconnect" class="btn ghost">Disconnect</button></div><button id="stop" class="btn danger" style="width:100%;margin-top:9px">Emergency stop</button><p id="serialHint" class="hint">Use Scan ports after reconnecting USB. The connector suppresses DTR/RTS reset and waits for CH340 re-enumeration.</p></section>
<section class="card card-pad"><div class="section-head"><div><div class="step">Step 03</div><h2>Game mode</h2></div><span id="gameBadge" class="badge">Idle</span></div><div id="lichessAccount" class="summary"><div><small>Lichess account</small><strong id="lichessUser">Not connected</strong></div><div><small>Scope</small><strong id="lichessScope">board:play</strong></div></div><div class="row"><button id="lichessConnect" class="btn">Connect Lichess</button><button id="lichessDisconnect" class="btn ghost">Forget token</button></div><div class="field"><label for="mode">Who is playing?</label><select id="mode" class="control"><option value="local">Two people on this board</option><option value="lichess">Camera player vs Lichess / AI</option><option value="mirror">Mirror two remote / AI players</option></select></div><div id="lichessFields"><div class="field"><label for="gameId">Lichess game ID</label><input id="gameId" class="control" value="J21i9aA4" maxlength="12"></div><div id="colorField" class="field"><label for="localColor">Camera player controls</label><select id="localColor" class="control"><option value="white">White</option><option value="black">Black</option></select></div></div><div class="row"><button id="gameStart" class="btn primary">Start full game</button><button id="gameStop" class="btn ghost">Stop game</button></div><p id="gameHint" class="hint">Both people move pieces manually. Sol records legal settled positions.</p></section>
</aside><div class="workspace">
<section class="vision-grid"><div class="vision-pane"><div class="pane-title"><h2 id="cameraViewTitle">Raw phone frame</h2><span id="frameMeta" class="badge">No frame</span></div><div class="row"><button id="showRaw" class="btn">Raw frame</button><button id="showPlan" class="btn">Plan view</button></div><div id="cameraWrap" class="camera-wrap"><img id="phoneStream" class="camera" alt="Direct phone camera broadcast" crossorigin="anonymous"><img id="camera" class="camera" alt="Processed camera preview" style="display:none"><canvas id="cameraCanvas" hidden></canvas><div id="cameraEmpty" class="camera-empty"><strong>Waiting for camera</strong>Start the phone camera app, then press Test phone.</div><i class="scan"></i></div><div id="cameraNote" class="camera-note">The phone is connected only while fresh frames arrive.</div></div><div class="board-pane"><div class="pane-title"><h2>Sol reconstruction</h2><span id="transcriptionBadge" class="badge">Idle</span></div><div class="board-shell"><div class="ranks"><span>8</span><span>7</span><span>6</span><span>5</span><span>4</span><span>3</span><span>2</span><span>1</span></div><div id="board" class="board"></div><div></div><div class="files"><span>a</span><span>b</span><span>c</span><span>d</span><span>e</span><span>f</span><span>g</span><span>h</span></div></div></div></section>
<section class="status-grid"><div class="stat"><span>Sol state</span><strong id="cameraState">Idle</strong></div><div class="stat"><span>Stable observations</span><strong id="stable">0 / 2</strong></div><div class="stat"><span>Turn</span><strong id="turn">—</strong></div><div class="stat"><span>Last move</span><strong id="lastMove">—</strong></div></section>
<section class="game-strip"><div class="card card-pad"><div class="section-head"><div><div class="step">Detected board</div><h2>Pieces</h2></div><span id="pieceCount" class="badge">0 pieces</span></div><div id="pieces" class="piece-list"><div class="piece">No complete position yet.</div></div></div><div class="card card-pad"><div class="section-head"><div><div class="step">Game coordinator</div><h2>Current game</h2></div></div><div class="summary"><div><small>State</small><strong id="gameState">Idle</strong></div><div><small>Mode</small><strong id="gameMode">—</strong></div><div><small>Confirmed ply</small><strong id="ply">0</strong></div><div><small>Physical moves</small><strong id="executed">0</strong></div></div><p id="gameError" class="hint"></p></div></section>
<section id="recovery" class="card card-pad"><div class="section-head"><div><div class="step">Physical safety</div><h2>Pending transaction recovery</h2></div><span id="pendingBadge" class="badge">Clear</span></div><p id="pendingSummary" class="hint">No pending physical transaction.</p><div class="recovery"><div class="field"><label for="confirmation">Type the exact phrase for the action</label><input id="confirmation" class="control" placeholder="MOVE COMPLETED or MOVE DID NOT HAPPEN"></div><div class="row"><button id="applyPending" class="btn danger">Move completed</button><button id="discardPending" class="btn">Move did not happen</button></div></div></section>
<section class="card card-pad"><div class="section-head"><div><div class="step">Activity</div><h2>Game log</h2></div><button id="refresh" class="btn ghost">Refresh</button></div><pre id="logs" class="logs">No events yet.</pre></section>
<section class="card card-pad"><div class="section-head"><div><div class="step">Commissioning</div><h2>Hardware test matrix</h2></div><span id="setupBadge" class="badge">Not started</span></div><div class="summary"><div><small>Diagnostics</small><strong id="setupDiagnostics">Pending</strong></div><div><small>Endstops</small><strong id="setupEndstops">Pending</strong></div><div><small>Homing</small><strong id="setupHomed">Pending</strong></div><div><small>5 mm motion</small><strong id="setupMovement">Pending</strong></div><div><small>Magnet pulse</small><strong id="setupMagnet">Pending</strong></div><div><small>64 centers</small><strong id="setupCenters">Pending</strong></div></div><div class="field"><label for="setupConfirmation">Type SETUP AREA CLEAR before any motion test</label><input id="setupConfirmation" class="control" placeholder="SETUP AREA CLEAR"></div><div class="row"><button id="setupDiagnosticsButton" class="btn">1. Diagnostics</button><button id="setupEndstopsButton" class="btn">2. Endstops</button><button id="setupHomeButton" class="btn">3. Home</button><button id="setupMovementButton" class="btn">4. Move 5 mm</button><button id="setupMagnetButton" class="btn">5. Pulse magnet</button><button id="setupCentersButton" class="btn">6. Visit centers</button><button id="setupCombinedButton" class="btn primary">Run complete setup</button></div><pre id="setupResults" class="logs">Run diagnostics first. Physical steps stop on the first failure.</pre></section>
</div></div></main><div id="toast" class="toast"></div><script>
const $=id=>document.getElementById(id);let current=null,busy=false;const symbols={P:'♙',N:'♘',B:'♗',R:'♖',Q:'♕',K:'♔',p:'♟',n:'♞',b:'♝',r:'♜',q:'♛',k:'♚','.':'',x:'?', '?':'?'};async function api(path,options={}){const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options});const body=await response.json();if(!response.ok||body.ok===false)throw new Error(body.error||`HTTP ${response.status}`);return body}function toast(message,error=false){const node=$('toast');node.textContent=message;node.className=`toast show${error?' error':''}`;setTimeout(()=>node.className='toast',3200)}async function act(fn){if(busy)return;busy=true;try{await fn();await refresh()}catch(error){toast(error.message,true)}finally{busy=false}}function badge(node,text,state=''){node.textContent=text;node.className=`badge ${state}`.trim()}function draw(rows){const root=$('board');root.innerHTML='';const values=rows||Array(8).fill('........');for(let row=0;row<8;row++)for(let column=0;column<8;column++){const value=values[row]?.[column]||'?';const cell=document.createElement('div');cell.className=`square ${(row+column)%2?'dark':'light'} ${value==='?'||value==='x'?'unknown':''}`;cell.textContent=symbols[value]??'?';root.appendChild(cell)}}function gameHelp(mode){$('lichessFields').style.display=mode==='local'?'none':'block';$('colorField').style.display=mode==='lichess'?'block':'none';$('gameHint').textContent=mode==='local'?'Both people move pieces manually. Sol records legal settled positions.':mode==='lichess'?'The camera player moves manually. Opponent moves are executed by the gantry.':'Every move from the fresh Lichess game is executed physically.'}function render(data){current=data;const c=data.camera,g=data.game.status,s=data.controller,p=data.pending,cap=data.capabilities,li=data.lichess;const cameraConnected=c.frames>0;const solReady=c.status==='complete'&&c.stable_observations>=2;const gameRunning=!['idle','stopped','failed'].includes(g.state);const mode=$('mode').value;$('readyCamera').textContent=cameraConnected?'Connected':'Not connected';$('readySol').textContent=cap.openai?(solReady?'Board stable':c.status==='complete'?'Stabilizing':'Key ready'):'API key missing';$('readyMotion').textContent=cap.demo?'Demo mode':s.connected?(s.homed?'Homed':'Connected'):'Disconnected';$('readyLichess').textContent=cap.lichess?'Token ready':'Token missing';const modeReady=mode==='mirror'?cap.lichess:solReady&&cap.openai&&(mode==='local'||cap.lichess);const allReady=modeReady&&!p;const master=$('masterDot');master.className=`dot ${allReady?'good':p?'bad':''}`;$('masterText').textContent=p?'Recovery required':allReady?'Ready to start':'Setup incomplete';$('blocker').classList.toggle('show',Boolean(p));$('blockerText').textContent=p?`Pending ${p.move?.position||'piece'} move from (${p.move?.px},${p.move?.py}) to (${p.move?.nx},${p.move?.ny}). Inspect the physical board before choosing.`:'';badge($('cameraBadge'),c.running?'Live':'Idle',c.running?'good':'');badge($('motionBadge'),s.connected?(s.homed?'Homed':'Connected'):'Offline',s.homed?'good':s.last_error?'bad':'');badge($('gameBadge'),g.state||'Idle',g.state==='failed'?'bad':gameRunning?'good':'');badge($('transcriptionBadge'),c.status||'Idle',c.status==='complete'?'good':c.status==='not_found'||c.status==='unusable'?'bad':'');badge($('pendingBadge'),p?'Blocked':'Clear',p?'bad':'good');$('cameraSource').value=c.source;$('orientation').value=c.orientation;$('rotation').value=String(c.rotation||0);$('cameraState').textContent=c.status||'idle';$('stable').textContent=`${c.stable_observations} / 2`;$('turn').textContent=c.turn||'—';$('lastMove').textContent=c.last_move||'—';$('frameMeta').textContent=cameraConnected?`${c.frames} frame${c.frames===1?'':'s'} · ${c.rotation||0}°`:'No frame';$('cameraWrap').classList.toggle('scanning',c.running&&Boolean(cap.openai));$('cameraEmpty').style.display=cameraConnected?'none':'block';if(cameraConnected)$('camera').src=`/api/camera/frame?t=${Date.now()}`;const issue=c.error||(c.problems||[]).join(', ');$('cameraNote').textContent=issue||'Frame connected. Sol checks the newest image every three seconds.';draw(c.rows);$('pieceCount').textContent=`${(c.pieces||[]).length} pieces`;$('pieces').innerHTML=(c.pieces||[]).map(piece=>`<div class="piece">${piece.square} · ${piece.color} ${piece.type}</div>`).join('')||'<div class="piece">No complete position yet.</div>';$('port').textContent=s.port||'—';$('homed').textContent=s.homed?'Yes':'No';$('firmware').textContent=s.firmware||'—';$('revision').textContent=s.board_revision??'—';$('gameState').textContent=g.state||'idle';$('gameMode').textContent=g.mode||'—';$('ply').textContent=g.confirmed_ply??0;$('executed').textContent=g.executed_count??0;$('gameError').textContent=g.error||'No game error.';$('logs').textContent=data.game.logs||'No events yet.';$('pendingSummary').textContent=p?JSON.stringify({event:p.move?.event_id,piece:p.move?.position,from:[p.move?.px,p.move?.py],to:[p.move?.nx,p.move?.ny],created:p.created_at},null,2):'No pending physical transaction.';$('connect').disabled=s.connected||gameRunning;$('disconnect').disabled=!s.connected||gameRunning;$('home').disabled=!s.connected||gameRunning;$('gameStart').disabled=gameRunning||Boolean(p)||!modeReady;$('gameStop').disabled=!gameRunning;$('applyPending').disabled=!p;$('discardPending').disabled=!p;$('cameraStart').disabled=c.running;$('cameraStop').disabled=!c.enabled}async function refresh(){try{render(await api('/api/full-status'))}catch(error){toast(error.message,true)}}$('mode').onchange=()=>{gameHelp($('mode').value);if(current)render(current)};$('recoveryFocus').onclick=()=>$('recovery').scrollIntoView({behavior:'smooth'});$('refresh').onclick=refresh;$('cameraPopout').onclick=()=>window.open('/camera','gantryCamera','width=1100,height=850');$('connect').onclick=()=>act(()=>api('/api/controller/connect',{method:'POST',body:'{}'}));$('disconnect').onclick=()=>act(()=>api('/api/controller/disconnect',{method:'POST',body:'{}'}));$('home').onclick=()=>act(()=>api('/api/controller/home',{method:'POST',body:'{}'}));$('stop').onclick=()=>act(()=>api('/api/controller/stop',{method:'POST',body:'{}'}));$('cameraStart').onclick=()=>act(()=>api('/api/camera/configure',{method:'POST',body:JSON.stringify({source:$('cameraSource').value.trim(),orientation:$('orientation').value,rotation:Number($('rotation').value),enabled:true})}));$('cameraStop').onclick=()=>act(()=>api('/api/camera/configure',{method:'POST',body:JSON.stringify({source:$('cameraSource').value.trim(),orientation:$('orientation').value,rotation:Number($('rotation').value),enabled:false})}));$('gameStart').onclick=()=>act(()=>api('/api/game/start',{method:'POST',body:JSON.stringify({mode:$('mode').value,game_id:$('gameId').value.trim()||null,local_color:$('localColor').value,confirm_motion:true,serial_port:$('serialPort').value,serial_baudrate:$('baudrate').value})}));$('gameStop').onclick=()=>act(()=>api('/api/game/stop',{method:'POST',body:'{}'}));$('applyPending').onclick=()=>act(()=>api('/api/reconcile/apply',{method:'POST',body:JSON.stringify({confirmation:$('confirmation').value})}));$('discardPending').onclick=()=>act(()=>api('/api/reconcile/discard',{method:'POST',body:JSON.stringify({confirmation:$('confirmation').value})}));gameHelp('local');draw(null);refresh();setInterval(refresh,1000);
</script><script>
let enhancedPortSelection='',calibrationClicks=[],calibrationActive=false;function updateEnhancements(data){const li=data.lichess||{},ports=data.ports||[],camera=data.camera||{};$('lichessUser').textContent=li.username||'Not connected';$('lichessScope').textContent=(li.scopes||[]).join(' ')||'board:play';$('lichessConnect').disabled=Boolean(li.connected);$('lichessDisconnect').disabled=!li.connected;const select=$('serialPort');const selected=select.value||enhancedPortSelection;if(document.activeElement!==select){select.innerHTML='<option value="">Auto-detect USB Marlin</option>'+ports.map(port=>`<option value="${port.device}">${port.device} · ${port.description}</option>`).join('');if([...select.options].some(option=>option.value===selected))select.value=selected}const age=camera.last_frame_at?Math.max(0,Date.now()/1000-camera.last_frame_at):null;if(age!==null)$('frameMeta').textContent=`${camera.frames} frames · ${age.toFixed(1)}s old · ${camera.rotation||0}°`;$('serialHint').textContent=data.controller.last_error||'Use Scan ports after reconnecting USB. The connector suppresses DTR/RTS reset and waits for CH340 re-enumeration.';$('calibrationHint').textContent=camera.calibrated?'Board crop calibrated. Sol receives a square top-down 1024×1024 board.':'For flawless square mapping, click: top-left, top-right, bottom-right, bottom-left.';const pieces=camera.pieces||[];if(pieces.length)$('pieces').innerHTML=pieces.map(piece=>{const machine=piece.machine_mm?` · machine ${piece.machine_mm.x.toFixed(1)},${piece.machine_mm.y.toFixed(1)} mm`:'';return `<div class="piece">${piece.square} · grid ${piece.grid.x},${piece.grid.y}${machine}<br>${piece.color} ${piece.type}</div>`}).join('')}
function normalizedImageClick(event){const image=event.target,rect=image.getBoundingClientRect(),naturalRatio=image.naturalWidth/image.naturalHeight,boxRatio=rect.width/rect.height;let width=rect.width,height=rect.height,left=rect.left,top=rect.top;if(naturalRatio>boxRatio){height=rect.width/naturalRatio;top+=((rect.height-height)/2)}else{width=rect.height*naturalRatio;left+=((rect.width-width)/2)}const x=(event.clientX-left)/width,y=(event.clientY-top)/height;if(x<0||x>1||y<0||y>1)throw new Error('Click the visible camera image, not the letterbox area.');return{x,y}}const originalRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data)}catch(error){toast(error.message,true)}};$('scanPorts').onclick=refresh;$('serialPort').onchange=()=>enhancedPortSelection=$('serialPort').value;$('connect').onclick=()=>act(()=>api('/api/controller/connect',{method:'POST',body:JSON.stringify({port:$('serialPort').value,baudrate:$('baudrate').value})}));$('lichessConnect').onclick=()=>act(async()=>{const value=await api('/api/lichess/oauth/start',{method:'POST',body:'{}'});window.location.assign(value.result.url)});$('lichessDisconnect').onclick=()=>act(()=>api('/api/lichess/disconnect',{method:'POST',body:'{}'}));$('calibrate').onclick=()=>{calibrationClicks=[];calibrationActive=true;$('calibrationHint').textContent='Click top-left corner (1 of 4).'};$('camera').onclick=event=>{if(!calibrationActive)return;try{calibrationClicks.push(normalizedImageClick(event))}catch(error){toast(error.message,true);return}const names=['top-left','top-right','bottom-right','bottom-left'];if(calibrationClicks.length<4){$('calibrationHint').textContent=`Click ${names[calibrationClicks.length]} corner (${calibrationClicks.length+1} of 4).`}else{calibrationActive=false;act(()=>api('/api/camera/calibrate',{method:'POST',body:JSON.stringify({corners:calibrationClicks})}))}};$('clearCalibration').onclick=()=>act(()=>api('/api/camera/calibration/clear',{method:'POST',body:'{}'}));refresh();
</script><script>
const setupRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data);const setup=data.controller.setup||{};for(const key of ['diagnostics','endstops','homed','movement','magnet','centers']){const node=$('setup'+key[0].toUpperCase()+key.slice(1));node.textContent=setup[key]?'Passed':'Pending'}badge($('setupBadge'),setup.centers?'Commissioned':setup.last_error?'Failed':'In progress',setup.centers?'good':setup.last_error?'bad':'');$('setupResults').textContent=setup.last_error?`FAILED at ${setup.last_step}: ${setup.last_error}`:JSON.stringify(setup.results||{},null,2)}catch(error){toast(error.message,true)}};const setupBody=()=>JSON.stringify({confirmation:$('setupConfirmation').value});$('setupDiagnosticsButton').onclick=()=>act(()=>api('/api/setup/diagnostics',{method:'POST',body:'{}'}));$('setupEndstopsButton').onclick=()=>act(()=>api('/api/setup/endstops',{method:'POST',body:'{}'}));$('setupHomeButton').onclick=()=>act(()=>api('/api/setup/home',{method:'POST',body:setupBody()}));$('setupMovementButton').onclick=()=>act(()=>api('/api/setup/movement',{method:'POST',body:setupBody()}));$('setupMagnetButton').onclick=()=>act(()=>api('/api/setup/magnet',{method:'POST',body:setupBody()}));$('setupCentersButton').onclick=()=>act(()=>api('/api/setup/centers',{method:'POST',body:setupBody()}));$('setupCombinedButton').onclick=()=>act(()=>api('/api/setup/combined',{method:'POST',body:setupBody()}));refresh();
</script><script>
let cameraViewMode='raw';function phoneBase(source){return source.replace(/^auto:/,'').replace(/^snapshot:/,'').replace(/\/shot\.jpg.*$/,'').replace(/\/video.*$/,'').replace(/\/$/,'')}function renderCameraHealth(data){const camera=data.camera,fresh=camera.running&&!camera.stale;$('readyCamera').textContent=fresh?`Connected · ${camera.resolved_source||camera.source}`:camera.capture_error?'Offline':'Waiting';badge($('cameraBadge'),fresh?'Live':camera.stale?'Stale':'Offline',fresh?'good':camera.stale?'bad':'');$('cameraNote').textContent=camera.capture_error||camera.inference_error||(fresh?`Fresh plan frame · ${camera.frame_age_s.toFixed(1)}s old`:'No fresh phone frame. Start the camera server and press Test phone.');$('cameraEmpty').style.display=fresh?'none':'block';if(fresh&&cameraViewMode==='plan')$('camera').src=`/api/camera/frame?t=${Date.now()}`;$('cameraViewTitle').textContent=cameraViewMode==='raw'?'Live phone broadcast':'Calibrated top-down plan view';$('showPlan').disabled=!camera.calibrated}const healthRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data);renderCameraHealth(data);const setup=data.controller.setup||{};for(const key of ['diagnostics','endstops','homed','movement','magnet','centers']){const node=$('setup'+key[0].toUpperCase()+key.slice(1));node.textContent=setup[key]?'Passed':'Pending'}badge($('setupBadge'),setup.centers?'Commissioned':setup.last_error?'Failed':'In progress',setup.centers?'good':setup.last_error?'bad':'');$('setupResults').textContent=setup.last_error?`FAILED at ${setup.last_step}: ${setup.last_error}`:JSON.stringify(setup.results||{},null,2)}catch(error){toast(error.message,true)}};$('cameraTest').onclick=()=>act(async()=>{const value=await api('/api/camera/probe',{method:'POST',body:JSON.stringify({source:$('cameraSource').value.trim()})});toast(`Phone connected: ${value.result.width}×${value.result.height}, ${value.result.latency_ms} ms`)});$('showRaw').onclick=()=>{cameraViewMode='raw';refresh()};$('showPlan').onclick=()=>{cameraViewMode='plan';refresh()};const previousCalibrate=$('calibrate').onclick;$('calibrate').onclick=()=>{cameraViewMode='raw';previousCalibrate()};refresh();
</script><script>
let bridgeTimer=null;function bridgeBase(source){return source.replace(/^browser:/,'').replace(/\/$/,'')}function startBrowserBridge(){const source=$('cameraSource').value.trim(),base=bridgeBase(source),stream=$('phoneStream');if(!source.startsWith('browser:'))return;stream.src=`${base}/video?t=${Date.now()}`;stream.style.display='block';$('camera').style.display='none';if(bridgeTimer)clearInterval(bridgeTimer);bridgeTimer=setInterval(async()=>{if(!stream.naturalWidth)return;const rotation=Number($('rotation').value),canvas=$('cameraCanvas'),context=canvas.getContext('2d'),swap=rotation===90||rotation===270;canvas.width=swap?stream.naturalHeight:stream.naturalWidth;canvas.height=swap?stream.naturalWidth:stream.naturalHeight;context.save();if(rotation===90){context.translate(canvas.width,0);context.rotate(Math.PI/2)}else if(rotation===180){context.translate(canvas.width,canvas.height);context.rotate(Math.PI)}else if(rotation===270){context.translate(0,canvas.height);context.rotate(-Math.PI/2)}context.drawImage(stream,0,0,stream.naturalWidth,stream.naturalHeight);context.restore();canvas.toBlob(async blob=>{if(!blob)return;try{await fetch('/api/camera/browser-frame',{method:'POST',headers:{'Content-Type':'image/jpeg'},body:blob})}catch(error){}},'image/jpeg',.88)},1000)}const bridgeCameraStart=$('cameraStart').onclick;$('cameraStart').onclick=()=>{startBrowserBridge();bridgeCameraStart()};const bridgeCameraStop=$('cameraStop').onclick;$('cameraStop').onclick=()=>{if(bridgeTimer)clearInterval(bridgeTimer);bridgeTimer=null;$('phoneStream').src='';bridgeCameraStop()};const bridgeRaw=$('showRaw').onclick;$('showRaw').onclick=()=>{$('phoneStream').style.display='block';$('camera').style.display='none';bridgeRaw()};const bridgePlan=$('showPlan').onclick;$('showPlan').onclick=()=>{$('phoneStream').style.display='none';$('camera').style.display='block';bridgePlan()};const bridgeCalibrate=$('calibrate').onclick;$('calibrate').onclick=()=>{$('phoneStream').style.display='block';$('camera').style.display='none';bridgeCalibrate()};$('phoneStream').onclick=event=>$('camera').onclick(event);
</script></body></html>"""


CAMERA_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Sol Camera</title><style>body{margin:0;background:#05090b;color:#edf4f8;font-family:system-ui;padding:14px}header{display:flex;justify-content:space-between;align-items:center}img{width:100%;max-height:79vh;object-fit:contain;background:#000;border-radius:10px}pre{background:#101b1f;border:1px solid #263a41;padding:12px;max-height:14vh;overflow:auto;border-radius:9px}</style></head><body><header><h1>Phone camera</h1><strong>OpenAI Sol</strong></header><img id="camera"><pre id="status">Waiting…</pre><script>async function refresh(){const r=await fetch('/api/camera/status');const d=(await r.json()).camera;if(d.frames)document.getElementById('camera').src='/api/camera/frame?t='+Date.now();document.getElementById('status').textContent=JSON.stringify({status:d.status,stable:d.stable_observations,error:d.error,problems:d.problems,pieces:d.pieces},null,2)}refresh();setInterval(refresh,1000)</script></body></html>"""


class RequestHandler(BaseHTTPRequestHandler):
    controller: GantryController

    def _same_site(self) -> bool:
        return self.headers.get("Sec-Fetch-Site", "") in {
            "",
            "same-origin",
            "same-site",
            "none",
        }

    def _authenticated(self) -> bool:
        verifier = getattr(self.server, "clerk_verifier", None)
        if self.command not in SAFE_METHODS and not self._same_site():
            return False
        if verifier is None:
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get(SESSION_COOKIE)
        if token is None:
            return False
        try:
            verifier.verify(token.value)
            return True
        except GantryError:
            return False

    def _json(self, payload: Mapping[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html: str) -> None:
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 262_144:
            raise ValidationError("request is too large")
        value = json.loads(self.rfile.read(length)) if length else {}
        if not isinstance(value, dict):
            raise ValidationError("request body must be an object")
        return value

    def _raw_body(self, maximum: int) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum:
            raise ValidationError(f"request body must be between 1 and {maximum} bytes")
        return self.rfile.read(length)

    def _camera(self) -> SolVisionManager:
        return self.server.camera

    def _game(self) -> GameCoordinator:
        return self.server.game

    def _lichess(self) -> LichessOAuth:
        return self.server.lichess_oauth

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/auth/lichess/callback":
            query = parse_qs(parsed.query)
            try:
                if query.get("error"):
                    raise ValidationError(
                        query.get("error_description", query["error"])[0]
                    )
                self._lichess().complete(
                    code=query.get("code", [""])[0],
                    state=query.get("state", [""])[0],
                )
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
            except GantryError as exc:
                self._html(
                    f"<!doctype html><title>Lichess connection failed</title><h1>Connection failed</h1><p>{str(exc)}</p><p><a href='/'>Return to Chess Gantry</a></p>"
                )
            return
        if parsed.path == "/":
            self._html(self.server.dashboard_html)
            return
        if not self._authenticated():
            self._json({"ok": False, "error": "authentication required"}, 401)
            return
        if parsed.path == "/camera":
            self._html(CAMERA_HTML)
        elif self.path == "/api/full-status":
            try:
                camera = self._camera().status()
                game = self._game().status()
                pending = self.controller.pending_transaction()
                self._json(
                    {
                        "ok": True,
                        "controller": self.controller.status(),
                        "camera": camera,
                        "game": game,
                        "board": self.controller.board_state(),
                        "pending": pending,
                        "capabilities": {
                            "openai": bool(
                                os.environ.get("OPENAI_API_KEY", "").strip()
                            ),
                            "lichess": self._lichess().token() is not None,
                            "demo": self.controller.demo,
                            "capture_storage": self.controller.config.capture.enabled,
                        },
                        "lichess": self._lichess().status(),
                        "ports": [
                            value.as_dict()
                            for value in discover_serial_ports()
                            if value.likely_printer
                        ],
                    }
                )
            except GantryError as exc:
                self._json({"ok": False, "error": str(exc)}, 409)
        elif self.path == "/api/camera/status":
            self._json({"ok": True, "camera": self._camera().status()})
        elif self.path == "/api/camera/frame":
            try:
                body = self._camera().preview_jpeg()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except GantryError as exc:
                self._json({"ok": False, "error": str(exc)}, 409)
        elif self.path == "/api/camera/raw-frame":
            try:
                body = self._camera().raw_preview_jpeg()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except GantryError as exc:
                self._json({"ok": False, "error": str(exc)}, 409)
        elif self.path == "/api/camera/stream":
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=gantryframe"
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_frame = None
            try:
                while True:
                    status = self._camera().status()
                    if status["last_frame_at"] != last_frame:
                        body = self._camera().raw_preview_jpeg()
                        self.wfile.write(b"--gantryframe\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(
                            f"Content-Length: {len(body)}\r\n\r\n".encode()
                        )
                        self.wfile.write(body)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                        last_frame = status["last_frame_at"]
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError, GantryError):
                return
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authenticated():
            self._json({"ok": False, "error": "authentication required"}, 401)
            return
        try:
            if self.path == "/api/camera/browser-frame":
                result = self._camera().ingest_browser_jpeg(self._raw_body(8_000_000))
                self._json({"ok": True, "result": result})
                return
            payload = self._payload()
            if self.path == "/api/controller/connect":
                baudrate = payload.get("baudrate")
                result = self.controller.connect(
                    port=str(payload.get("port", "")).strip() or None,
                    baudrate=int(baudrate) if baudrate not in {None, ""} else None,
                )
            elif self.path == "/api/controller/disconnect":
                result = self.controller.disconnect()
            elif self.path == "/api/controller/home":
                result = self.controller.home_xy()
            elif self.path == "/api/controller/stop":
                result = self.controller.emergency_stop()
            elif self.path == "/api/setup/diagnostics":
                result = self.controller.run_setup_diagnostics()
            elif self.path == "/api/setup/endstops":
                result = self.controller.verify_setup_endstops()
            elif self.path == "/api/setup/home":
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before homing")
                result = self.controller.home_xy()
            elif self.path == "/api/setup/movement":
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before movement test")
                result = self.controller.run_setup_movement_test()
            elif self.path == "/api/setup/magnet":
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before magnet test")
                result = self.controller.run_setup_magnet_test()
            elif self.path == "/api/setup/centers":
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before center test")
                result = self.controller.run_setup_square_centers()
            elif self.path == "/api/setup/combined":
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before combined setup")
                result = self.controller.run_setup_combined()
            elif self.path == "/api/camera/configure":
                result = self._camera().configure(
                    source=str(payload.get("source", "")),
                    enabled=payload.get("enabled") is True,
                    orientation=payload.get("orientation"),
                    rotation=payload.get("rotation"),
                )
            elif self.path == "/api/camera/probe":
                result = self._camera().probe(str(payload.get("source", "")))
            elif self.path == "/api/camera/calibrate":
                result = self._camera().calibrate(payload.get("corners"))
            elif self.path == "/api/camera/calibration/clear":
                result = self._camera().clear_calibration()
            elif self.path == "/api/game/start":
                if self.controller.connected:
                    self.controller.disconnect()
                mode = str(payload.get("mode", ""))
                if mode != "mirror":
                    status = self._camera().status()
                    self._camera().configure(
                        source=status["source"],
                        enabled=True,
                        orientation=status["orientation"],
                        rotation=status["rotation"],
                    )
                result = self._game().start(
                    mode=mode,
                    game_id=payload.get("game_id"),
                    local_color=payload.get("local_color"),
                    confirm_motion=payload.get("confirm_motion") is True,
                    serial_port=str(payload.get("serial_port", "")).strip() or None,
                    serial_baudrate=(
                        int(payload["serial_baudrate"])
                        if payload.get("serial_baudrate") not in {None, ""}
                        else None
                    ),
                )
            elif self.path == "/api/game/stop":
                result = self._game().stop()
            elif self.path == "/api/reconcile/apply":
                if payload.get("confirmation") != "MOVE COMPLETED":
                    raise ValidationError(
                        "type MOVE COMPLETED to apply the pending move"
                    )
                result = self.controller.service.reconcile_mark_applied().to_dict()
            elif self.path == "/api/reconcile/discard":
                if payload.get("confirmation") != "MOVE DID NOT HAPPEN":
                    raise ValidationError(
                        "type MOVE DID NOT HAPPEN to discard the pending move"
                    )
                self.controller.service.reconcile_discard()
                result = {"discarded": True}
            elif self.path == "/api/lichess/oauth/start":
                host = self.headers.get("Host", "127.0.0.1:8000")
                redirect = f"http://{host}/auth/lichess/callback"
                result = {"url": self._lichess().begin(redirect)}
            elif self.path == "/api/lichess/validate":
                result = self._lichess().validate()
            elif self.path == "/api/lichess/disconnect":
                self._lichess().disconnect()
                result = self._lichess().status()
            else:
                self._json({"ok": False, "error": "not found"}, 404)
                return
            self._json({"ok": True, "result": result})
        except GantryError as exc:
            self._json({"ok": False, "error": str(exc)}, 409)
        except Exception as exc:
            self._json({"ok": False, "error": f"unexpected server error: {exc}"}, 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {fmt % args}")


class GantryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def web_clerk_settings(
    host: str,
    *,
    allow_network: bool,
    require_clerk: bool,
    clerk: Optional[ClerkSettings],
) -> Optional[ClerkSettings]:
    settings = clerk
    if require_clerk and settings is None:
        settings = ClerkSettings.require_from_environment()
    if host not in LOOPBACK_HOSTS and settings is None and not allow_network:
        raise ValidationError(
            "network dashboard requires --allow-network or --require-clerk"
        )
    return settings


def run_web_server(
    *,
    config: AppConfig,
    state_path: str,
    journal_path: str,
    audit_path: str,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
    demo: bool = False,
    clerk: Optional[ClerkSettings] = None,
    allow_network: bool = False,
    require_clerk: bool = False,
) -> None:
    settings = web_clerk_settings(
        host,
        allow_network=allow_network,
        require_clerk=require_clerk,
        clerk=clerk,
    )
    service = GantryService(config, state_path, journal_path, audit_path)
    if not service.store.path.exists():
        service.store.initialize(BoardState.standard(), overwrite=True)
    controller = GantryController(config, service, demo=demo)
    RequestHandler.controller = controller
    source = os.environ.get("CHESS_GANTRY_CAMERA_SOURCE", DEFAULT_PHONE_SOURCE)
    root = Path.cwd().resolve()
    camera = SolVisionManager(
        source=source,
        geometry=config.board,
        calibration_path=root / "data" / "camera_calibration.json",
    )
    oauth = LichessOAuth()
    game = GameCoordinator(root, config, camera, demo=demo, token_provider=oauth.token)
    try:
        server = GantryHTTPServer((host, port), RequestHandler)
    except OSError as exc:
        camera.close()
        controller.disconnect()
        raise web_bind_error(host, port, exc) from exc
    server.camera = camera
    server.game = game
    server.lichess_oauth = oauth
    server.clerk_verifier = ClerkVerifier(settings) if settings else None
    server.dashboard_html = render_dashboard(HTML, settings) if settings else HTML
    url = f"http://{host}:{port}"
    print(f"Chess Gantry running at {url}")
    print(f"Default phone camera: {source}")
    print("Press Control-C to stop it.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if game.running():
            game.stop()
        camera.close()
        controller.disconnect()
        server.server_close()
