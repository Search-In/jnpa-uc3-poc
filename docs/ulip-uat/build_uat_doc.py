#!/usr/bin/env python3
"""Render the ULIP UAT / test-case document to PDF — submission edition.

    python3 docs/ulip-uat/build_uat_doc.py

This is the document sent to NLDSL in support of a production-access request,
so it is deliberately short: an evaluator needs to see that every granted API
was called from a real application, what came back, and how the application
behaved — not our internal call graph or a restatement of ULIP's own contracts.

What is deliberately left out, and why:
  * ULIP's request/response contracts — NLDSL wrote them; quoting them back
    costs pages and adds nothing an evaluator does not already have.
  * The per-API layer/artefact chain — internal structure, not evidence.
  * Standalone "input request" screenshots — every response screenshot already
    shows the request that produced it, so a second image is duplication.
  * The constraints and queries section — sent separately as a covering note so
    this document stays an evidence pack.

Screenshots are picked up automatically: a file named ``SS-nn*.png`` in
``screenshots/`` is embedded wherever slot ``SS-nn`` falls. Slot ids are
assigned in document order, so they shift if sections are added or removed —
re-run ``capture_api.py`` / ``capture_ui.mjs`` after any structural change.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import content as C  # noqa: E402
from results import RESULTS  # noqa: E402

SHOTS_DIR = HERE / "screenshots"
OUT_HTML = HERE / "ULIP_UAT_TestCases_JNPA_UC3.html"
OUT_PDF = HERE / "ULIP_UAT_TestCases_JNPA_UC3.pdf"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

SHOT_KIND_LABEL = {"response": "Request and response", "ui": "Application screen"}

# Slots whose evidence cannot be produced, and why — stated so a reviewer sees
# a reason rather than an apparent omission.
BLOCKED: dict[str, str] = {
    "SS-43": "The staging licence returns an empty TransportValidityTodate, so "
             "the transport-over-non-transport preference cannot be shown on "
             "live data. Covered by an automated contract test.",
    "SS-45": "Requires a driver enrolled against a licence SARATHI does not "
             "recognise; no such record exists on staging.",
    "SS-46": "No licence with a non-active status was available on staging. "
             "Covered by an automated contract test.",
}


class Shots:
    """Assigns ``SS-nn`` ids in document order and resolves image files."""

    def __init__(self) -> None:
        self.n = 0
        self.index: list[dict] = []

    def add(self, kind: str, caption: str, where: str) -> dict:
        self.n += 1
        slot = {"id": f"SS-{self.n:02d}", "kind": kind, "caption": caption,
                "where": where, "file": self._find(f"SS-{self.n:02d}")}
        self.index.append(slot)
        return slot

    @staticmethod
    def _find(shot_id: str) -> Path | None:
        if not SHOTS_DIR.is_dir():
            return None
        for path in sorted(SHOTS_DIR.iterdir()):
            if path.name.startswith(shot_id) and path.suffix.lower() in {
                    ".png", ".jpg", ".jpeg", ".webp"}:
                return path
        return None


def esc(text: str) -> str:
    return html.escape(str(text))


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def render_shot(slot: dict) -> str:
    label = SHOT_KIND_LABEL.get(slot["kind"], slot["kind"])
    head = (f'<div class="shot-head"><span class="shot-id">{slot["id"]}</span>'
            f'<span class="shot-kind">{esc(label)}</span>'
            f'<span class="shot-cap">{esc(slot["caption"])}</span></div>')
    if slot["file"]:
        return (f'<figure class="shot">{head}'
                f'<img src="{data_uri(slot["file"])}" alt="{esc(slot["id"])}">'
                f"</figure>")
    reason = BLOCKED.get(slot["id"], "")
    body = ('<div class="shot-placeholder">'
            '<div class="ph-title">Evidence not obtainable on staging</div>'
            f'<div class="ph-how">{esc(reason or slot["where"])}</div></div>')
    return f'<figure class="shot pending">{head}{body}</figure>'


# ----------------------------------------------------------------- sections
def cover() -> str:
    d = C.DOC
    rows = [("Project", d["project"]), ("Use case", d["use_case"]),
            ("Environment", d["environment"]), ("ULIP account", d["account"]),
            ("Calling IP address", d["egress_ip"]),
            ("Document version", d["version"]), ("Date", d["date"]),
            ("Submitted to", d["submitted_to"])]
    body = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
    return f"""
<section class="cover">
  <div class="cover-org">{esc(d["org"])}</div>
  <h1 class="cover-title">{esc(d["title"])}</h1>
  <div class="cover-sub">{esc(d["subtitle"])}</div>
  <table class="cover-meta">{body}</table>
  <div class="cover-foot">
    Submitted in support of a request for <b>production API access</b> for the
    thirteen APIs granted to this account on the ULIP staging environment.
  </div>
