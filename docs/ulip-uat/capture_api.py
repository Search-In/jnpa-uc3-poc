#!/usr/bin/env python3
"""Capture the request/response evidence screenshots for the UAT document.

Calls the running gateway (which must be pointed at LIVE ULIP), renders each
call as an API-console image, and writes it to ``screenshots/SS-nn.png``.

    python3 docs/ulip-uat/capture_api.py            # all slots
    python3 docs/ulip-uat/capture_api.py SS-05 SS-11

The gateway must be reachable at ``GATEWAY`` below with ``ULIP_LIVE_ENABLED=1``
and real credentials, and its egress IP must be the one NLDSL whitelisted.
Nothing here fabricates a response: whatever the endpoint actually returns is
what lands in the image, including misses and failures — those are the evidence
for the negative test cases.
"""
from __future__ import annotations

import html
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SHOTS = HERE / "screenshots"
GATEWAY = "http://127.0.0.1:8000"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# slot -> (title, method, path, body|None, show)
#   show="request"  -> render the request only (input evidence)
#   show="response" -> render request + response (output evidence)
CALLS: dict[str, tuple] = {
    # --- FASTAG/01 -------------------------------------------------------
    "SS-04": ("Toll crossings — input", "POST", "/api/fastag/transactions",
              {"rc_number": "CG07BC9186"}, "request"),
    "SS-05": ("Toll crossings — response", "POST", "/api/fastag/transactions",
              {"rc_number": "CG07BC9186"}, "response"),
    "SS-07": ("Toll crossings — vehicle with no activity in 72 h", "POST",
              "/api/fastag/transactions", {"rc_number": "MH19JK3923"}, "response"),
    "SS-09": ("Toll crossings — malformed vehicle number rejected", "POST",
              "/api/fastag/transactions", {"rc_number": "12"}, "response"),
    # --- FASTAG/02 -------------------------------------------------------
    "SS-10": ("Tag registry by vehicle — input", "POST", "/api/fastag/tag-status",
              {"rc_number": "CG07BC9186"}, "request"),
    "SS-11": ("Tag registry by vehicle — response", "POST", "/api/fastag/tag-status",
              {"rc_number": "CG07BC9186"}, "response"),
    "SS-13": ("Tag registry by tag id — input", "POST", "/api/fastag/tag-status",
              {"tag_id": "34161FA8203286140F4064E0"}, "request"),
    "SS-14": ("Tag registry by tag id — response", "POST", "/api/fastag/tag-status",
              {"tag_id": "34161FA8203286140F4064E0"}, "response"),
    "SS-15": ("Tag registry — both identifiers rejected", "POST",
              "/api/fastag/tag-status",
              {"rc_number": "CG07BC9186", "tag_id": "34161FA8203286140F4064E0"},
              "response"),
    # --- LDB/01 ----------------------------------------------------------
    "SS-16": ("Container tracking — input", "GET",
              "/api/logistics/tracking/TCLU8538808", None, "request"),
    "SS-17": ("Container tracking — response", "GET",
              "/api/logistics/tracking/TCLU8538808", None, "response"),
    "SS-19": ("Container tracking — a trail naming a different container is "
              "rejected", "GET",
              "/api/logistics/tracking/CXRU1145597", None, "response"),
    "SS-21": ("Container tracking — malformed container number", "GET",
              "/api/logistics/tracking/ABC123", None, "response"),
    # --- VAHAN/04 --------------------------------------------------------
    "SS-22": ("RC verification — input", "GET", "/api/vahan/rc/UP32KH0320",
              None, "request"),
    "SS-23": ("RC verification — response (LIVE_PRIMARY / ULIP)", "GET",
              "/api/vahan/rc/UP32KH0320", None, "response"),
    "SS-25": ("RC verification — unknown vehicle degrades", "GET",
              "/api/vahan/rc/MH01ZZ9999", None, "response"),
    # --- VAHAN/01 --------------------------------------------------------
    "SS-28": ("RC via the XML API — input", "GET", "/api/vahan/rc/UP32KH0320",
              None, "request"),
    "SS-29": ("RC via the XML API — response", "GET",
              "/api/vahan/rc/UP32KH0320", None, "response"),
    "SS-31": ("RC retry — both upstreams attempted", "GET",
              "/api/vahan/rc/MH01ZZ9999", None, "response"),
    # --- VAHAN/02 / 03 ---------------------------------------------------
    "SS-32": ("Chassis lookup — input", "GET",
              "/api/vahan/chassis/MA3ERLF1S00170578", None, "request"),
    "SS-33": ("Chassis lookup — response", "GET",
              "/api/vahan/chassis/MA3ERLF1S00170578", None, "response"),
    "SS-35": ("Chassis lookup — unknown chassis", "GET",
              "/api/vahan/chassis/ZZZZZZZZZZZZZZZZZ", None, "response"),
    "SS-36": ("Engine lookup — input", "GET",
              "/api/vahan/engine/F8DN5217616", None, "request"),
    "SS-37": ("Engine lookup — response", "GET",
              "/api/vahan/engine/F8DN5217616", None, "response"),
    "SS-39": ("Engine lookup — unknown engine", "GET",
              "/api/vahan/engine/ZZZZZZZZZZ", None, "response"),
    # --- SARATHI/02 ------------------------------------------------------
    "SS-40": ("DL verification — input", "GET",
              "/api/vahan/dl/AP01620210000019", None, "request"),
    "SS-41": ("DL verification — response", "GET",
              "/api/vahan/dl/AP01620210000019", None, "response"),
    "SS-44": ("DL verification — unknown licence", "GET",
              "/api/vahan/dl/XX00000000000000", None, "response"),
    # --- SARATHI/01 ------------------------------------------------------
    "SS-47": ("Enrolment lookup with DL + date of birth — input", "GET",
              "/api/vahan/dl/GJ04%2020120005008?dob=1987-05-26", None, "request"),
    "SS-48": ("Enrolment lookup with DL + date of birth — response", "GET",
              "/api/vahan/dl/GJ04%2020120005008?dob=1987-05-26", None, "response"),
    "SS-50": ("Malformed date of birth rejected before the call", "GET",
              "/api/vahan/dl/GJ04%2020120005008?dob=26-05-1987", None, "response"),
    # --- persistence -----------------------------------------------------
    "SS-27": ("Verification history — ULIP-sourced verifications persisted",
              "GET", "/api/vahan/verification-history?limit=6", None, "response"),
    # --- GATISHAKTI/04 ---------------------------------------------------
    "SS-51": ("Toll-plaza refresh — input", "POST", "/api/gatishakti/refresh",
              {"state_id": "27", "nh_no": "NH-5"}, "request"),
    "SS-52": ("Toll-plaza master for Maharashtra — response", "GET",
              "/api/gatishakti/toll-plazas?state_id=27&limit=6", None, "response"),
    "SS-53": ("Refresh repeated — rows updated in place, not duplicated", "POST",
              "/api/gatishakti/refresh", {"state_id": "27", "nh_no": "NH-5"},
              "response"),
    "SS-54": ("Toll plazas — unknown state id", "GET",
              "/api/gatishakti/toll-plazas?state_id=99", None, "response"),
    # --- GATISHAKTI/01 ---------------------------------------------------
    "SS-55": ("Highway detail by NH number — input", "GET",
              "/api/gatishakti/roads?nh_no=NH-5&limit=6", None, "request"),
    "SS-56": ("Highway detail by NH number — response", "GET",
              "/api/gatishakti/roads?nh_no=NH-5&limit=6", None, "response"),
    "SS-57": ("Highway detail — malformed NH number", "GET",
              "/api/gatishakti/roads?nh_no=5", None, "response"),
    # --- GATISHAKTI/02 ---------------------------------------------------
    "SS-58": ("State reference set — input", "GET",
              "/api/gatishakti/roads?state_id=27", None, "request"),
    "SS-59": ("State reference set — response", "GET",
              "/api/gatishakti/roads?state_id=27&limit=6", None, "response"),
    "SS-60": ("State reference set — unknown state id", "GET",
              "/api/gatishakti/roads?state_id=99", None, "response"),
    # --- GATISHAKTI/03 ---------------------------------------------------
    "SS-61": ("Named points — input", "GET",
              "/api/gatishakti/road-points?state_id=27", None, "request"),
    "SS-62": ("Named points — response", "GET",
              "/api/gatishakti/road-points?state_id=27&limit=6", None, "response"),
    "SS-63": ("Named points — stored numeric coordinates", "GET",
              "/api/gatishakti/road-points?state_id=27&limit=3", None, "response"),
}


