# CT TestBench — Cryptotherm ASIC Miner Test Bench Platform

**Current Version:** v1.0.0 (production) | v1.1.0-dev (sandbox)

Enterprise ASIC miner testing platform for Cryptotherm. Tests Bitcoin ASIC miners (Antminer, Whatsminer, Avalon) on a test bench, generates diagnostic labels, manages PDU power control, and tracks deployment.

---

## Quick Reference for Russ

| What | Where |
|------|-------|
| **Kiosk URL** | http://10.0.0.248:8000 (cthome LAN) |
| **Kiosk (Pi)** | Wayland display on Geniov1 (10.0.0.71) |
| **Server** | cthome (10.0.0.248 / 100.85.100.85) |
| **Database** | PostgreSQL on cthome :5432 |
| **Git repo** | github.com/cryptotherm/miner-testbench |
| **Production branch** | `main` — stable, always working |
| **Development branch** | `sandbox` — make changes here first |

---

## Branch & Version Workflow

### Two Branches

```
main (PRODUCTION)         sandbox (DEVELOPMENT)
────────────────          ─────────────────────
v1.0.0 — stable          v1.1.0-dev — working copy
Always working            Make changes here
Kiosk runs this           Test before promoting
Tagged releases           Commit freely
```

### How to Make Changes

1. **Switch to sandbox** on the kiosk: Settings tab → Build Channel → `v1.1.0-dev — Sandbox` → Switch & Restart
2. **Make your changes** to the code on cthome at `~/miner-testbench/`
3. **Test** on the kiosk — if something breaks, switch back to Production
4. **When happy**, promote to production (see below)

### Promoting Sandbox → Production

```bash
# On cthome
cd ~/miner-testbench

# Make sure sandbox is up to date
git checkout sandbox
git pull origin sandbox

# Merge to main
git checkout main
git pull origin main
git merge sandbox

# Tag the new release
git tag -a v1.1.0 -m 'Brief description of what changed'
git push origin main
git push origin v1.1.0
```

### Version Numbers
- `v1.0.X` — bug fix (e.g. v1.0.1, v1.0.2)
- `v1.X.0` — new feature (e.g. v1.1.0, v1.2.0)
- `vX.0.0` — major rewrite (e.g. v2.0.0)

Update `SANDBOX_VERSION` in `server/version.py` when starting new sandbox work.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  cthome (Ubuntu, 10.0.0.248 / Tailscale 100.85.100.85)     │
│  ┌─────────────────────┐  ┌────────────┐                   │
│  │  FastAPI Server     │  │ PostgreSQL │                   │
│  │  :8000              │  │ :5432      │                   │
│  │  pm2: miner-testbench│  │            │                   │
│  └──────────┬──────────┘  └────────────┘                   │
└─────────────┼───────────────────────────────────────────────┘
              │ LAN (10.0.0.x)
┌─────────────┼───────────────────────────────────────────────┐
│  Geniov1 Pi │(10.0.0.71) — Kiosk display                   │
│  ┌──────────▼────────────────────────────────────────────┐  │
│  │  Chromium Kiosk (Wayland, CDP port 9223)              │  │
│  │  Connects to cthome:8000                              │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
              │