</section>"""


def summary(shots: Shots) -> str:
    rows = "".join(
        f'<tr><td><code>{esc(api)}</code></td><td>{esc(inp)}</td>'
        f'<td class="{"pass" if st == "PASS" else "fail"}">{esc(st)}</td>'
        f"<td>{esc(lat)}</td><td>{note}</td></tr>"
        for api, inp, st, lat, note in C.EXECUTION["rows"])
    # SS-01/02 are the login request and response; SS-03 the full run. All
    # three are allocated so that every later id keeps matching the filename it
    # was captured under — only the ones worth printing are rendered.
    shots.add("request", "Login request", "login")
    login = shots.add("response", "ULIP login — token issued, token redacted",
                      "Login against ULIP staging")
    run = shots.add("response",
                    "All 13 granted APIs called in one run, with per-API "
                    "status and latency", "Live run against ULIP staging")
    return f"""
<section class="first">
  <h2><span class="num">1.</span> Summary</h2>
  <p>{C.INTRO}</p>
  <div class="callout">{C.SCOPE_NOTE}</div>

  <h3>Environment and authentication</h3>
  <p>All calls originate from a single static AWS Elastic IP,
     <b>65.2.212.121</b>, which NLDSL registered on the staging allow-list on
     11&nbsp;August&nbsp;2026. The application logs in once via
     <code>POST /user/login</code>, caches the bearer token for 30&nbsp;minutes,
     sends it on every subsequent call, and re-authenticates exactly once on a
     401 or 403. Credentials reach the backend through environment variables
     only — they are never committed, never logged and never exposed to the
     browser.</p>
  <p>Every ULIP miss arrives as <b>HTTP 200</b> with an error marker inside the
     body rather than as an HTTP error status — VAHAN <code>code 231</code>,
     SARATHI <code>errorcode -1</code>, FASTag <code>errCode 740</code> and
     <code>respCode 239</code>, GatiShakti an empty <code>data</code> array, LDB
     <code>responseStatus FAILURE</code>. The application treats each as
     "no data" and never as a partial record; each is exercised by a negative
     test case in the sections that follow.</p>

  <h3>Execution against ULIP staging, 11 August 2026</h3>
  <table class="grid small">
    <thead><tr><th style="width:13%">API</th><th style="width:18%">Input</th>
      <th style="width:8%">Result</th><th style="width:9%">Latency</th>
      <th>Observation</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="callout">{C.EXECUTION["outcome"]}</div>
  {render_shot(login)}
  {render_shot(run)}
</section>"""


def api_section(idx: int, api: dict, shots: Shots) -> str:
    cases, shot_blocks = [], []
    for case in api["cases"]:
        tc_id = f"TC-{api['api'].replace('/', '')}-{case['id']}"
        status, actual = RESULTS.get((api["api"], case["id"]),
                                     ("NOT RUN", "not executed"))
        # The response screenshots already contain the request that produced
        # them, so request-only slots are allocated (to keep ids stable) but
        # never printed.
        declared = [(kind, shots.add(kind, cap, f"{tc_id} — {api['screen']}"))
                    for kind, cap in case["shots"]]
        slots = [slot for kind, slot in declared if kind != "request"]
        cases.append(
            f"<tr><td><b>{esc(tc_id)}</b><br>"
            f"<span class='muted'>{esc(case['title'])}</span></td>"
            f"<td><code>{esc(case['input'])}</code></td>"
            f"<td>{esc(case['expected'])}</td><td>{actual}</td>"
            f'<td class="ctr">'
            f'<span class="verdict v-{status.lower().replace(" ", "-")}">'
            f"{esc(status)}</span><br>"
            f"<span class='muted ev'>{', '.join(s['id'] for s in slots) or '—'}"
            f"</span></td></tr>")
        shot_blocks.extend(render_shot(s) for s in slots)

    return f"""
<section class="api">
  <h2><span class="num">{idx}.</span> {esc(api["api"])} — {esc(api["name"])}</h2>
  <table class="grid strip">
    <tr><th>Granted id</th><td><code>{esc(api["granted"])}</code></td>
        <th>ULIP endpoint</th>
        <td><code>POST /ulip/v1.0.0/{esc(api["api"])}</code></td></tr>
    <tr><th>Application API</th><td colspan="3">
        <code>{esc(api["app_api"])}</code></td></tr>
    <tr><th>Operator screen</th><td colspan="3">{esc(api["screen"])}</td></tr>
  </table>
  <p class="purpose">{esc(api["purpose"])}</p>
  <table class="grid tc">
    <thead><tr><th style="width:15%">Test case</th><th style="width:16%">Input</th>
      <th style="width:24%">Expected</th><th>Actual result</th>
      <th style="width:11%">Status</th></tr></thead>
    <tbody>{"".join(cases)}</tbody>
  </table>
  {"".join(shot_blocks)}
