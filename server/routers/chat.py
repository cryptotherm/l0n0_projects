"""
Chat relay — runs a fresh OpenClaw agent turn each time with full bench context injected.
Uses --local flag so no session history accumulates — always starts clean with live data.
"""
import asyncio, json, re, base64
import urllib.request
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

router = APIRouter(prefix="/api/chat", tags=["chat"])

CTHOME_HOST = "100.85.100.85"
CTHOME_USER = "ctadmin"
CTHOME_PASS = "Iethe1"

BENCH_CONTEXT_TEMPLATE = """You are an AI assistant built into the CT TestBench kiosk — a touchscreen Raspberry Pi at Cryptotherm's ASIC repair bench in Calgary. The operator is physically standing at the bench talking to you.

YOU HAVE DIRECT ACCESS TO ALL BENCH DATA BELOW. Answer questions using it — never say you can't access the bench or ask the operator to look things up for you.

BENCH KIOSK: Pi at 192.168.3.13 / 10.0.0.133, FastAPI port 8000, code at /home/abank/miner-testbench/
PDU: http://10.0.0.226 login admin/admin — 24 outlets, 3 phases (L1/L2/L3), 8 each
PRINTER: Brother QL-810W USB on Pi
RACK SUBNETS: Rack A=192.168.3.x, B=192.168.2.x, C=192.168.5.x, D=192.168.6.x
SLOTS: A: AL1-1..AL1-4 | B: BL2-5..BL2-7 | C: CL2-1..CL2-4 | D: DL1-5..DL1-8

=== LIVE MINER DATABASE (as of right now) ===
{miner_table}
=== END MINER DATA ===

PREVIOUS CONVERSATION:
{history}

HOW TO RESPOND:
- Be direct and short — operator is at the bench, not reading an essay
- Reference miner IDs, IPs, and statuses from the data above when relevant
- If something needs fixing in the code, say exactly what to do
- If a miner is offline/not responding, say so and suggest checking the cable/power
- You CAN run commands on the Pi via: ssh abank@100.115.130.43 (pw: motmaster)"""


def _get_live_miners():
    try:
        from datetime import datetime, timezone
        resp = urllib.request.urlopen('http://localhost:8000/api/miners', timeout=3)
        miners = json.loads(resp.read())
        now = datetime.now(timezone.utc)
        lines = []
        for m in miners:
            ls = m.get('last_seen') or ''
            age = 'never seen'
            if ls and not ls.startswith('2000'):
                try:
                    t = datetime.fromisoformat(ls.replace('Z', '+00:00'))
                    mins = (now - t).total_seconds() / 60
                    age = f"seen {mins:.0f}min ago"
                except: age = ls[:16]
            hr = m.get('rt_hashrate') or 0
            sn = m.get('serial_number') or 'NO S/N'
            mac = m.get('mac_address') or 'no MAC'
            ip = m.get('ip_address') or 'no IP'
            loc = m.get('location') or 'unassigned'
            hbs = m.get('hashboards') or []
            dead = sum(1 for h in hbs if not h.get('active', True))
            fault = (m.get('fault_summary') or '')[:80]
            st = m.get('status', '?')
            lines.append(
                f"  ID {m['id']}: {m.get('model','Unknown')[:18]:<18} | {st:<18} | "
                f"IP {ip:<16} | {mac} | S/N: {sn} | "
                f"{hr:.1f} TH/s | slot:{loc} | {age}"
                + (f"\n    FAULT: {fault}" if fault else "")
                + (f" | {dead} dead hashboard(s)" if dead else "")
            )
        return '\n'.join(lines) if lines else "(no miners in database)"
    except Exception as e:
        return f"(could not load: {e})"


class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]

@router.post("")
async def chat(req: ChatRequest):
    user_msg = ""
    for m in reversed(req.messages):
        if m.role == "user":
            user_msg = m.content
            break
    if not user_msg:
        return {"reply": "No message provided."}

    # Build conversation history for context
    history_lines = []
    for m in req.messages[:-1]:  # exclude current message
        role = "Operator" if m.role == "user" else "Assistant"
        history_lines.append(f"{role}: {m.content}")
    history = '\n'.join(history_lines[-6:]) if history_lines else "(this is the first message)"

    miner_table = _get_live_miners()
    context = BENCH_CONTEXT_TEMPLATE.format(
        miner_table=miner_table,
        history=history
    )
    full_msg = f"{context}\n\nOPERATOR: {user_msg}"

    b64 = base64.b64encode(full_msg.encode()).decode()
    # Use timestamped session ID so each conversation turn gets fresh context
    import time as _time
    sid = f"bench-{int(_time.time())}"
    remote_cmd = (
        f"echo '{b64}' | base64 -d > /tmp/bench_chat_msg.txt && "
        f"openclaw agent --session-id {sid} -m \"$(cat /tmp/bench_chat_msg.txt)\" --json 2>/dev/null; "
        f"rm -f /tmp/bench_chat_msg.txt"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "sshpass", "-p", CTHOME_PASS,
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
            f"{CTHOME_USER}@{CTHOME_HOST}",
            remote_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=90)
        out = stdout.decode().strip()
        out = re.sub(r'\x1b\[[0-9;]*m', '', out)

        reply = ""
        try:
            fat = re.search(r'"finalAssistantVisibleText"\s*:\s*"((?:[^"\\]|\\.)*)"', out)
            if fat:
                reply = fat.group(1).replace('\\n', '\n').replace('\\"', '"')
            else:
                json_match = re.search(r'\{.*\}', out, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group())
                    result = data.get("result", data)
                    reply = (result.get("finalAssistantVisibleText") or
                             result.get("reply") or result.get("content") or str(data))
                else:
                    reply = out
        except Exception:
            reply = out

        if not reply:
            reply = re.sub(r'\x1b\[[0-9;]*m', '', stderr.decode().strip()) or "No response."

        return {"reply": reply}

    except asyncio.TimeoutError:
        return {"reply": "Agent timed out — please try again."}
    except Exception as e:
        return {"reply": f"Error: {e}"}