┌─────────────▼───────────────────────────────────────────────┐
│  Test Bench Network                                          │
│  Rack A: 192.168.2.x   Rack B: 192.168.3.x                 │
│  Rack C: 192.168.4.x   Rack D: 192.168.5.x                 │
│  PDU: 10.0.0.226 (L1/L2/L3/L4 phases, 8 outlets each)     │
│  Printer: TCP://192.168.1.200 (Brother QL-810W)            │
└─────────────────────────────────────────────────────────────┘
```

---

## Server Management

### Start / Stop / Restart
```bash
# On cthome (ssh ctadmin@100.85.100.85)
pm2 restart miner-testbench    # restart
pm2 stop miner-testbench       # stop
pm2 start miner-testbench      # start
pm2 logs miner-testbench       # view logs
```

### Switch Branch (via command line)
```bash
cd ~/miner-testbench
git checkout sandbox            # switch to sandbox
git checkout main               # switch to production
pm2 restart miner-testbench     # restart after switching
```

### Deploy New Code
```bash
cd ~/miner-testbench
git pull origin sandbox         # pull latest sandbox
pm2 restart miner-testbench
```

---

## Kiosk Management

### Reload Kiosk Page
```bash
# On cthome (SSH to Pi first if needed)
cd ~/miner-testbench/server && .venv/bin/python3 /tmp/cdp_reload.py
```

### SSH to Pi
```bash
ssh -J ctadmin@100.85.100.85 arduino@10.0.0.71
# Password: motmaster
```

---

## Tabs Overview

| Tab | Function |
|-----|---------|
| **Miners** | Full miner inventory — all registered miners, status, location |
| **Scan** | Scan subnet for miners, register new ones by MAC address |
| **Bench** | Live test bench view — active miners, run diagnostic cycle |
| **History** | Test history, pass/fail records, diagnostic results |
| **PDU** | Smart PDU control — individual outlet on/off, amperage |
| **Settings** | Server config, AI settings, printer, LED, **build channel** |
| **Discovery** | UDP broadcast discovery of miners on local network |
| **Chat** | Chat with AI agent for diagnostics and support |

---

## Diagnostic Flow

1. Scan miner → register with serial + MAC
2. Slot miner into bench rack
3. Run test cycle (power cycle × 3 if fails)
4. AI diagnosis via Qwen2.5 on Alienware GPU (LLM)
5. Pass → print green pass label
6. Fail → print red fail label with diagnosis
7. Escalate to OpenClaw agent if AI can't resolve

---

## Files & Directories

```
miner-testbench/
├── server/
│   ├── main.py              # FastAPI app entry point
│   ├── version.py           # Version management (NEW)
│   ├── config.py            # Settings (PDU IP, DB URL, etc.)
│   ├── models/              # SQLAlchemy DB models
│   ├── routers/             # API route handlers
│   │   ├── miners.py        # Miner CRUD + diagnostics
│   │   ├── scan.py          # Network scanning
│   │   ├── bench.py         # Bench operations
│   │   ├── pdu.py           # PDU control
│   │   ├── labels.py        # Label printing
│   │   ├── settings.py      # Settings + version switch
│   │   └── chat.py          # AI chat
│   ├── services/
│   │   ├── ai_diagnosis.py  # Qwen2.5 LLM diagnosis
│   │   ├── label_printer.py # Brother QL-810W driver
│   │   ├── miner_client.py  # Antminer/Whatsminer API
│   │   └── autoscan_service.py # Background scanning
│   └── static/
│       └── index.html       # Kiosk UI (all-in-one)
├── secrets/                 # SSH keys, credentials (private)
└── README.md                # This file
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/version` | Current version, branch, channel |
| POST | `/api/version/switch` | Switch branch + restart |
| GET | `/api/miners` | List all miners |
| POST | `/api/miners/scan` | Scan for new miners |
| POST | `/api/miners/{id}/test` | Run diagnostic test |
| GET | `/api/pdu/outlets` | PDU outlet status |
| POST | `/api/pdu/outlets/{id}/toggle` | Toggle PDU outlet |
| GET | `/api/settings` | Server settings |
| POST | `/api/labels/{id}/print` | Print label |

Full API docs at: http://10.0.0.248:8000/docs

---

## AI Diagnosis

- **Primary:** Qwen2.5-Coder 7B on Alienware-1 GPU (100.86.10.89:11434)
- **Fallback:** Rule-based diagnosis (no LLM needed)
- Diagnoses: hashboard failures, temp issues, fan faults, pool connection problems
- Returns: issue summary, suggested fix, severity, auto-fixable flag

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Kiosk shows blank screen | `pm2 restart miner-testbench` on cthome |
| Can't reach miners | Check rack subnet (192.168.x.x) — Pi must be on same L2 |
| PDU not responding | Check 10.0.0.226 is reachable from cthome |
| Label printer not printing | Check TCP://192.168.1.200, printer on and connected |
| AI diagnosis not working | Check Alienware-1 (100.86.10.89:11434) is online |
| Switch to production | Settings → Build Channel → Production → Switch & Restart |

---

*Built by Cryptotherm. Maintained by Austin Bank & Russ.*