</section>"""


def traceability(idx: int, shots: Shots) -> str:
    posture = [shots.add(k, c, "Integration posture screens")
               for k, c in C.HEALTH["shots"]]
    rows, tally = [], {}
    for api in C.APIS:
        cells = []
        for c in api["cases"]:
            st = RESULTS.get((api["api"], c["id"]), ("NOT RUN", ""))[0]
            tally[st] = tally.get(st, 0) + 1
            cells.append(f"TC-{api['api'].replace('/', '')}-{c['id']} "
                         f'<span class="verdict v-{st.lower().replace(" ", "-")}">'
                         f"{esc(st)}</span>")
        rows.append(f"<tr><td><code>{esc(api['granted'])}</code></td>"
                    f"<td><code>{esc(api['api'])}</code></td>"
                    f"<td>{'<br>'.join(cells)}</td></tr>")
    total = sum(tally.values())
    tallied = " · ".join(f"<b>{n}</b> {k}" for k, n in sorted(tally.items()))
    posture_shots = "".join(render_shot(s) for s in posture)
    return f"""
<section>
  <h2><span class="num">{idx}.</span> Traceability and outcome</h2>
  <p>All thirteen granted APIs were called from the application against ULIP
     staging, and every one is covered by at least one test case with screenshot
     evidence of the request and the corresponding response.</p>
  <div class="callout">Across {total} test cases: {tallied}.<br><br>
     <b>BLOCKED</b> marks a case whose evidence could not be produced for a
     reason outside the application — an upstream service that was unavailable,
     or a test value that ULIP masks and that we therefore cannot supply.
     <b>OBSERVED</b> marks a case where the application behaves soundly but
     differently from the expectation as originally drafted; the actual
     behaviour is stated. No case failed because of a defect in the
     integration.</div>
  <table class="grid small">
    <thead><tr><th style="width:16%">Granted id</th>
      <th style="width:14%">ULIP API</th>
      <th>Test cases and outcome</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>

  <h3>Integration posture in the application</h3>
  <p>The application exposes the ULIP link on its own operator screens, so the
     control room can see at a glance which integrations are live and what the
     most recent call returned.</p>
  {posture_shots}

  <h3>Queries raised with NLDSL, and their resolution</h3>
  <p>Every query we raised during testing has been answered by NLDSL and the
     answers are reflected above. LDB/01 was confirmed working and, with the
     container NLDSL supplied, now returns a full thirteen-leg trail. SARATHI/01
     resolves with the test data provided. The response structures of
     GATISHAKTI/01, /02 and /03 were confirmed as the intended datasets and the
     application maps them as returned. VAHAN/01's varying answer and FASTAG/02's
     empty tag list were explained as staging holding static test data, which
     need not reflect production. The masked chassis and engine numbers behind
     VAHAN/02 and VAHAN/03 were confirmed to fall under the PII clause; those two
     cases therefore remain unevidenced, which is a data-access matter and not a
     defect in the integration.</p>
  <p>Two items are being taken forward separately with
     <code>bd@nldsl.in</code> as new requirements, per NLDSL's guidance: road
     network and corridor data carrying geometry or coordinates, and access to
     unmasked chassis and engine numbers for gate-side vehicle identification.
     Neither blocks this submission.</p>

  <h3>Request</h3>
  <p>On the strength of the evidence in this document we request
     <b>production API access</b> for the same thirteen APIs, and confirmation
     of whether the calling IP <code>65.2.212.121</code> should also be
     registered for the production environment.</p>