def call(method: str, path: str, body) -> tuple[int, str]:
    url = GATEWAY + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except Exception as exc:  # noqa: BLE001
        return 0, json.dumps({"error": str(exc)})


def pretty(text: str, limit: int = 2600) -> str:
    try:
        out = json.dumps(json.loads(text), indent=2)
    except Exception:  # noqa: BLE001
        out = text
    if len(out) > limit:
        out = out[:limit].rstrip() + "\n  … truncated for the document …"
    return out


PAGE = """<!doctype html><meta charset="utf-8"><style>
 body{{margin:0;background:#0f1319;font:12.5px/1.5 "SF Mono",Menlo,monospace;color:#d7dee8}}
 .bar{{background:#1c2430;padding:8px 14px;color:#9fb0c4;font-size:12px;
   border-bottom:1px solid #2b3644;display:flex;gap:10px;align-items:center}}
 .tag{{background:#2f6feb;color:#fff;border-radius:3px;padding:1px 7px;font-size:11px;
   font-weight:700;letter-spacing:.03em}}
 .body{{padding:14px 16px 18px}}
 .lbl{{color:#7f8fa4;font-size:10.5px;letter-spacing:.11em;text-transform:uppercase;
   font-weight:700;margin:12px 0 5px}} .lbl:first-child{{margin-top:0}}
 pre{{margin:0;white-space:pre-wrap;word-break:break-word}}
 .req{{color:#9ae6b4}} .st{{font-weight:700}} .ok{{color:#4ade80}} .bad{{color:#f87171}}
</style><div class="bar"><span class="tag">{slot}</span>{title}</div>
<div class="body">{content}</div>"""


