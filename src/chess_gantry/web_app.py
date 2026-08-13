from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional
import json
import os
import socket
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlsplit

from .clerk_auth import SESSION_COOKIE, ClerkSettings, ClerkVerifier, render_dashboard
from .ai_arena import AIArena
from .commissioning import CONFIRMATION, CommissioningStore
from .config import AppConfig
from .controller import GantryController
from .errors import ConfigurationError, GantryError, ValidationError
from .game_coordinator import GameCoordinator
from .lichess_oauth import LichessOAuth
from .local_vision import generate_reference_markers
from .models import BoardState
from .openai_opponent import ClaudeChessOpponent, SolChessOpponent
from .serial_link import discover_serial_ports
from .service import GantryService
from .station_game import qr_svg
from .lichess_station import LichessStation, STATION_CONFIRMATION
from .station_web import STATION_HTML
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


def station_public_url(host: str, port: int) -> str:
    configured = os.environ.get("CHESS_GANTRY_PUBLIC_URL", "").strip().rstrip("/")
    if not configured:
        public_host = os.environ.get("CHESS_GANTRY_PUBLIC_HOST", "").strip()
        if public_host:
            configured = f"http://{public_host}"
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError(
                "CHESS_GANTRY_PUBLIC_URL must be a complete HTTP or HTTPS origin"
            )
        return configured
    resolved = host
    if host in {"0.0.0.0", "::"}:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))
            resolved = str(probe.getsockname()[0])
        except OSError:
            resolved = socket.gethostbyname(socket.gethostname())
        finally:
            probe.close()
    return f"http://{resolved}:{port}"


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Chess Gantry</title><style>
:root{color-scheme:dark;--ink:#edf4f8;--muted:#91a2ae;--ground:#071013;--panel:#0d191d;--raised:#132329;--line:#22373e;--mint:#72efc1;--amber:#ffc968;--red:#ff6677;--blue:#77b9ff;--shadow:0 20px 60px #0008;font-family:"IBM Plex Sans",Inter,system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;background:linear-gradient(145deg,#0b191d 0,#061013 48%,#080c10 100%);color:var(--ink);min-height:100vh}button,input,select{font:inherit}button{cursor:pointer}button:disabled{cursor:not-allowed;opacity:.38}.shell{max-width:1460px;margin:auto;padding:24px}.mast{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;padding:8px 0 23px;border-bottom:1px solid var(--line)}.eyebrow{color:var(--mint);font:700 .72rem/1 monospace;letter-spacing:.16em;text-transform:uppercase}.mast h1{font-size:clamp(2.2rem,5vw,4.7rem);letter-spacing:-.07em;line-height:.9;margin:10px 0}.mast p{margin:0;color:var(--muted);max-width:650px}.master-status{display:flex;align-items:center;gap:9px;border:1px solid var(--line);background:#101e22;padding:10px 14px;border-radius:999px;white-space:nowrap}.dot{width:9px;height:9px;background:var(--amber);border-radius:50%;box-shadow:0 0 18px currentColor}.dot.good{background:var(--mint)}.dot.bad{background:var(--red)}.readiness{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin:18px 0}.ready-item{background:#0d191d;padding:13px 16px}.ready-item span{display:block;color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.1em}.ready-item strong{display:block;margin-top:5px}.blocker{display:none;grid-template-columns:1fr auto;gap:16px;align-items:center;background:#2a1719;border:1px solid #74313b;border-radius:14px;padding:16px;margin-bottom:18px}.blocker.show{display:grid}.blocker h2{margin:0;color:#ffbdc4}.blocker p{margin:6px 0 0;color:#eab5bb}.layout{display:grid;grid-template-columns:340px minmax(0,1fr);gap:18px}.rail{display:flex;flex-direction:column;gap:14px}.card{background:linear-gradient(180deg,#101e22,#0b161a);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow)}.card-pad{padding:17px}.section-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:15px}.step{color:var(--mint);font:700 .65rem/1 monospace;letter-spacing:.14em;text-transform:uppercase}.section-head h2{font-size:1.05rem;margin:5px 0 0}.badge{font:700 .72rem/1 monospace;padding:7px 8px;border:1px solid var(--line);border-radius:8px;color:var(--muted)}.badge.good{color:var(--mint);border-color:#28644f}.badge.bad{color:#ff9baa;border-color:#70303a}.field{margin-top:11px}.field label{display:block;color:var(--muted);font-size:.75rem;margin:0 0 6px}.control{width:100%;background:#14252a;color:var(--ink);border:1px solid #2a424a;border-radius:9px;padding:10px 11px}.control:focus{outline:2px solid #46cfa0;outline-offset:1px}.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.btn{border:1px solid #2c464f;background:#172a30;color:var(--ink);border-radius:9px;padding:10px 12px;font-weight:750}.btn.primary{background:var(--mint);border-color:var(--mint);color:#06120e}.btn.danger{background:#411a21;border-color:#76313b;color:#ffd5da}.btn.ghost{background:transparent}.hint{font-size:.75rem;color:var(--muted);line-height:1.45;margin:10px 0 0}.workspace{min-width:0;display:flex;flex-direction:column;gap:18px}.vision-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(330px,.65fr);gap:1px;background:var(--line);border-radius:16px;overflow:hidden;border:1px solid var(--line)}.vision-pane,.board-pane{background:#0a1417;padding:16px;min-width:0}.pane-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:11px}.pane-title h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.1em;margin:0;color:#b7c6ce}.camera-wrap{position:relative;background:#030708;border-radius:11px;overflow:hidden;aspect-ratio:16/9;display:grid;place-items:center}.camera{width:100%;height:100%;object-fit:contain}.camera-empty{position:absolute;color:#6f818a;text-align:center;padding:30px;pointer-events:none}.camera-empty strong{display:block;color:#a7b6bd;margin-bottom:5px}.scan{position:absolute;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--mint),transparent);box-shadow:0 0 12px var(--mint);animation:scan 2.8s linear infinite;opacity:0}.scanning .scan{opacity:.8}@keyframes scan{from{top:8%}to{top:92%}}.camera-note{padding:10px 2px 0;color:var(--muted);font-size:.76rem;min-height:28px}.board-shell{width:min(100%,560px);margin:auto;display:grid;grid-template-columns:20px 1fr;grid-template-rows:1fr 20px}.ranks,.files{color:#80919a;font:600 .65rem/1 monospace}.ranks{display:grid;grid-template-rows:repeat(8,1fr);place-items:center}.files{display:grid;grid-template-columns:repeat(8,1fr);place-items:center}.board{display:grid;grid-template-columns:repeat(8,1fr);aspect-ratio:1;border:2px solid #30464e;box-shadow:0 16px 40px #0008}.square{display:grid;place-items:center;font-size:clamp(1.6rem,4vw,3.5rem);line-height:1}.light{background:#c0d1d1;color:#152426}.dark{background:#527780;color:#f3f9f8}.unknown{color:#8f2f3a}.status-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stat{background:#102025;border:1px solid var(--line);border-radius:11px;padding:13px}.stat span{display:block;color:var(--muted);font-size:.7rem;text-transform:uppercase;letter-spacing:.09em}.stat strong{display:block;margin-top:7px;overflow-wrap:anywhere}.game-strip{display:grid;grid-template-columns:1fr 1fr;gap:18px}.summary{display:grid;grid-template-columns:repeat(2,1fr);gap:9px}.summary div{background:#122228;border:1px solid var(--line);border-radius:10px;padding:12px}.summary small{color:var(--muted);display:block}.summary strong{display:block;margin-top:5px}.piece-list{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;max-height:220px;overflow:auto}.piece{background:#122228;border-left:3px solid var(--blue);border-radius:7px;padding:8px;font:600 .7rem/1.35 monospace}.logs{margin:0;background:#050b0d;border:1px solid #17282d;border-radius:10px;padding:13px;white-space:pre-wrap;min-height:140px;max-height:280px;overflow:auto;color:#a9bac2;font:12px/1.55 monospace}.recovery{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:end}.recovery input{min-width:230px}.toast{position:fixed;right:22px;bottom:22px;max-width:440px;background:#18282d;border:1px solid #3a5660;box-shadow:var(--shadow);border-radius:12px;padding:13px 15px;transform:translateY(30px);opacity:0;pointer-events:none;transition:.2s}.toast.show{transform:none;opacity:1}.toast.error{border-color:#7c3540;color:#ffd1d7}@media(max-width:1040px){.layout{grid-template-columns:1fr}.rail{display:grid;grid-template-columns:repeat(2,1fr)}.vision-grid{grid-template-columns:1fr}.game-strip{grid-template-columns:1fr}}@media(max-width:680px){.shell{padding:14px}.mast{align-items:flex-start;flex-direction:column}.readiness{grid-template-columns:1fr 1fr}.rail{display:flex}.status-grid{grid-template-columns:1fr 1fr}.piece-list{grid-template-columns:1fr 1fr}.blocker,.recovery{grid-template-columns:1fr}.row .btn{flex:1}.vision-pane,.board-pane{padding:10px}}
.square{position:relative;font-family:"Noto Sans Symbols 2","DejaVu Sans",serif;user-select:none}.square .piece-glyph{filter:drop-shadow(0 2px 1px #0006);transform:translateY(-1%)}.square.last-from{box-shadow:inset 0 0 0 5px #ffc968aa}.square.last-to{box-shadow:inset 0 0 0 5px #72efc1}.square.in-check{background:#d45866!important;animation:checkpulse 1.2s ease-in-out infinite alternate}.square::after{content:attr(data-square);position:absolute;right:4px;bottom:3px;font:700 .55rem/1 monospace;opacity:.45}.move-history{display:grid;grid-template-columns:2.4rem 1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;max-height:260px;overflow-y:auto}.move-history>*{background:#102025;padding:8px}.move-history .number{color:var(--muted);text-align:right}.move-pill{display:flex;justify-content:space-between;gap:6px}.move-pill small{color:var(--muted)}.ai-insight{margin-top:12px;border:1px solid #315a76;background:#0e202d;border-radius:11px;padding:13px}.ai-insight strong{color:var(--blue)}.ai-insight p{margin:7px 0}.ai-insight small{color:var(--muted)}@keyframes checkpulse{from{box-shadow:inset 0 0 0 3px #ff93a0}to{box-shadow:inset 0 0 24px #ff263f}}@media(max-width:680px){.square::after{font-size:.48rem}.move-history{grid-template-columns:2rem 1fr 1fr}}</style></head><body><main class="shell"><header class="mast"><div><div class="eyebrow">Physical chess control system</div><h1>Chess Gantry</h1><p>Frame the board. Confirm Sol. Start the game. Remote moves are planned, journaled, and acknowledged by Marlin.</p></div><div class="master-status"><i id="masterDot" class="dot"></i><strong id="masterText">Loading system</strong></div></header>
<section class="readiness"><div class="ready-item"><span>Commissioning</span><strong id="readyCommissioning">Checking</strong></div><div class="ready-item"><span>Phone camera</span><strong id="readyCamera">Checking</strong></div><div class="ready-item"><span>Fast vision</span><strong id="readySol">Checking</strong></div><div class="ready-item"><span>Motion</span><strong id="readyMotion">Checking</strong></div><div class="ready-item"><span>Lichess</span><strong id="readyLichess">Checking</strong></div></section>
<section id="blocker" class="blocker"><div><h2>Physical recovery required</h2><p id="blockerText">A pending transaction blocks games and movement.</p></div><button id="recoveryFocus" class="btn danger">Resolve below</button></section>
<div class="layout"><aside class="rail">
<section class="card card-pad"><div class="section-head"><div><div class="step">Step 01</div><h2>Phone and Sol</h2></div><span id="cameraBadge" class="badge">Idle</span></div><div class="field"><label for="cameraSource">Camera source</label><input id="cameraSource" class="control" value="auto:http://192.168.100.88:8080"></div><div class="field"><label for="rotation">Rotate phone image</label><select id="rotation" class="control"><option value="0">0°</option><option value="90">90° clockwise</option><option value="180">180°</option><option value="270">90° counterclockwise</option></select></div><div class="field"><label for="orientation">Board orientation after rotation</label><select id="orientation" class="control"><option value="white_bottom">White nearest bottom</option><option value="black_bottom">Black nearest bottom</option></select></div><div class="row"><button id="cameraTest" class="btn">Test phone</button><button id="cameraStart" class="btn primary">Start recognition</button><button id="calibrate" class="btn">Calibrate 4 corners</button><button id="clearCalibration" class="btn ghost">Clear crop</button><button id="cameraPopout" class="btn">Pop out</button><button id="cameraStop" class="btn ghost">Stop</button></div><p id="calibrationHint" class="hint">Start the phone camera server, then test. For plan view click: top-left, top-right, bottom-right, bottom-left.</p></section>
<section class="card card-pad"><div class="section-head"><div><div class="step">Fast vision</div><h2>ArUco + color caps</h2></div><span id="fastVisionBadge" class="badge">Needs setup</span></div><div class="row"><button id="generateMarkers" class="btn">Generate 4 references</button><button id="arucoCalibrate" class="btn">Auto-calibrate ArUco</button></div><div id="markerDownloads" class="hint"></div><div class="field"><label for="pieceColor">Click a cap in Plan view to sample</label><select id="pieceColor" class="control"><option value="green">Green · pawns</option><option value="blue">Blue · bishops</option><option value="brown">Brown · rooks</option><option value="pink">Pink · knights</option><option value="yellow">Yellow · kings</option><option value="orange">Orange · queens</option></select></div><button id="sampleColor" class="btn primary">Sample selected color</button><div id="colorStatus" class="piece-list" style="margin-top:10px"></div><div class="summary" style="margin-top:10px"><div><small>Mode</small><strong id="detectionMode">Waiting</strong></div><div><small>Confidence</small><strong id="localConfidence">0%</strong></div><div><small>Stable frames</small><strong id="localStable">0 / 3</strong></div><div><small>Unresolved</small><strong id="localUnresolved">0</strong></div></div><p class="hint">Local OpenCV detects moves immediately. Sol runs only when local colors are incomplete or ambiguous.</p></section><section class="card card-pad"><div class="section-head"><div><div class="step">Step 02</div><h2>Motion controller</h2></div><span id="motionBadge" class="badge">Offline</span></div><div class="field"><label for="serialPort">Serial port</label><select id="serialPort" class="control"><option value="">Auto-detect USB Marlin</option></select></div><div class="field"><label for="baudrate">Baud rate</label><select id="baudrate" class="control"><option value="">Auto: 115200 then 250000</option><option value="115200">115200</option><option value="250000">250000</option></select></div><div class="summary"><div><small>Active port</small><strong id="port">—</strong></div><div><small>Homed</small><strong id="homed">No</strong></div><div><small>Firmware</small><strong id="firmware">—</strong></div><div><small>Revision</small><strong id="revision">0</strong></div></div><div class="row"><button id="scanPorts" class="btn">Scan ports</button><button id="connect" class="btn primary">Connect</button><button id="home" class="btn">Home XYZ</button><button id="disconnect" class="btn ghost">Disconnect</button></div><button id="stop" class="btn danger" style="width:100%;margin-top:9px">Emergency stop</button><p id="serialHint" class="hint">Use Scan ports after reconnecting USB. The connector suppresses DTR/RTS reset and waits for CH340 re-enumeration.</p></section>
<section class="card card-pad"><div class="section-head"><div><div class="step">Step 03</div><h2>Game mode</h2></div><span id="gameBadge" class="badge">Idle</span></div><div id="lichessAccount" class="summary"><div><small>Lichess account</small><strong id="lichessUser">Not connected</strong></div><div><small>Scope</small><strong id="lichessScope">board:play</strong></div></div><div class="row"><button id="lichessConnect" class="btn">Connect Lichess</button><button id="lichessDisconnect" class="btn ghost">Forget token</button></div><div class="field"><label for="mode">Who is playing?</label><select id="mode" class="control"><option value="local">Two people on this board</option><option value="openai">Play against OpenAI Sol</option><option value="lichess">Camera player vs Lichess / AI</option><option value="mirror">Mirror two remote / AI players</option></select></div><div id="lichessFields"><div class="field"><label for="gameId">Lichess game ID</label><input id="gameId" class="control" value="J21i9aA4" maxlength="12"></div><div class="row"><button id="lichessValidateGame" class="btn">Validate game</button></div><p id="lichessGameStatus" class="hint">Validate a fresh game before starting.</p><div id="colorField" class="field"><label for="localColor">Camera player controls</label><select id="localColor" class="control"><option value="white">White</option><option value="black">Black</option></select></div><div id="openaiFields" class="field"><label for="opponentStyle">OpenAI playing style</label><select id="opponentStyle" class="control"><option value="balanced">Balanced</option><option value="aggressive">Aggressive</option><option value="positional">Positional</option><option value="creative">Creative</option></select></div></div><div class="row"><button id="gameStart" class="btn primary">Start full game</button><button id="gameStop" class="btn ghost">Stop game</button></div><p id="gameHint" class="hint">Both people move pieces manually. Sol records legal settled positions.</p></section>
</aside><div class="workspace">
<section class="vision-grid"><div class="vision-pane"><div class="pane-title"><h2 id="cameraViewTitle">Raw phone frame</h2><span id="frameMeta" class="badge">No frame</span></div><div class="row"><button id="showRaw" class="btn">Raw frame</button><button id="showPlan" class="btn">Plan view</button></div><div id="cameraWrap" class="camera-wrap"><img id="camera" class="camera" alt="Proxied phone camera and processed plan view"><div id="cameraEmpty" class="camera-empty"><strong>Waiting for camera</strong>Start the phone camera app, then press Test phone.</div><i class="scan"></i></div><div id="cameraNote" class="camera-note">The phone is connected only while fresh frames arrive.</div></div><div class="board-pane"><div class="pane-title"><h2>Board analysis</h2><div class="row"><button id="flipBoard" class="btn ghost">Flip board</button><span id="transcriptionBadge" class="badge">Idle</span></div></div><div class="board-shell"><div id="rankLabels" class="ranks"><span>8</span><span>7</span><span>6</span><span>5</span><span>4</span><span>3</span><span>2</span><span>1</span></div><div id="board" class="board"></div><div></div><div id="fileLabels" class="files"><span>a</span><span>b</span><span>c</span><span>d</span><span>e</span><span>f</span><span>g</span><span>h</span></div></div><div class="summary" style="margin-top:12px"><div><small>Position source</small><strong id="boardSource">Camera</strong></div><div><small>Legal moves</small><strong id="legalMoves">0</strong></div><div><small>Game state</small><strong id="boardGameState">Waiting</strong></div><div><small>View</small><strong id="boardView">White</strong></div></div><pre id="fenDisplay" class="logs" style="min-height:48px;max-height:80px">No FEN yet.</pre></div></section>
<section class="status-grid"><div class="stat"><span>Sol state</span><strong id="cameraState">Idle</strong></div><div class="stat"><span>Stable observations</span><strong id="stable">0 / 2</strong></div><div class="stat"><span>Turn</span><strong id="turn">—</strong></div><div class="stat"><span>Last move</span><strong id="lastMove">—</strong></div></section>
<section class="game-strip"><div class="card card-pad"><div class="section-head"><div><div class="step">Detected board</div><h2>Pieces</h2></div><span id="pieceCount" class="badge">0 pieces</span></div><div id="pieces" class="piece-list"><div class="piece">No complete position yet.</div></div></div><div class="card card-pad"><div class="section-head"><div><div class="step">Game coordinator</div><h2>Current game</h2></div><button id="analyzePosition" class="btn">Ask Sol for move</button></div><div class="summary"><div><small>State</small><strong id="gameState">Idle</strong></div><div><small>Mode</small><strong id="gameMode">—</strong></div><div><small>Confirmed ply</small><strong id="ply">0</strong></div><div><small>Physical moves</small><strong id="executed">0</strong></div></div><p id="gameError" class="hint"></p><div class="section-head" style="margin-top:16px"><div><div class="step">Moves</div><h2>Game score</h2></div></div><div id="moveHistory" class="move-history"><span class="hint">No moves yet.</span></div><div id="aiInsight" class="ai-insight" hidden><strong>OpenAI Sol</strong><p id="aiRationale"></p><small id="aiPlan"></small></div></div></section>
<section id="recovery" class="card card-pad"><div class="section-head"><div><div class="step">Physical safety</div><h2>Pending transaction recovery</h2></div><span id="pendingBadge" class="badge">Clear</span></div><p id="pendingSummary" class="hint">No pending physical transaction.</p><div class="recovery"><div class="field"><label for="confirmation">Type the exact phrase for the action</label><input id="confirmation" class="control" placeholder="MOVE COMPLETED or MOVE DID NOT HAPPEN"></div><div class="row"><button id="applyPending" class="btn danger">Move completed</button><button id="discardPending" class="btn">Move did not happen</button></div></div></section>
<section class="card card-pad"><div class="section-head"><div><div class="step">Activity</div><h2>Game log</h2></div><button id="refresh" class="btn ghost">Refresh</button></div><pre id="logs" class="logs">No events yet.</pre></section>
<details id="setupDetails" class="card card-pad"><summary class="section-head"><div><div class="step">Commissioning</div><h2>Hardware test matrix</h2></div><span id="setupBadge" class="badge">Not started</span></summary><div class="field"><label for="commissioningConfirmation">Commissioning attestation</label><input id="commissioningConfirmation" class="control" placeholder="I CONFIRM PHYSICAL SETUP IS SAFE"></div><div class="row"><button id="commissioningAttest" class="btn primary">Mark commissioned</button><button id="commissioningClear" class="btn ghost">Clear attestation</button></div><div class="summary"><div><small>Diagnostics</small><strong id="setupDiagnostics">Pending</strong></div><div><small>Endstops</small><strong id="setupEndstops">Pending</strong></div><div><small>Homing</small><strong id="setupHomed">Pending</strong></div><div><small>5 mm motion</small><strong id="setupMovement">Pending</strong></div><div><small>Magnet pulse</small><strong id="setupMagnet">Pending</strong></div><div><small>64 centers</small><strong id="setupCenters">Pending</strong></div></div><div class="field"><label for="setupConfirmation">Type SETUP AREA CLEAR before any motion test</label><input id="setupConfirmation" class="control" placeholder="SETUP AREA CLEAR"></div><div class="row"><button id="setupDiagnosticsButton" class="btn">1. Diagnostics</button><button id="setupEndstopsButton" class="btn">2. Endstops</button><button id="setupHomeButton" class="btn">3. Home</button><button id="setupMovementButton" class="btn">4. Move 5 mm</button><button id="setupMagnetButton" class="btn">5. Pulse magnet</button><button id="setupCentersButton" class="btn">6. Visit centers</button><button id="setupCombinedButton" class="btn primary">Run complete setup</button></div><pre id="setupResults" class="logs">Run diagnostics first. Physical steps stop on the first failure.</pre></section>
</div></details><section class="card card-pad"><div class="section-head"><div><div class="step">AI Arena</div><h2>Claude vs ChatGPT</h2></div><span id="arenaBadge" class="badge">Idle</span></div><div class="summary"><div><small>ChatGPT</small><strong id="chatgptReady">Checking</strong></div><div><small>Claude</small><strong id="claudeReady">Checking</strong></div><div><small>State</small><strong id="arenaState">Idle</strong></div><div><small>Result</small><strong id="arenaResult">—</strong></div></div><div class="fields"><div class="field"><label for="arenaWhite">White</label><select id="arenaWhite" class="control"><option value="chatgpt">ChatGPT</option><option value="claude">Claude</option></select></div><div class="field"><label for="arenaStyle">Style</label><select id="arenaStyle" class="control"><option value="balanced">Balanced</option><option value="aggressive">Aggressive</option><option value="positional">Positional</option><option value="creative">Creative</option></select></div><div class="field"><label for="arenaDelay">Seconds between moves</label><input id="arenaDelay" class="control" type="number" min="0" max="30" step="0.5" value="1"></div><div class="field"><label for="arenaPlies">Maximum plies</label><input id="arenaPlies" class="control" type="number" min="1" max="500" value="200"></div></div><label class="field"><input id="arenaPhysical" type="checkbox" checked> Execute normal Claude/ChatGPT moves physically</label><div class="row"><button id="arenaStart" class="btn primary">Start Claude vs ChatGPT</button><button id="arenaStop" class="btn ghost">Stop arena</button></div><div id="arenaManual" class="ai-insight" hidden><strong>Manual physical action required</strong><p id="arenaInstruction"></p><div class="field"><input id="arenaConfirmation" class="control" placeholder="AI MOVE COMPLETED"></div><button id="arenaConfirm" class="btn">Confirm physical move</button></div><div id="arenaHistory" class="move-history"><span class="hint">No arena moves yet.</span></div></section></div></div></main><div id="toast" class="toast"></div><script>
const $=id=>document.getElementById(id);let current=null,busy=false;const symbols={P:'♙',N:'♘',B:'♗',R:'♖',Q:'♕',K:'♔',p:'♟',n:'♞',b:'♝',r:'♜',q:'♛',k:'♚','.':'',x:'?', '?':'?'};async function api(path,options={}){const response=await fetch(path,{headers:{'Content-Type':'application/json'},...options});const body=await response.json();if(!response.ok||body.ok===false)throw new Error(body.error||`HTTP ${response.status}`);return body}function toast(message,error=false){const node=$('toast');node.textContent=message;node.className=`toast show${error?' error':''}`;setTimeout(()=>node.className='toast',3200)}async function act(fn){if(busy)return;busy=true;try{await fn();await refresh()}catch(error){toast(error.message,true)}finally{busy=false}}function badge(node,text,state=''){node.textContent=text;node.className=`badge ${state}`.trim()}function draw(rows){const root=$('board');root.innerHTML='';const values=rows||Array(8).fill('........');for(let row=0;row<8;row++)for(let column=0;column<8;column++){const value=values[row]?.[column]||'?';const cell=document.createElement('div');cell.className=`square ${(row+column)%2?'dark':'light'} ${value==='?'||value==='x'?'unknown':''}`;cell.textContent=symbols[value]??'?';root.appendChild(cell)}}function gameHelp(mode){$('lichessAccount').style.display=['lichess','mirror'].includes(mode)?'grid':'none';$('lichessFields').style.display=['lichess','mirror'].includes(mode)?'block':'none';$('colorField').style.display=['lichess','openai'].includes(mode)?'block':'none';$('openaiFields').style.display=mode==='openai'?'block':'none';$('gameHint').textContent=mode==='local'?'Both people move manually. Sol records each legal settled position.':mode==='openai'?'You move the selected side physically. OpenAI Sol chooses only from server-verified legal moves; its replies are executed by the gantry.':mode==='lichess'?'The camera player moves manually. Lichess opponent moves are executed by the gantry.':'Every move from the fresh Lichess game is executed physically.'}function render(data){current=data;const c=data.camera,g=data.game.status,s=data.controller,p=data.pending,cap=data.capabilities,li=data.lichess;const cameraConnected=c.frames>0;const localReady=c.status==='complete'&&c.stable_observations>=3&&c.local?.profiles_ready;const gameRunning=!['idle','stopped','failed'].includes(g.state);const mode=$('mode').value;$('readyCamera').textContent=cameraConnected?'Connected':'Not connected';$('readySol').textContent=localReady?`Local ready · ${c.local?.hz||0} Hz`:c.local?.profiles_ready?'Stabilizing':'Sample six colors';$('readyMotion').textContent=cap.demo?'Demo mode':s.connected?(s.homed?'Homed':'Connected'):'Disconnected';$('readyLichess').textContent=cap.lichess?'Token ready':'Token missing';const modeReady=mode==='mirror'?cap.lichess:mode==='openai'?localReady&&cap.openai:mode==='lichess'?localReady&&cap.lichess:localReady;const allReady=modeReady&&!p;const master=$('masterDot');master.className=`dot ${allReady?'good':p?'bad':''}`;$('masterText').textContent=p?'Recovery required':allReady?'Ready to start':!c.local?.profiles_ready?'Calibrate six cap colors':c.stable_observations<3?'Hold board still':mode==='openai'&&!cap.openai?'OpenAI key missing':mode!=='local'&&!cap.lichess&&mode!=='openai'?'Connect Lichess':'Complete mode requirements';$('blocker').classList.toggle('show',Boolean(p));$('blockerText').textContent=p?`Pending ${p.move?.position||'piece'} move from (${p.move?.px},${p.move?.py}) to (${p.move?.nx},${p.move?.ny}). Inspect the physical board before choosing.`:'';badge($('cameraBadge'),c.running?'Live':'Idle',c.running?'good':'');badge($('motionBadge'),s.connected?(s.homed?'Homed':'Connected'):'Offline',s.homed?'good':s.last_error?'bad':'');badge($('gameBadge'),g.state||'Idle',g.state==='failed'?'bad':gameRunning?'good':'');badge($('transcriptionBadge'),c.status||'Idle',c.status==='complete'?'good':c.status==='not_found'||c.status==='unusable'?'bad':'');badge($('pendingBadge'),p?'Blocked':'Clear',p?'bad':'good');$('cameraSource').value=c.source;$('orientation').value=c.orientation;$('rotation').value=String(c.rotation||0);$('cameraState').textContent=c.status||'idle';$('stable').textContent=`${c.stable_observations} / 2`;$('turn').textContent=c.turn||'—';$('lastMove').textContent=c.last_move||'—';$('frameMeta').textContent=cameraConnected?`${c.frames} frame${c.frames===1?'':'s'} · ${c.rotation||0}°`:'No frame';$('cameraWrap').classList.toggle('scanning',c.running&&Boolean(cap.openai));$('cameraEmpty').style.display=cameraConnected?'none':'block';const issue=c.error||(c.problems||[]).join(', ');$('cameraNote').textContent=issue||'Frame connected. Sol checks the newest image every three seconds.';draw(c.rows);$('pieceCount').textContent=`${(c.pieces||[]).length} pieces`;$('pieces').innerHTML=(c.pieces||[]).map(piece=>`<div class="piece">${piece.square} · ${piece.color} ${piece.type}</div>`).join('')||'<div class="piece">No complete position yet.</div>';$('port').textContent=s.port||'—';$('homed').textContent=s.homed?'Yes':'No';$('firmware').textContent=s.firmware||'—';$('revision').textContent=s.board_revision??'—';$('gameState').textContent=g.state||'idle';$('gameMode').textContent=g.mode||'—';$('ply').textContent=g.confirmed_ply??0;$('executed').textContent=g.executed_count??0;$('gameError').textContent=g.error||'No game error.';$('logs').textContent=data.game.logs||'No events yet.';$('pendingSummary').textContent=p?JSON.stringify({event:p.move?.event_id,piece:p.move?.position,from:[p.move?.px,p.move?.py],to:[p.move?.nx,p.move?.ny],created:p.created_at},null,2):'No pending physical transaction.';$('connect').disabled=s.connected||gameRunning;$('disconnect').disabled=!s.connected||gameRunning;$('home').disabled=!s.connected||gameRunning;$('gameStart').disabled=gameRunning||Boolean(p)||!modeReady;$('gameStop').disabled=!gameRunning;$('applyPending').disabled=!p;$('discardPending').disabled=!p;$('cameraStart').disabled=c.running;$('cameraStop').disabled=!c.enabled}async function refresh(){try{render(await api('/api/full-status'))}catch(error){toast(error.message,true)}}$('mode').onchange=()=>{gameHelp($('mode').value);if(current)render(current)};$('recoveryFocus').onclick=()=>$('recovery').scrollIntoView({behavior:'smooth'});$('refresh').onclick=refresh;$('cameraPopout').onclick=()=>window.open('/camera','gantryCamera','width=1100,height=850');$('connect').onclick=()=>act(()=>api('/api/controller/connect',{method:'POST',body:'{}'}));$('disconnect').onclick=()=>act(()=>api('/api/controller/disconnect',{method:'POST',body:'{}'}));$('home').onclick=()=>act(()=>api('/api/controller/home',{method:'POST',body:'{}'}));$('stop').onclick=()=>act(()=>api('/api/controller/stop',{method:'POST',body:'{}'}));$('cameraStart').onclick=()=>act(()=>api('/api/camera/configure',{method:'POST',body:JSON.stringify({source:$('cameraSource').value.trim(),orientation:$('orientation').value,rotation:Number($('rotation').value),enabled:true})}));$('cameraStop').onclick=()=>act(()=>api('/api/camera/configure',{method:'POST',body:JSON.stringify({source:$('cameraSource').value.trim(),orientation:$('orientation').value,rotation:Number($('rotation').value),enabled:false})}));$('gameStart').onclick=()=>act(()=>api('/api/game/start',{method:'POST',body:JSON.stringify({mode:$('mode').value,game_id:$('gameId').value.trim()||null,local_color:$('localColor').value,confirm_motion:true,serial_port:$('serialPort').value,serial_baudrate:$('baudrate').value,opponent_style:$('opponentStyle').value})}));$('gameStop').onclick=()=>act(()=>api('/api/game/stop',{method:'POST',body:'{}'}));$('applyPending').onclick=()=>act(()=>api('/api/reconcile/apply',{method:'POST',body:JSON.stringify({confirmation:$('confirmation').value})}));$('discardPending').onclick=()=>act(()=>api('/api/reconcile/discard',{method:'POST',body:JSON.stringify({confirmation:$('confirmation').value})}));gameHelp('local');draw(null);refresh();setInterval(refresh,1000);
</script><script>
let enhancedPortSelection='',calibrationClicks=[],calibrationActive=false;function updateEnhancements(data){const li=data.lichess||{},ports=data.ports||[],camera=data.camera||{};$('lichessUser').textContent=li.username||'Not connected';$('lichessScope').textContent=(li.scopes||[]).join(' ')||'board:play';$('lichessConnect').disabled=Boolean(li.connected);$('lichessDisconnect').disabled=!li.connected;const select=$('serialPort');const selected=select.value||enhancedPortSelection;if(document.activeElement!==select){select.innerHTML='<option value="">Auto-detect USB Marlin</option>'+ports.map(port=>`<option value="${port.device}">${port.device} · ${port.description}</option>`).join('');if([...select.options].some(option=>option.value===selected))select.value=selected}const age=camera.last_frame_at?Math.max(0,Date.now()/1000-camera.last_frame_at):null;if(age!==null)$('frameMeta').textContent=`${camera.frames} frames · ${age.toFixed(1)}s old · ${camera.rotation||0}°`;$('serialHint').textContent=data.controller.last_error||'Use Scan ports after reconnecting USB. The connector suppresses DTR/RTS reset and waits for CH340 re-enumeration.';$('calibrationHint').textContent=camera.calibrated?'Board crop calibrated. Sol receives a square top-down 1024×1024 board.':'For flawless square mapping, click: top-left, top-right, bottom-right, bottom-left.';const pieces=camera.pieces||[];if(pieces.length)$('pieces').innerHTML=pieces.map(piece=>{const machine=piece.machine_mm?` · machine ${piece.machine_mm.x.toFixed(1)},${piece.machine_mm.y.toFixed(1)} mm`:'';return `<div class="piece">${piece.square} · grid ${piece.grid.x},${piece.grid.y}${machine}<br>${piece.color} ${piece.type}</div>`}).join('')}
function normalizedImageClick(event){const image=event.target,rect=image.getBoundingClientRect(),naturalRatio=image.naturalWidth/image.naturalHeight,boxRatio=rect.width/rect.height;let width=rect.width,height=rect.height,left=rect.left,top=rect.top;if(naturalRatio>boxRatio){height=rect.width/naturalRatio;top+=((rect.height-height)/2)}else{width=rect.height*naturalRatio;left+=((rect.width-width)/2)}const x=(event.clientX-left)/width,y=(event.clientY-top)/height;if(x<0||x>1||y<0||y>1)throw new Error('Click the visible camera image, not the letterbox area.');return{x,y}}const originalRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data)}catch(error){toast(error.message,true)}};$('scanPorts').onclick=refresh;$('serialPort').onchange=()=>enhancedPortSelection=$('serialPort').value;$('connect').onclick=()=>act(()=>api('/api/controller/connect',{method:'POST',body:JSON.stringify({port:$('serialPort').value,baudrate:$('baudrate').value})}));$('lichessConnect').onclick=()=>act(async()=>{const value=await api('/api/lichess/oauth/start',{method:'POST',body:'{}'});window.location.assign(value.result.url)});$('lichessDisconnect').onclick=()=>act(()=>api('/api/lichess/disconnect',{method:'POST',body:'{}'}));$('calibrate').onclick=()=>{calibrationClicks=[];calibrationActive=true;$('calibrationHint').textContent='Click top-left corner (1 of 4).'};$('camera').onclick=event=>{if(!calibrationActive)return;try{calibrationClicks.push(normalizedImageClick(event))}catch(error){toast(error.message,true);return}const names=['top-left','top-right','bottom-right','bottom-left'];if(calibrationClicks.length<4){$('calibrationHint').textContent=`Click ${names[calibrationClicks.length]} corner (${calibrationClicks.length+1} of 4).`}else{calibrationActive=false;act(()=>api('/api/camera/calibrate',{method:'POST',body:JSON.stringify({corners:calibrationClicks})}))}};$('clearCalibration').onclick=()=>act(()=>api('/api/camera/calibration/clear',{method:'POST',body:'{}'}));refresh();
</script><script>
const setupRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data);const setup=data.controller.setup||{};for(const key of ['diagnostics','endstops','homed','movement','magnet','centers']){const node=$('setup'+key[0].toUpperCase()+key.slice(1));node.textContent=setup[key]?'Passed':'Pending'}badge($('setupBadge'),setup.centers?'Commissioned':setup.last_error?'Failed':'In progress',setup.centers?'good':setup.last_error?'bad':'');$('setupResults').textContent=setup.last_error?`FAILED at ${setup.last_step}: ${setup.last_error}`:JSON.stringify(setup.results||{},null,2)}catch(error){toast(error.message,true)}};const setupBody=()=>JSON.stringify({confirmation:$('setupConfirmation').value});$('setupDiagnosticsButton').onclick=()=>act(()=>api('/api/setup/diagnostics',{method:'POST',body:'{}'}));$('setupEndstopsButton').onclick=()=>act(()=>api('/api/setup/endstops',{method:'POST',body:'{}'}));$('setupHomeButton').onclick=()=>act(()=>api('/api/setup/home',{method:'POST',body:setupBody()}));$('setupMovementButton').onclick=()=>act(()=>api('/api/setup/movement',{method:'POST',body:setupBody()}));$('setupMagnetButton').onclick=()=>act(()=>api('/api/setup/magnet',{method:'POST',body:setupBody()}));$('setupCentersButton').onclick=()=>act(()=>api('/api/setup/centers',{method:'POST',body:setupBody()}));$('setupCombinedButton').onclick=()=>act(()=>api('/api/setup/combined',{method:'POST',body:setupBody()}));refresh();
</script><script>
let cameraViewMode='raw';function phoneBase(source){return source.replace(/^auto:/,'').replace(/^snapshot:/,'').replace(/\/shot\.jpg.*$/,'').replace(/\/video.*$/,'').replace(/\/$/,'')}function renderCameraHealth(data){const camera=data.camera,fresh=camera.running&&!camera.stale;$('readyCamera').textContent=fresh?`Connected · ${camera.resolved_source||camera.source}`:camera.capture_error?'Offline':'Waiting';badge($('cameraBadge'),fresh?'Live':camera.stale?'Stale':'Offline',fresh?'good':camera.stale?'bad':'');$('cameraNote').textContent=camera.capture_error||camera.inference_error||(fresh?`Fresh plan frame · ${camera.frame_age_s.toFixed(1)}s old`:'No fresh phone frame. Start the camera server and press Test phone.');$('cameraEmpty').style.display=fresh?'none':'block';if(fresh){if(cameraViewMode==='raw'){if(!$('camera').src.includes('/api/camera/stream'))$('camera').src='/api/camera/stream'}else{$('camera').src=`/api/camera/frame?t=${Date.now()}`}}$('cameraViewTitle').textContent=cameraViewMode==='raw'?'Live phone broadcast':'Calibrated top-down plan view';$('showPlan').disabled=!camera.calibrated}const healthRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data);renderCameraHealth(data);const setup=data.controller.setup||{};for(const key of ['diagnostics','endstops','homed','movement','magnet','centers']){const node=$('setup'+key[0].toUpperCase()+key.slice(1));node.textContent=setup[key]?'Passed':'Pending'}badge($('setupBadge'),setup.centers?'Commissioned':setup.last_error?'Failed':'In progress',setup.centers?'good':setup.last_error?'bad':'');$('setupResults').textContent=setup.last_error?`FAILED at ${setup.last_step}: ${setup.last_error}`:JSON.stringify(setup.results||{},null,2)}catch(error){toast(error.message,true)}};$('cameraTest').onclick=()=>act(async()=>{const value=await api('/api/camera/probe',{method:'POST',body:JSON.stringify({source:$('cameraSource').value.trim()})});toast(`Phone connected: ${value.result.width}×${value.result.height}, ${value.result.latency_ms} ms`)});$('showRaw').onclick=()=>{cameraViewMode='raw';$('camera').src='/api/camera/stream';refresh()};$('showPlan').onclick=()=>{cameraViewMode='plan';refresh()};const previousCalibrate=$('calibrate').onclick;$('calibrate').onclick=()=>{cameraViewMode='raw';$('camera').src='/api/camera/stream';previousCalibrate()};refresh();
</script><script>
let analysisOrientation='white',analysisSuggestion=null;function squareRowsFromStatus(data){const arena=data.arena||{};if(arena.rows&&!['idle','stopped'].includes(arena.state))return{rows:arena.rows,source:'AI Arena'};const game=data.game.status;if(game&&game.rows&&game.mode!=='idle')return{rows:game.rows,source:'Game'};return{rows:data.camera.rows||Array(8).fill('........'),source:'Camera'}}function squareIndex(name){if(!name||name.length<2)return null;return(8-Number(name[1]))*8+'abcdefgh'.indexOf(name[0])}function renderAnalysis(data){const arena=data.arena||{},coordinator=data.game.status||{},game=arena.rows&&!['idle','stopped'].includes(arena.state)?arena:coordinator,position=squareRowsFromStatus(data),rows=position.rows||Array(8).fill('........'),history=game.history||[],last=history[history.length-1],highlight=analysisSuggestion||last,from=highlight?squareIndex(highlight.uci.slice(0,2)):null,to=highlight?squareIndex(highlight.uci.slice(2,4)):null,display=[];for(let row=0;row<8;row++)for(let column=0;column<8;column++){const sourceRow=analysisOrientation==='white'?row:7-row,sourceColumn=analysisOrientation==='white'?column:7-column,symbol=rows[sourceRow]?.[sourceColumn]||'.',file='abcdefgh'[sourceColumn],rank=String(8-sourceRow),canonical=sourceRow*8+sourceColumn;display.push({symbol,name:file+rank,canonical})}const root=$('board');root.innerHTML='';for(const item of display){const cell=document.createElement('div');cell.className=`square ${(('abcdefgh'.indexOf(item.name[0])+Number(item.name[1]))%2)?'light':'dark'} ${item.symbol==='?'||item.symbol==='x'?'unknown':''}`;if(item.canonical===from)cell.classList.add('last-from');if(item.canonical===to)cell.classList.add('last-to');if(game.in_check&&((game.turn==='white'&&item.symbol==='K')||(game.turn==='black'&&item.symbol==='k')))cell.classList.add('in-check');cell.dataset.square=item.name;cell.setAttribute('role','gridcell');cell.setAttribute('aria-label',`${item.name} ${item.symbol==='.'?'empty':item.symbol}`);const glyph=document.createElement('span');glyph.className='piece-glyph';glyph.textContent=symbols[item.symbol]??'?';cell.appendChild(glyph);root.appendChild(cell)}$('rankLabels').innerHTML=(analysisOrientation==='white'?[8,7,6,5,4,3,2,1]:[1,2,3,4,5,6,7,8]).map(value=>`<span>${value}</span>`).join('');$('fileLabels').innerHTML=(analysisOrientation==='white'?'abcdefgh':'hgfedcba').split('').map(value=>`<span>${value}</span>`).join('');$('boardSource').textContent=position.source;$('legalMoves').textContent=game.legal_move_count??0;$('boardGameState').textContent=game.game_over?`Finished ${game.result}`:game.in_check?`${game.turn} in check`:game.turn?`${game.turn} to move`:'Waiting';$('boardView').textContent=analysisOrientation==='white'?'White':'Black';$('fenDisplay').textContent=game.fen||'No active game FEN yet.';$('moveHistory').innerHTML=history.length?Array.from({length:Math.ceil(history.length/2)},(_,index)=>{const white=history[index*2],black=history[index*2+1];const move=value=>value?`<div class="move-pill"><strong>${value.san}</strong><small>${value.actor}</small></div>`:'<div></div>';return`<div class="number">${index+1}.</div>${move(white)}${move(black)}`}).join(''):'<span class="hint">No moves yet.</span>';const ai=game.last_ai;$('aiInsight').hidden=!ai;if(ai){$('aiRationale').textContent=`${ai.san} · ${ai.rationale}`;$('aiPlan').textContent=ai.plan}}const analysisRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data);renderCameraHealth(data);renderAnalysis(data);const setup=data.controller.setup||{};for(const key of ['diagnostics','endstops','homed','movement','magnet','centers']){const node=$('setup'+key[0].toUpperCase()+key.slice(1));node.textContent=setup[key]?'Passed':'Pending'}badge($('setupBadge'),setup.centers?'Commissioned':setup.last_error?'Failed':'In progress',setup.centers?'good':setup.last_error?'bad':'');$('setupResults').textContent=setup.last_error?`FAILED at ${setup.last_step}: ${setup.last_error}`:JSON.stringify(setup.results||{},null,2)}catch(error){toast(error.message,true)}};$('flipBoard').onclick=()=>{analysisOrientation=analysisOrientation==='white'?'black':'white';if(current)renderAnalysis(current)};$('analyzePosition').onclick=()=>act(async()=>{const value=await api('/api/analysis/openai',{method:'POST',body:JSON.stringify({style:$('opponentStyle').value})});analysisSuggestion=value.result;$('aiInsight').hidden=false;$('aiRationale').textContent=`${value.result.uci} · ${value.result.rationale}`;$('aiPlan').textContent=value.result.plan;if(current)renderAnalysis(current)});refresh();
</script><script>
function arenaMoveGrid(history){return history.length?Array.from({length:Math.ceil(history.length/2)},(_,index)=>{const white=history[index*2],black=history[index*2+1];const move=value=>value?`<div class="move-pill"><strong>${value.san}</strong><small>${value.actor} · ${value.latency_s}s</small></div>`:'<div></div>';return`<div class="number">${index+1}.</div>${move(white)}${move(black)}`}).join(''):'<span class="hint">No arena moves yet.</span>'}function renderArenaSetup(data){const arena=data.arena||{},commission=data.commissioning||{},cap=data.capabilities||{};$('readyCommissioning').textContent=commission.commissioned?'Commissioned':commission.reason==='configuration_changed'?'Config changed':'Not attested';$('setupDetails').open=!commission.commissioned;$('chatgptReady').textContent=cap.openai?'Ready':'OPENAI_API_KEY missing';$('claudeReady').textContent=cap.anthropic?'Ready':'ANTHROPIC_API_KEY missing';$('arenaState').textContent=arena.state||'idle';$('arenaResult').textContent=arena.result||'—';badge($('arenaBadge'),arena.state||'idle',arena.state==='failed'?'bad':arena.state==='finished'?'good':'');$('arenaStart').disabled=!cap.openai||!cap.anthropic||['starting','homing','thinking','executing','waiting_manual','running'].includes(arena.state);$('arenaStop').disabled=!['starting','homing','thinking','executing','waiting_manual','running'].includes(arena.state);$('arenaHistory').innerHTML=arenaMoveGrid(arena.history||[]);const manual=arena.manual_action;$('arenaManual').hidden=!manual;if(manual)$('arenaInstruction').textContent=manual.instruction}const arenaRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data);renderCameraHealth(data);renderAnalysis(data);renderArenaSetup(data);const setup=data.controller.setup||{};for(const key of ['diagnostics','endstops','homed','movement','magnet','centers']){const node=$('setup'+key[0].toUpperCase()+key.slice(1));node.textContent=setup[key]?'Passed':'Pending'}badge($('setupBadge'),data.commissioning?.commissioned?'Commissioned':setup.centers?'Tests passed':setup.last_error?'Failed':'Not commissioned',data.commissioning?.commissioned?'good':setup.last_error?'bad':'');$('setupResults').textContent=setup.last_error?`FAILED at ${setup.last_step}: ${setup.last_error}`:JSON.stringify(setup.results||{},null,2)}catch(error){toast(error.message,true)}};$('arenaWhite').onchange=()=>$('arenaWhite').dataset.black=$('arenaWhite').value==='chatgpt'?'claude':'chatgpt';$('arenaStart').onclick=()=>act(()=>api('/api/arena/start',{method:'POST',body:JSON.stringify({white:$('arenaWhite').value,black:$('arenaWhite').value==='chatgpt'?'claude':'chatgpt',style:$('arenaStyle').value,delay_s:Number($('arenaDelay').value),max_plies:Number($('arenaPlies').value),physical:$('arenaPhysical').checked,confirm_motion:true,serial_port:$('serialPort').value,serial_baudrate:$('baudrate').value})}));$('arenaStop').onclick=()=>act(()=>api('/api/arena/stop',{method:'POST',body:'{}'}));$('arenaConfirm').onclick=()=>act(()=>api('/api/arena/confirm-manual',{method:'POST',body:JSON.stringify({confirmation:$('arenaConfirmation').value})}));$('commissioningAttest').onclick=()=>act(()=>api('/api/commissioning/attest',{method:'POST',body:JSON.stringify({confirmation:$('commissioningConfirmation').value})}));$('commissioningClear').onclick=()=>act(()=>api('/api/commissioning/clear',{method:'POST',body:'{}'}));refresh();
</script><script>
let lichessGameValidation=null;$('lichessValidateGame').onclick=()=>act(async()=>{const value=await api('/api/lichess/game/validate',{method:'POST',body:JSON.stringify({game_id:$('gameId').value.trim()})});lichessGameValidation=value.result;$('localColor').value=value.result.local_color;$('lichessGameStatus').textContent=`Ready · you are ${value.result.local_color} vs ${value.result.opponent}`});$('gameId').oninput=()=>{lichessGameValidation=null;$('lichessGameStatus').textContent='Validate this game before starting.'};
</script><script>
let colorSampling=false;function renderFastVision(data){const local=data.camera.local||{},profiles=local.profiles||{},ready=Boolean(local.profiles_ready);badge($('fastVisionBadge'),ready?'Ready':'Sample 6 colors',ready?'good':'');$('detectionMode').textContent=(data.camera.detection_mode||'waiting').replaceAll('_',' ');$('localConfidence').textContent=`${Math.round((local.confidence||0)*100)}% · ${local.hz||0} Hz`;$('localStable').textContent=`${local.stable_frames||0} / 3 · ${local.last_ms??'—'} ms`;$('localUnresolved').textContent=(local.unresolved||[]).join(', ')||'0';$('colorStatus').innerHTML=['green','blue','brown','pink','yellow','orange'].map(color=>{const value=profiles[color]||{};return`<div class="piece" style="border-left-color:${color}">${color} · ${value.type||''}<br>${value.sampled?'calibrated':'not sampled'}</div>`}).join('')}const fastRefresh=refresh;refresh=async function(){try{const data=await api('/api/full-status');render(data);updateEnhancements(data);renderCameraHealth(data);renderAnalysis(data);renderArenaSetup(data);renderFastVision(data);const setup=data.controller.setup||{};for(const key of ['diagnostics','endstops','homed','movement','magnet','centers']){const node=$('setup'+key[0].toUpperCase()+key.slice(1));node.textContent=setup[key]?'Passed':'Pending'}badge($('setupBadge'),data.commissioning?.commissioned?'Commissioned':setup.centers?'Tests passed':setup.last_error?'Failed':'Not commissioned',data.commissioning?.commissioned?'good':setup.last_error?'bad':'');$('setupResults').textContent=setup.last_error?`FAILED at ${setup.last_step}: ${setup.last_error}`:JSON.stringify(setup.results||{},null,2)}catch(error){toast(error.message,true)}};$('generateMarkers').onclick=()=>act(async()=>{const value=await api('/api/camera/markers/generate',{method:'POST',body:'{}'});$('markerDownloads').innerHTML=value.result.files.map(file=>`<a class="btn" href="${file.url}" download>${file.name}</a>`).join(' ');toast('Four printable references generated')});$('arucoCalibrate').onclick=()=>act(()=>api('/api/camera/calibrate-aruco',{method:'POST',body:'{}'}));$('sampleColor').onclick=()=>{colorSampling=true;cameraViewMode='plan';$('camera').style.display='block';refresh();toast(`Click one ${$('pieceColor').value} cap in Plan view`)};const colorClick=$('camera').onclick;$('camera').onclick=event=>{if(colorSampling){colorSampling=false;let point;try{point=normalizedImageClick(event)}catch(error){toast(error.message,true);return}act(()=>api('/api/camera/color/sample',{method:'POST',body:JSON.stringify({color:$('pieceColor').value,x:point.x,y:point.y})}));return}colorClick(event)};refresh();
</script></body></html>"""


CAMERA_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Sol Camera</title><style>body{margin:0;background:#05090b;color:#edf4f8;font-family:system-ui;padding:14px}header{display:flex;justify-content:space-between;align-items:center}img{width:100%;max-height:79vh;object-fit:contain;background:#000;border-radius:10px}pre{background:#101b1f;border:1px solid #263a41;padding:12px;max-height:14vh;overflow:auto;border-radius:9px}.square{position:relative;font-family:"Noto Sans Symbols 2","DejaVu Sans",serif;user-select:none}.square .piece-glyph{filter:drop-shadow(0 2px 1px #0006);transform:translateY(-1%)}.square.last-from{box-shadow:inset 0 0 0 5px #ffc968aa}.square.last-to{box-shadow:inset 0 0 0 5px #72efc1}.square.in-check{background:#d45866!important;animation:checkpulse 1.2s ease-in-out infinite alternate}.square::after{content:attr(data-square);position:absolute;right:4px;bottom:3px;font:700 .55rem/1 monospace;opacity:.45}.move-history{display:grid;grid-template-columns:2.4rem 1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;max-height:260px;overflow-y:auto}.move-history>*{background:#102025;padding:8px}.move-history .number{color:var(--muted);text-align:right}.move-pill{display:flex;justify-content:space-between;gap:6px}.move-pill small{color:var(--muted)}.ai-insight{margin-top:12px;border:1px solid #315a76;background:#0e202d;border-radius:11px;padding:13px}.ai-insight strong{color:var(--blue)}.ai-insight p{margin:7px 0}.ai-insight small{color:var(--muted)}@keyframes checkpulse{from{box-shadow:inset 0 0 0 3px #ff93a0}to{box-shadow:inset 0 0 24px #ff263f}}@media(max-width:680px){.square::after{font-size:.48rem}.move-history{grid-template-columns:2rem 1fr 1fr}}</style></head><body><header><h1>Phone camera</h1><strong>OpenAI Sol</strong></header><img id="camera"><pre id="status">Waiting…</pre><script>async function refresh(){const r=await fetch('/api/camera/status');const d=(await r.json()).camera;if(d.frames)document.getElementById('camera').src='/api/camera/frame?t='+Date.now();document.getElementById('status').textContent=JSON.stringify({status:d.status,stable:d.stable_observations,error:d.error,problems:d.problems,pieces:d.pieces},null,2)}refresh();setInterval(refresh,1000)</script></body></html>"""


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

    def _arena(self) -> AIArena:
        return self.server.ai_arena

    def _commissioning(self) -> CommissioningStore:
        return self.server.commissioning

    def _station(self) -> LichessStation:
        return self.server.station_game

    def _station_player_endpoint(self, path: str) -> bool:
        return False

    def _require_station_idle(self) -> None:
        if self._station().reserves_hardware():
            raise ConfigurationError(
                "station game reserves the gantry; finish, stop, or reconcile it first"
            )

    def _analysis_board(self) -> Any:
        import chess

        game = self._game().status()["status"]
        if game.get("fen"):
            return chess.Board(game["fen"])
        raise ValidationError(
            "start a game before analysis so side-to-move and rule state are authoritative"
        )

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
        elif parsed.path == "/station":
            self._html(STATION_HTML)
        elif parsed.path == "/api/station/status":
            self._json({"ok": True, "result": self._station().admin_status()})
        elif parsed.path == "/api/station/qr":
            seat = parse_qs(parsed.query).get("seat", [""])[0]
            urls = self._station().admin_status().get("join_urls", {})
            if seat not in urls:
                self._json({"ok": False, "error": "station seat is unavailable"}, 404)
                return
            body = qr_svg(urls[seat])
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/full-status":
            try:
                camera = self._camera().status()
                game = self._game().status()
                arena = self._arena().status()
                commissioning = self._commissioning().status()
                pending = self.controller.pending_transaction()
                self._json(
                    {
                        "ok": True,
                        "controller": self.controller.status(),
                        "camera": camera,
                        "game": game,
                        "arena": arena,
                        "station": self._station().admin_status(),
                        "commissioning": commissioning,
                        "board": self.controller.board_state(),
                        "pending": pending,
                        "capabilities": {
                            "openai": bool(
                                os.environ.get("OPENAI_API_KEY", "").strip()
                            ),
                            "anthropic": bool(
                                os.environ.get("ANTHROPIC_API_KEY", "").strip()
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
        elif self.path.startswith("/api/camera/marker/"):
            name = self.path.rsplit("/", 1)[-1]
            if (
                not name.startswith("board-")
                or not name.endswith(".png")
                or "/" in name
            ):
                self._json({"ok": False, "error": "invalid marker name"}, 400)
                return
            path = Path.cwd() / "data" / "vision-markers" / name
            if not path.exists():
                self._json({"ok": False, "error": "marker not generated"}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if not self._station_player_endpoint(parsed.path) and not self._authenticated():
            self._json({"ok": False, "error": "authentication required"}, 401)
            return
        try:
            if parsed.path == "/api/camera/browser-frame":
                result = self._camera().ingest_browser_jpeg(self._raw_body(8_000_000))
                self._json({"ok": True, "result": result})
                return
            payload = self._payload()
            if parsed.path == "/api/station/create":
                if self._game().running() or self._arena().running():
                    raise ConfigurationError(
                        "stop the current game before station mode"
                    )
                if (
                    not self._commissioning().status()["commissioned"]
                    and not self.controller.demo
                ):
                    raise ConfigurationError(
                        "station mode requires completed commissioning attestation"
                    )
                if self.controller.connected:
                    self.controller.disconnect()
                if self.controller.pending_transaction() is not None:
                    raise ConfigurationError(
                        "reconcile the pending physical transaction before station mode"
                    )
                result = self._station().create(
                    base_url=getattr(
                        self.server,
                        "station_public_url",
                        f"http://{self.headers.get('Host', '127.0.0.1:8000')}",
                    ),
                    confirmation=str(payload.get("confirmation", "")),
                )
            elif parsed.path == "/api/station/stop":
                result = self._station().stop()
            elif parsed.path == "/api/station/reconcile/apply":
                result = self._station().reconcile(
                    applied=True,
                    confirmation=str(payload.get("confirmation", "")),
                )
            elif parsed.path == "/api/station/reconcile/discard":
                result = self._station().reconcile(
                    applied=False,
                    confirmation=str(payload.get("confirmation", "")),
                )
            elif parsed.path == "/api/controller/connect":
                self._require_station_idle()
                baudrate = payload.get("baudrate")
                result = self.controller.connect(
                    port=str(payload.get("port", "")).strip() or None,
                    baudrate=int(baudrate) if baudrate not in {None, ""} else None,
                )
            elif parsed.path == "/api/controller/disconnect":
                self._require_station_idle()
                result = self.controller.disconnect()
            elif self.path == "/api/controller/home":
                self._require_station_idle()
                result = self.controller.home_xy()
            elif self.path == "/api/controller/stop":
                if self._station().reserves_hardware():
                    result = self._station().stop()
                else:
                    result = self.controller.emergency_stop()
            elif self.path == "/api/setup/diagnostics":
                self._require_station_idle()
                result = self.controller.run_setup_diagnostics()
            elif self.path == "/api/setup/endstops":
                self._require_station_idle()
                result = self.controller.verify_setup_endstops()
            elif self.path == "/api/setup/home":
                self._require_station_idle()
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before homing")
                result = self.controller.home_xy()
            elif self.path == "/api/setup/movement":
                self._require_station_idle()
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before movement test")
                result = self.controller.run_setup_movement_test()
            elif self.path == "/api/setup/magnet":
                self._require_station_idle()
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before magnet test")
                result = self.controller.run_setup_magnet_test()
            elif self.path == "/api/setup/centers":
                self._require_station_idle()
                if payload.get("confirmation") != "SETUP AREA CLEAR":
                    raise ValidationError("type SETUP AREA CLEAR before center test")
                result = self.controller.run_setup_square_centers()
            elif self.path == "/api/setup/combined":
                self._require_station_idle()
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
            elif self.path == "/api/camera/calibrate-aruco":
                result = self._camera().calibrate_aruco()
            elif self.path == "/api/camera/color/sample":
                result = self._camera().sample_piece_color(
                    str(payload.get("color", "")),
                    float(payload.get("x")),
                    float(payload.get("y")),
                )
            elif self.path == "/api/camera/markers/generate":
                paths = generate_reference_markers(
                    Path.cwd() / "data" / "vision-markers"
                )
                result = {
                    "files": [
                        {
                            "name": path.name,
                            "url": f"/api/camera/marker/{path.name}",
                        }
                        for path in paths
                    ]
                }
            elif self.path == "/api/camera/calibration/clear":
                result = self._camera().clear_calibration()
            elif self.path == "/api/game/start":
                self._require_station_idle()
                if self._arena().running():
                    raise ConfigurationError("stop the Claude vs ChatGPT arena first")
                if self.controller.connected:
                    self.controller.disconnect()
                mode = str(payload.get("mode", ""))
                game_id = payload.get("game_id")
                local_color = payload.get("local_color")
                if mode in {"lichess", "mirror"}:
                    validation = self._lichess().validate_game(str(game_id or ""))
                    if mode == "lichess":
                        local_color = validation["local_color"]
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
                    game_id=game_id,
                    local_color=local_color,
                    confirm_motion=payload.get("confirm_motion") is True,
                    serial_port=str(payload.get("serial_port", "")).strip() or None,
                    serial_baudrate=(
                        int(payload["serial_baudrate"])
                        if payload.get("serial_baudrate") not in {None, ""}
                        else None
                    ),
                    opponent_style=str(payload.get("opponent_style", "balanced")),
                )
            elif self.path == "/api/game/stop":
                result = self._game().stop()
            elif self.path == "/api/analysis/openai":
                if self.server.openai_opponent is None:
                    raise ConfigurationError(
                        "OPENAI_API_KEY is required for ChatGPT analysis"
                    )
                board = self._analysis_board()
                result = self.server.openai_opponent.choose_move(
                    board, style=str(payload.get("style", "balanced"))
                ).model_dump()
            elif self.path == "/api/arena/start":
                self._require_station_idle()
                if self._game().running():
                    raise ConfigurationError(
                        "stop the current game before starting AI arena"
                    )
                physical = payload.get("physical") is True
                if physical and not self._commissioning().status()["commissioned"]:
                    raise ConfigurationError(
                        "physical AI arena requires completed commissioning attestation"
                    )
                if self.controller.connected:
                    self.controller.disconnect()
                if physical and self.controller.pending_transaction() is not None:
                    raise ConfigurationError(
                        "reconcile the pending physical transaction before AI arena"
                    )
                result = self._arena().start(
                    white=str(payload.get("white", "chatgpt")),
                    black=str(payload.get("black", "claude")),
                    style=str(payload.get("style", "balanced")),
                    delay_s=float(payload.get("delay_s", 0.5)),
                    max_plies=None,
                    physical=physical,
                    confirm_motion=payload.get("confirm_motion") is True,
                    serial_port=str(payload.get("serial_port", "")).strip() or None,
                    serial_baudrate=(
                        int(payload["serial_baudrate"])
                        if payload.get("serial_baudrate") not in {None, ""}
                        else None
                    ),
                )
            elif self.path == "/api/arena/stop":
                result = self._arena().stop()
            elif self.path == "/api/arena/confirm-manual":
                if payload.get("confirmation") != "AI MOVE COMPLETED":
                    raise ValidationError(
                        "type AI MOVE COMPLETED after the physical move"
                    )
                result = self._arena().confirm_manual_action()
            elif self.path == "/api/commissioning/attest":
                result = self._commissioning().attest(
                    str(payload.get("confirmation", "")),
                    git_commit=os.environ.get("CHESS_GANTRY_GIT_COMMIT", "unknown"),
                )
            elif self.path == "/api/commissioning/clear":
                result = self._commissioning().clear()
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
            elif self.path == "/api/lichess/game/validate":
                result = self._lichess().validate_game(str(payload.get("game_id", "")))
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
        color_profile_path=root / "data" / "color_profiles.json",
    )
    oauth = LichessOAuth()
    opponent = (
        SolChessOpponent() if os.environ.get("OPENAI_API_KEY", "").strip() else None
    )
    claude = (
        ClaudeChessOpponent()
        if os.environ.get("ANTHROPIC_API_KEY", "").strip()
        else None
    )
    game = GameCoordinator(
        root,
        config,
        camera,
        demo=demo,
        token_provider=oauth.token,
        opponent=opponent,
    )
    arena = AIArena(opponent, claude, config=config, root=root, demo=demo)
    station = LichessStation(root, config, demo=demo)
    commissioning = CommissioningStore(
        root / "data" / "commissioning.json", root / "config.json"
    )
    try:
        server = GantryHTTPServer((host, port), RequestHandler)
    except OSError as exc:
        camera.close()
        controller.disconnect()
        raise web_bind_error(host, port, exc) from exc
    server.camera = camera
    server.game = game
    server.lichess_oauth = oauth
    server.openai_opponent = opponent
    server.ai_arena = arena
    server.station_game = station
    server.commissioning = commissioning
    server.clerk_verifier = ClerkVerifier(settings) if settings else None
    server.dashboard_html = render_dashboard(HTML, settings) if settings else HTML
    server.station_public_url = station_public_url(host, port)
    if os.environ.get("CHESS_GANTRY_STATION_AUTOSTART", "") == "1":
        if controller.pending_transaction() is not None:
            server.server_close()
            camera.close()
            raise ConfigurationError(
                "station autostart is blocked by a pending physical transaction"
            )
        if not demo and not commissioning.status()["commissioned"]:
            server.server_close()
            camera.close()
            raise ConfigurationError(
                "station autostart requires completed commissioning"
            )
        station.create(
            base_url=server.station_public_url,
            confirmation=STATION_CONFIRMATION,
        )
    url = f"http://{host}:{port}"
    print(f"Chess Gantry running at {url}")
    print(f"Station lobby at {server.station_public_url}/station")
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
        if arena.running():
            arena.stop()
        if station.active():
            station.stop()
        camera.close()
        controller.disconnect()
        server.server_close()