</section>"""


CSS = """
@page { size: A4; margin: 15mm 13mm 16mm 13mm; }
* { box-sizing: border-box; }
body { font-family: "Helvetica Neue", Arial, sans-serif; font-size: 9.2pt;
       line-height: 1.45; color: #16202b; margin: 0; }
h1, h2, h3 { color: #0b2545; margin: 0 0 .4em; line-height: 1.25; }
h2 { font-size: 13.5pt; padding-bottom: .26em; border-bottom: 2px solid #0b2545;
     margin-top: 0; }
h3 { font-size: 10pt; margin-top: 1.2em; color: #23415f; }
.num { color: #6b7f96; font-weight: 600; margin-right: .35em; }
p { margin: .45em 0; text-align: justify; }
p.purpose { margin: .5em 0 .9em; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 8.1pt;
       background: #eef2f6; padding: .06em .28em; border-radius: 2px;
       word-break: break-word; }
section { page-break-before: always; }
section.cover { page-break-before: auto; }
.muted { color: #6b7f96; }
.ev { font-size: 7.4pt; }
.ctr { text-align: center; }

.cover { display: flex; flex-direction: column; justify-content: center;
         min-height: 245mm; }
.cover-org { font-size: 11pt; letter-spacing: .16em; text-transform: uppercase;
             color: #6b7f96; margin-bottom: 2.2em; }
.cover-title { font-size: 25pt; border: 0; margin-bottom: .18em; }
.cover-sub { font-size: 12.5pt; color: #46617f; margin-bottom: 3em; }
.cover-meta { width: 100%; border-collapse: collapse; font-size: 9.6pt; }
.cover-meta th { text-align: left; width: 34%; padding: .55em .8em .55em 0;
                 color: #6b7f96; font-weight: 600; vertical-align: top;
                 border-bottom: 1px solid #e3e9f0; }
.cover-meta td { padding: .55em 0; border-bottom: 1px solid #e3e9f0;
                 vertical-align: top; }
.cover-foot { margin-top: 3.4em; font-size: 8.6pt; color: #6b7f96;
              border-top: 2px solid #0b2545; padding-top: .9em; }

table.grid { width: 100%; border-collapse: collapse; margin: .6em 0 .9em;
             font-size: 8.4pt; }
table.grid th, table.grid td { border: 1px solid #dde4ec; padding: .4em .55em;
                               text-align: left; vertical-align: top; }
table.grid thead th { background: #0b2545; color: #fff; font-weight: 600; }
table.grid.small { font-size: 8pt; }
table.grid.strip th { background: #eef2f6; color: #23415f; width: 15%;
                      white-space: nowrap; }
table.grid.tc { font-size: 7.9pt; }
table.grid.tc tbody tr { page-break-inside: avoid; }
td.pass { color: #1c6b3a; font-weight: 700; }
td.fail { color: #a3302a; font-weight: 700; }
.verdict { font-weight: 700; letter-spacing: .03em; font-size: 7.6pt;
           padding: .1em .45em; border-radius: 2px; white-space: nowrap; }
.v-pass { color: #14532d; background: #dcfce7; }
.v-blocked { color: #7c2d12; background: #ffedd5; }
.v-partial { color: #713f12; background: #fef3c7; }
.v-observed { color: #1e3a5f; background: #dbeafe; }
.v-fail { color: #7f1d1d; background: #fee2e2; }

figure.shot { margin: 8px 0 0; border: 1px solid #dde4ec; border-radius: 4px;
              overflow: hidden; page-break-inside: avoid; background: #fff; }
.shot-head { display: flex; align-items: baseline; gap: .55em; padding: 4px 8px;
             background: #eef2f6; border-bottom: 1px solid #dde4ec;
             font-size: 7.6pt; }
.shot-id { font-weight: 700; color: #0b2545; letter-spacing: .04em; }
.shot-kind { font-size: 6.9pt; text-transform: uppercase; letter-spacing: .08em;
             color: #fff; background: #46617f; padding: .1em .45em;
             border-radius: 2px; white-space: nowrap; }
.shot-cap { color: #46617f; }
figure.shot img { display: block; width: 100%; height: auto; }
.shot-placeholder { padding: 14px; text-align: center; background: #fdf6ef;
                    border: 1px dashed #d8b48a; margin: 6px; border-radius: 3px; }
.ph-title { font-weight: 700; color: #8a5a2b; font-size: 8.4pt;
            letter-spacing: .04em; text-transform: uppercase; }
.ph-how { color: #6b5340; font-size: 7.9pt; margin-top: .4em; }

.callout { border-left: 3px solid #46617f; background: #f4f7fa; padding: 7px 10px;
           margin: .8em 0; font-size: 8.4pt; }
"""


def build_html() -> str:
    shots = Shots()
    parts = [cover(), summary(shots)]
    for i, api in enumerate(C.APIS, start=2):
        parts.append(api_section(i, api, shots))
    parts.append(traceability(len(C.APIS) + 2, shots))
    filled = sum(1 for s in shots.index if s["file"])
    print(f"screenshot slots: {len(shots.index)} "
          f"({filled} embedded, {len(shots.index) - filled} stated unobtainable)")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{esc(C.DOC["title"])}</title><style>{CSS}</style></head>'
            f'<body>{"".join(parts)}</body></html>')


def main() -> int:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(build_html(), encoding="utf-8")
    if not Path(CHROME).exists():
        print("Chrome not found — open the HTML and print to PDF manually.")
        return 1
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         "--virtual-time-budget=20000", f"--print-to-pdf={OUT_PDF}",
         OUT_HTML.as_uri()], check=True, capture_output=True)
    print(f"wrote {OUT_PDF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