def render(slot: str, title: str, method: str, path: str, body,
           status: int | None, resp_text: str | None) -> None:
    parts = ['<div class="lbl">Request</div><pre class="req">'
             + html.escape(f"{method} {path}\nContent-Type: application/json\n"
                           f"Accept: application/json")
             + (("\n\n" + html.escape(json.dumps(body, indent=2))) if body else "")
             + "</pre>"]
    if status is not None:
        cls = "ok" if 200 <= status < 300 else "bad"
        parts.append(f'<div class="lbl">Response</div>'
                     f'<pre><span class="st {cls}">HTTP {status}</span>\n'
                     + html.escape(pretty(resp_text or "")) + "</pre>")
    page = PAGE.format(slot=slot, title=html.escape(title),
                       content="".join(parts))
    tmp = SHOTS / f".{slot}.html"
    tmp.write_text(page, encoding="utf-8")
    out = SHOTS / f"{slot}.png"
    # Chrome's --screenshot captures the viewport, not the document, so render
    # tall and trim the dead space below the content rather than guessing a
    # height per payload.
    subprocess.run([CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
                    "--window-size=1080,2400", "--screenshot=" + str(out),
                    tmp.as_uri()], check=True, capture_output=True)
    tmp.unlink(missing_ok=True)
    _trim(out)


def _trim(path: Path, pad: int = 14) -> None:
    """Crop the uniform background below the rendered content."""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    w, h = img.size
    bg = img.getpixel((w - 3, h - 3))
    last = 0
    for y in range(h - 1, -1, -1):
        row = img.crop((0, y, w, y + 1)).getcolors(w) or []
        if not (len(row) == 1 and row[0][1] == bg):
            last = y
            break
    img.crop((0, 0, w, min(h, last + pad))).save(path)


def main(argv: list[str]) -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    wanted = [a.upper() for a in argv] or sorted(CALLS)
    for slot in wanted:
        spec = CALLS.get(slot)
        if not spec:
            print(f"{slot}: no such slot")
            continue
        title, method, path, body, show = spec
        if show == "request":
            render(slot, title, method, path, body, None, None)
            print(f"{slot}  request-only  {method} {path}")
            continue
        status, text = call(method, path, body)
        render(slot, title, method, path, body, status, text)
        head = text.replace("\n", " ")[:90]
        print(f"{slot}  HTTP {status:<4} {method} {path}\n        {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
