# Berthing Reports — PDF Data Audit & Full-Extraction Design (UC-III Module 7)

**Status:** Audit + design only. No code changed by this document.
**Source:** `Digital Twin/Data/7-Berthing Reports/` (external to the repo; `.gitignore`d).
**Scope:** Complete inventory + section/table/column catalogue of every terminal PDF, a
terminal-detection + full-fidelity extraction architecture (no data loss), an additive
DB migration plan, an extraction-accuracy report, and a phased implementation plan.

> This complements the existing normalised module (migration `0036`, `services/berthing/`).
> The existing pipeline extracts only the **vessel rows** (on-berth / sailed / expected)
> into the normalised `jnpa.berthing_reports` model. This audit designs a **parallel,
> additive** capability that captures **every table on the page verbatim** into JSONB —
> nothing is dropped, normalised away, or renamed.

---

## 1. File inventory (all 25 PDFs, verified)

Every file is a **single-page** PDF (v1.7). Report date is in the filename for 23 of 25;
`BERTHING_CT.pdf` and `BERTHING_GT.pdf` carry the date only in the page body
(`DATE: dd/mm/yyyy`).

| Terminal | Folder | Files | Report dates | Pages each |
|---|---|---|---|---|
| APMT | `APM Terminals/` | 5 | 26-May, 04-Jun, 06-Jun, 08-Jun, 09-Jun 2026 | 1 |
| BMCT | `BMCT_PSA/` | 5 | 26-May, 04-Jun, 06-Jun, 08-Jun, 09-Jun 2026 (one file tagged `_JNPT`) | 1 |
| NSFT | `NSFT/` | 5 | 26-05, 04-06, 06-06, 08-06, 09-06 2026 | 1 |
| NSICT | `NSICT_DP World/` | 5 | 04-06, 06-06, 08-06, 09-06 2026 + `BERTHING_CT.pdf` (date in body) | 1 |
| NSIGT | `NSIGT_DP World/` | 5 | 04-06, 06-06, 08-06, 09-06 2026 + `BERTHING_GT.pdf` (date in body) | 1 |

**Filenames (exact):**

- **APMT:** `APMT_Berthing_Report_-_26-May-2026.pdf`, `..._-_04-Jun-2026.pdf`, `..._-_06-Jun-2026.pdf`, `..._-_08-Jun-2026.pdf`, `..._-_09-Jun-2026.pdf`
- **BMCT:** `Berthing_Sheet__26_MAY_2026.pdf`, `Berthing_Sheet__04_JUN_2026_JNPT.pdf`, `Berthing_Sheet__06_JUN_2026.pdf`, `Berthing_Sheet__08_JUN_2026.pdf`, `Berthing_Sheet__09_JNPT_JUN_2026.pdf`
- **NSFT:** `Daily_Berthing_Report_26_5_2026.pdf`, `..._4_6_2026.pdf`, `..._6_6_2026.pdf`, `..._8_6_2026.pdf`, `..._9_6_2026.pdf`
- **NSICT:** `BERTHING-CT04062026.pdf`, `BERTHING-CT 06062026.pdf`, `BERTHING-CT 08062026.pdf`, `BERTHING-CT 09062026.pdf`, `BERTHING_CT.pdf`
- **NSIGT:** `BERTHING-GT04062026.pdf`, `BERTHING-GT_06062026.pdf`, `BERTHING-GT_08062026.pdf`, `BERTHING-GT 09062026.pdf`, `BERTHING_GT.pdf`

> **Filename inconsistency** (spaces, underscores, `_JNPT`, no-date) means the terminal +
> report date must be resolved from **PDF content**, not the filename.

---

## 2. Global structural findings (apply to all terminals)

1. **Multi-panel free layout, not ruled grids.** Each page is a dashboard of 6–9 panels
   arranged in columns. The panels are positioned with whitespace, not table borders.
2. **`extract_tables()` is unreliable** — detected table count per file: APMT 6, BMCT 1,
   NSFT 7–8, NSICT 2, NSIGT 1. The same logical layout yields wildly different grids, and
   adjacent panels bleed into one detected "table". **Line-based `extract_text()` interleaves
   spatially-separate panels** (e.g. NSICT's *Vessels Expected* rows are glued to the
   *ICD/CFS Pendency* columns on the same text line — see §3.4).
3. **Consequence for full extraction:** robust, no-data-loss extraction must use
   **word coordinates** (`page.extract_words()` → `x0, top`), cluster words into panels by
   position, then reconstruct each panel's rows/columns against its own header anchors.
   A pure text/regex approach (what the normalised parser uses today) is sufficient for the
   *vessel* rows but **loses** the yard/gate/pendency/throughput panels and mixes columns.
4. **Two-line headers.** Vessel tables use a primary header line plus a sub-line that splits
   composite columns into `Date`/`Time`/`Side` (e.g. APMT `Alongside → Date, Time, Side`).
5. **Transposed matrices.** APMT *CFS Pendency* is a group/value matrix (a row of 14 group
   codes, then a row of 14 TEU values), repeated in bands — not a vertical table.
6. **Sub-columns.** NSFT/NSICT/NSIGT *Expected* split gate windows into `Dry / Reefer`
   sub-columns; NSICT/NSIGT *Expected* nests `EXPECTED(IMP, EXP, TOTAL)` and
   `GATE OPEN/CUTOFF` under group headers.
7. **Voyage code (VIA)** is plain `S0xxx` on-berth everywhere, but **prefixed** in NSICT/NSIGT
   *Expected* (`AGMS0655` = prefix `AGM` + `S0655`).

---

## 3. Terminal-wise analysis

Legend: **Panel** = a visually bounded region; **Table** = a normalised table_name we assign;
`•` composite column split by the header sub-line.

### 3.1 APM Terminals (APMT)

**Identity marker:** header line `APM Terminals Mumbai ******DAILY BERTHING REPORT ******`;
berth codes `APM01/APM02`. **Date:** `Date : 4-June-26`; row dates `dd-Mon` (no year).

**Sections found:** Time Table (×2) · On-Berth Vessel · Sailed Vessel · Vessels Expected ·
Yard Inventory / Reefer · Gate Movements · Rail Pendency · CFS Pendency · Traffic Throughput ·
Contact/Notes block.

| Table Name | Columns (verbatim) | Row format |
|---|---|---|
| `TIME_TABLE` | Date, Time1, Height1, Time2, Height2, Time3, Height3, Time4, Height4 (two side-by-side blocks, 4 days) | 1 row/day, 4 tide cycles |
| `ON_BERTH_VESSEL` | Berth, Vessel, VIA, LOA, Alongside•(Date, Time), Side, Ops Commenced•(Date, Time), Ops Completed•(Date, Time), QC Boom up•(Date, Time), Imp, Imp Bal, Exp, Exp Bal, Arrival BFL•(Date, Time), Max Draft, ETC•(Date, Time) | 1 row/vessel on berth |
| `SAILED_VESSEL` | *(same as ON_BERTH_VESSEL, last col = Sailing Time•(Date, Time))* | 1 row/sailed vessel |
| `VESSELS_EXPECTED` | VIA, Vessel Name, Draft, LOA, ETA•(Date, Time), Arrival BFL•(Date, Time), Gate Open(Date/Time), Reefer Opening(Date/Time), Reefer Cut-Off(Date/Time), Cut-Off(Date/Time), Service, Line | 1 row/expected vessel (~20) |
| `YARD_INVENTORY` | Category (Import/Export/Tranship/Total), TEUs, Reefer boxes | 4 rows |
| `GATE_MOVEMENTS` | Gate (IN/OUT/Total), Cntrs, TEU's | 3 rows |
| `RAIL_PENDENCY` | Dest, Date, TEUS (two column-groups side by side) | N rows (per ICD) |
| `CFS_PENDENCY` | **Matrix**: Group (14 codes/band), Teus (14 values/band); 3 bands + Total | transposed |
| `TRAFFIC_THROUGHPUT` | Period (DAY/MONTH/YEAR), Vessel, Imp TEUs, Exp TEUs, Total | 3 rows |

**Notes:** contact-desk phone block is free text (capture as `NOTES` raw text, not a table).
`ON_BERTH` and `SAILED` share a header but are two stacked sub-tables (the second header line
ends `Sailing Time` vs `ETC`). Some expected rows omit gate/reefer windows (blank cells).

### 3.2 BMCT (BMCT PSA)

**Identity marker:** generic header `BERTHING REPORT` (no terminal name) → identify by berth
codes `BMCT01..05` (or filename `Berthing_Sheet`). **Date:** `Date : 04 JUNE 2026`; row dates `dd-Jun`.

**Sections found:** Tide Table · Yard Inv · Reefer · Gate Movements · Vessels On Berthed ·
Sailed Vessel · Vessels Expected · ICD Pendency · CFS Pendency · Traffic Throughput.

| Table Name | Columns (verbatim) | Row format |
|---|---|---|
| `TIDE_TABLE` | Date, Time1, Height1, Time2, Height2, Time3, Height3, Time4, Height4 | 1 row/day (4 days) |
| `YARD_INVENTORY` | Category (Import/Export/Tranship/Total), TEUs, Reefer (UNT) | 4 rows |
| `GATE_MOVEMENTS` | Gate (IN/OUT/Total), Cntrs, TEU's | 3 rows |
| `VESSELS_ON_BERTHED` | Berth, Vessel, VIA, LOA, Berthing•(Date, Time), Side, Ops Commenced•(Date, Time), Ops Completed•(Date, Time), ETD•(Date, Time), IMP, IMP BAL, EXP, EXP BAL, Max Draft | 1 row/vessel (5 berths) |
| `SAILED_VESSEL` | Berth, Vessel, VIA, LOA, Berthing•(Date, Time), Side, Ops Commenced•(Date, Time), Ops Completed•(Date, Time), Sailing Time•(Date, Time), Max Draft | 1 row/berth |
| `VESSELS_EXPECTED` | VIA No., Vessel Name, Service, Line, LOA, Draft, ETA•(Date, Time), Gate Open(Date/Time), Reefer Opening(Date/Time), Reefer Cut-OFF(Date/Time), Cut-OFF(Date/Time) | 1 row/expected (~45) |
| `ICD_PENDENCY` | Dest, Moves, Teus | N rows + TOTAL |
| `CFS_PENDENCY` | Dest, Moves, Teus | N rows + TOTAL |
| `TRAFFIC_THROUGHPUT` | Period (DAY/MONTH/YEAR), Vessel, Import, Export, Total | 3 rows |

**Notes:** ICD & CFS pendency panels are interleaved to the right of the *Expected* rows in the
text stream. Some on-berth rows are partial (e.g. `BMCT04 JOLLY BIANCO … PUP@06:30` — vessel
present but not yet worked).

### 3.3 NSFT (Nhava Sheva Freeport Terminal)

**Identity marker:** header `NHAVA SHEVA FREEPORT TERMINAL - DAILY BERTHING REPORT`.
**Date:** `Date 04.06.2026`; row datetimes **full** `dd-mm-yyyy hh:mm`. **No berth column.**

**Sections found:** Tide Table · Vessel Sailed (24h) · Vessel at Berth · Vessels Expected ·
Yard Inventory · Gate Movement (24h) · ICD Pendancy · CFS Pendancy.

| Table Name | Columns (verbatim) | Row format |
|---|---|---|
| `TIDE_TABLE` | Date (across 4 columns), Time, Tide (4 cycles/day) — **transposed** (days as columns) | 4 tide rows × 4 days |
| `VESSEL_SAILED_24H` | SR No, Vessel Name, Via No, LOA, Service, Line, Berthed, Ops. Commenced, Ops Completed, Sailed, Import Moves, Export Moves, Total Moves | 1 row/sailed vessel |
| `VESSEL_AT_BERTH` | SR No, Vessel Name, Via No, LOA, Service, Line, Berthed, Ops. Commenced, ETC, Import Moves, Import Balance, Export Moves, Export Balance | 1 row/vessel at berth |
| `VESSELS_EXPECTED` | SR No, Vessel Name, VIA No, LOA, Service, Line, ETA, Gate Open•(Dry container, Reefer container), Gate Cut-off•(Dry container, Reefer container), Import TEUs, Export TEUs | 1 row/expected (~24) |
| `YARD_INVENTORY` | Category (Import/Export/Transhipment/Total), TEUs, Reefer TEUs | 4 rows |
| `GATE_MOVEMENT_24H` | Direction (Inward/Outward/Total), Cntrs, TEUs | 3 rows |
| `ICD_PENDANCY` | Destination, Units, TEUs | N rows + TOTAL ICD PENDENCY |
| `CFS_PENDANCY` | CFS, Units, TEUs | N rows + TOTAL CFS PENDENCY |

**Notes:** the `Gate Open` and `Gate Cut-off` headers each split into `Dry container / Reefer
container` sub-columns (a second header line). Destination codes carry a human name in
parentheses, e.g. `MCT (MANDIDEEP)`, `DRT (DRONAGIRI)` — preserve verbatim.

### 3.4 NSICT (DP World — NSICT)

**Identity marker:** header `DAILY BERTHING REPORT - NSICT`. **Date:** `DATE: 04/06/2026 7:06`;
on-berth datetimes `dd/mm/yyyy hh:mm`; expected ETA `Ddd/dd/mm hh:mm` (e.g. `Thu/04/06 16:00`);
gate cutoff `dd/HHMM` (e.g. `03/1900`). **Berth codes `CB04/CB05`.**

**Sections found:** Tide Table · Yard Inv · Vessels On Berth · Sailed Vessels · Vessels
Expected · ICD Pendency · CFS Pendency · Traffic Throughput.

| Table Name | Columns (verbatim) | Row format |
|---|---|---|
| `TIDE_TABLE` | Date, Time, Height (×4 cycles) | 1 row/day (4 days) |
| `YARD_INV` | Category (EXPORT/IMPORT/T-P/TOTAL), TEUS, RFR MVS | 4 rows |
| `VESSELS_ON_BERTH` | BERTH, VESSEL NAME, VIA, LOA, SERVICE, BERTH SIDE, IMPORT, EXPORT, TTL MVS, ATA, OPS COMMENCE, ETC, ETD | 1 row/vessel on berth |
| `SAILED_VESSELS` | BERTH, VESSEL NAME, VIA, LOA, SERVICE, BERTH SIDE, ATA, OPS COMMENCE, ATC, ATD | often **empty** (berth codes only) |
| `VESSELS_EXPECTED` | SR NO, VESSEL NAME, VIA, LOA, SERVICE, VOA, EXPECTED•(IMP, EXP, TOTAL), ETA, GATE OPEN/CUTOFF•(DRY, REEFER) | 1 row/expected (~32) |
| `ICD_PENDENCY` | DEST, MVS, TEUS | N rows + TOTAL |
| `CFS_PENDENCY` | DEST (codes prefixed `CFS…`), MVS, TEUS | N rows + TOTAL |
| `TRAFFIC_THROUGHPUT` | Period (DAY/MONTH/YEAR), VSL, IMP, EXP, TOTAL | 3 rows |

**Interleave hazard (verified):** in the raw text stream, each *Expected* vessel row is glued to
one *ICD Pendency* row **and** one *CFS Pendency* row on the same physical line, e.g.:

```
2 HT JOURNEY HHTS0641 191.45 ADHOC OAS 232 200 432 Thu/04/06 11:00 01/0600 02/0600 04/1200  BGK 0 0  CFSAPO 2 180
└────────────── VESSELS_EXPECTED row ──────────────────────────────────────────────┘ └ICD┘ └─CFS──┘
```

→ line-based parsing **cannot** separate these three tables reliably; **positional (x-range)
segmentation is required** so the ICD/CFS columns are cut off before they contaminate the
vessel row (and vice-versa).

### 3.5 NSIGT (DP World — NSIGT)

**Identity marker:** header `DAILY BERTHING REPORT - NSIGT`. Berth codes `CB06`.
**Layout is byte-for-byte identical to NSICT** — same panels, same headers, same date formats,
same interleave hazard. **One shared DP-World parser template covers both** (terminal label
switched by the `NSICT`/`NSIGT` header token).

---

## 4. Cross-terminal differences (do NOT assume uniformity)

| Aspect | APMT | BMCT | NSFT | NSICT / NSIGT |
|---|---|---|---|---|
| Identity marker | "APM Terminals Mumbai" | "BERTHING REPORT" + `BMCT##` | "NHAVA SHEVA FREEPORT TERMINAL" | "…REPORT - NSICT/NSIGT" |
| Report-date format | `4-June-26` | `04 JUNE 2026` | `04.06.2026` | `04/06/2026 7:06` |
| Row datetime format | `dd-Mon` + `hh:mm` (no year) | `dd-Jun` + `hh:mm` (no year) | `dd-mm-yyyy hh:mm` (full) | `dd/mm/yyyy hh:mm`; ETA `Ddd/dd/mm hh:mm`; cutoff `dd/HHMM` |
| Berth column | `APM01/02` | `BMCT01..05` | **none** (SR No) | `CB04/05/06` |
| On-berth section title | `ON BERTH VESSEL` | `VESSELS ON BERTHED` | `Vessel at Berth` | `VESSELS ON BERTH` |
| Sailed section title | *(2nd sub-table)* | `SAILED VESSEL` | `Vessel Sailed in last 24 hours` | `SAILED VESSELS` |
| Expected section title | `Vessels Expected` | `VESSELS EXPECTED` | `Vessels Expected` | `VESSELS EXPECTED` |
| On-berth extra cols | QC Boom up, Arrival BFL, ETC | ETD, IMP/EXP Bal | Moves/Balance (no berth) | IMPORT/EXPORT/TTL MVS, ATA, ETC, ETD |
| Expected extra cols | Draft, Arrival BFL, Gate/Reefer×4, Service, Line | Service, Line, Draft, Gate/Reefer×4 | Gate Open/Cut-off (Dry/Reefer), Import/Export TEUs | VOA, IMP/EXP/TOTAL, GATE OPEN/CUTOFF (Dry/Reefer) |
| VIA/voyage form | `S0xxx` | `S0xxx` | `S0xxx` | on-berth `S0xxx`; expected `AAAS0xxx` |
| Pendency panels | Rail + CFS (matrix) | ICD + CFS (dest/moves/teus) | ICD + CFS (destination/units/teus) | ICD + CFS (dest/mvs/teus, CFS-prefixed) |
| Yard labels | TEUs / Reefer boxes | TEUs / Reefer (UNT) | TEUs / Reefer TEUs | TEUS / RFR MVS |
| `extract_tables()` count | 6 | 1 | 7–8 | 2 / 1 |

**Bottom line:** 4 distinct layout families → **4 parser templates**: `APMT`, `BMCT`, `NSFT`,
`DPWORLD` (NSICT+NSIGT). No two share table_name + header + order + section set.

---

## 5. Extraction architecture (full fidelity, no data loss)

```
Upload PDF
   │
   ▼
[1] extract_words() with coordinates  (pdfplumber: text, x0, x1, top, bottom)
   │
   ▼
[2] detect_terminal(text)  → APMT | BMCT | NSFT | NSICT | NSIGT   (header/berth markers)
   │        (reuses services/berthing/pdf_parsers.detect_terminal — already exists)
   ▼
[3] load terminal template  → panel regions + per-panel header anchors + column x-ranges
   │
   ▼
[4] for each panel:
       • locate the section-header label(s) by text
       • bound the panel by (x-range, y-range) relative to the header
       • cluster words into rows by `top` (y) within a tolerance
       • assign each word to a column by `x0` against the header anchors
       • preserve EVERY cell (unknown/blank kept as "" — never dropped)
   ▼
[5] emit one raw table per panel:
       { table_name, page_number, original_columns: [...verbatim...], rows: [ {col: val} ] }
   ▼
[6] persist verbatim → jnpa.berthing_report_tables (JSONB)     ← NEW, additive
   │   + one jnpa.berthing_report_documents row per file        ← NEW, additive
   ▼
[7] (unchanged) the normalised vessel rows continue to flow to jnpa.berthing_reports
       via the existing services/berthing pipeline — the two run side by side.
```

**Design principles**

- **Positional first, text second.** Column assignment is by word x-coordinate against header
  anchors, so interleaved panels (NSICT expected vs ICD/CFS) never cross-contaminate.
- **Verbatim capture.** `original_columns` stores the header tokens exactly as printed;
  `rows` stores exactly what each cell contains (including blanks and unknown columns). No
  renaming, no type coercion, no dropping — normalisation stays in the separate `0036` path.
- **Terminal templates are declarative** (a Python dict per terminal): list of panels, each
  with `{table_name, header_labels, x_min, x_max, column_anchors[]}`. Adding/adjusting a
  terminal is data, not new control flow.
- **Graceful degradation.** A panel that fails to bound (layout drift) is recorded as an
  extraction *warning* with its raw text preserved under `rows:[{"_raw": "<line>"}]` — still
  **no data loss**, and the validation report flags it.
- **Idempotent.** `pdf_hash` (sha256) dedups re-uploads exactly like the existing
  `berthing_import_files.file_hash`.

**Per-terminal parser design (the 4 templates)**

| Template | Terminals | Panels to extract (table_name) |
|---|---|---|
| `APMT` | APMT | TIME_TABLE, ON_BERTH_VESSEL, SAILED_VESSEL, VESSELS_EXPECTED, YARD_INVENTORY, GATE_MOVEMENTS, RAIL_PENDENCY, CFS_PENDENCY, TRAFFIC_THROUGHPUT, NOTES |
| `BMCT` | BMCT | TIDE_TABLE, YARD_INVENTORY, GATE_MOVEMENTS, VESSELS_ON_BERTHED, SAILED_VESSEL, VESSELS_EXPECTED, ICD_PENDENCY, CFS_PENDENCY, TRAFFIC_THROUGHPUT |
| `NSFT` | NSFT | TIDE_TABLE, VESSEL_SAILED_24H, VESSEL_AT_BERTH, VESSELS_EXPECTED, YARD_INVENTORY, GATE_MOVEMENT_24H, ICD_PENDANCY, CFS_PENDANCY |
| `DPWORLD` | NSICT, NSIGT | TIDE_TABLE, YARD_INV, VESSELS_ON_BERTH, SAILED_VESSELS, VESSELS_EXPECTED, ICD_PENDENCY, CFS_PENDENCY, TRAFFIC_THROUGHPUT |

---

## 6. Database migration plan (additive — nothing existing changes)

Keep unchanged: `jnpa.berthing_reports`, `jnpa.berthing_events`, `jnpa.berthing_import_files`,
`jnpa.berthing_import_errors` (migration `0036`). Add a **new** migration
`0037_berthing_report_documents.sql` mirroring the existing idempotent/`_ext` pattern
(`gateway/berthing_ext.py` gains matching DDL; a lock-step test asserts parity).

```sql
-- 0037_berthing_report_documents.sql — full verbatim PDF capture. Additive + idempotent.
CREATE SCHEMA IF NOT EXISTS jnpa;

CREATE TABLE IF NOT EXISTS jnpa.berthing_report_documents (
    id            bigserial PRIMARY KEY,
    file_name     text NOT NULL,
    terminal      text,                       -- APMT|BMCT|NSFT|NSICT|NSIGT (detected)
    report_date   date,                       -- parsed from PDF body
    pdf_hash      text UNIQUE,                 -- sha256(bytes) → idempotent re-upload
    page_count    integer,
    uploaded_by   text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jnpa.berthing_report_tables (
    id                bigserial PRIMARY KEY,
    document_id       bigint NOT NULL
                      REFERENCES jnpa.berthing_report_documents (id) ON DELETE CASCADE,
    terminal          text,
    table_name        text NOT NULL,           -- ON_BERTH_VESSEL, VESSELS_EXPECTED, ...
    page_number       integer NOT NULL DEFAULT 1,
    original_columns  jsonb NOT NULL,          -- ["Berth","Vessel","VIA","LOA","Alongside",...]
    rows              jsonb NOT NULL,           -- [{"Berth":"APM01","Vessel":"OOCL LUXEMBOURG",...}]
    row_count         integer NOT NULL DEFAULT 0,
    extraction_note   text,                    -- warnings (e.g. "panel-bound fallback")
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_brt_doc      ON jnpa.berthing_report_tables (document_id, id);
CREATE INDEX IF NOT EXISTS idx_brt_name     ON jnpa.berthing_report_tables (terminal, table_name);
CREATE INDEX IF NOT EXISTS idx_brdoc_hash   ON jnpa.berthing_report_documents (pdf_hash);
```

- Matches the user-specified column set (`berthing_report_documents`, `berthing_report_tables`)
  and adds only safe extras (`page_count`, `row_count`, `page_number`, `extraction_note`).
- `pdf_hash UNIQUE` → re-uploading the same PDF is a no-op (returns the existing document).
- JSONB preserves the exact table shape; querying is possible but the store is
  presentation-faithful, not normalised.

---

## 7. Extraction accuracy report (targets & risks)

Expected panel/row yield per representative file (04-Jun set), to be asserted by tests:

| Terminal | Panels (tables) | Vessel rows (on-berth / sailed / expected) | Other-panel rows | Extraction confidence |
|---|---|---|---|---|
| APMT | 9 + notes | 2 / 2 / ~20 | Yard 4, Gate 3, Rail N, CFS-matrix 3 bands, Throughput 3 | **Med-High** (CFS matrix = hard) |
| BMCT | 9 | 5 / 4 / ~45 | Yard 4, Gate 3, ICD N, CFS N, Throughput 3 | **High** |
| NSFT | 8 | 2 / 1 / ~24 | Yard 4, Gate 3, ICD N, CFS N | **High** (full datetimes, cleanest) |
| NSICT | 8 | 2 / 0 / ~32 | Yard 4, ICD N, CFS N, Throughput 3 | **Med** (expected/ICD/CFS interleave) |
| NSIGT | 8 | 1 / 0 / ~13 | Yard 4, ICD N, CFS N, Throughput 3 | **Med** (same as NSICT) |

**Accuracy method:** for each of the 25 files, the extractor emits `(pages, tables_found,
rows_total)`; the validation report compares against the expected panel set for the detected
terminal and lists: total pages, total tables, total rows, **missing sections**, and
**extraction errors/warnings**. Target: **100% of panels emitted** (a panel may be empty but
must appear), **0 cross-contaminated columns** on the vessel tables.

**Known hard cases (must be handled, not dropped):**

1. **APMT `CFS_PENDENCY` transposed matrix** — emit as-is: `original_columns=["Group","Teus"]`
   with one row per (group, teus) pair unpivoted, or a `_matrix:true` note preserving bands.
2. **NSICT/NSIGT expected ↔ ICD/CFS interleave** — hard x-range split (see §3.4).
3. **NSFT Dry/Reefer sub-columns** — two-line header → composite column names
   `Gate Open (Dry)`, `Gate Open (Reefer)`, etc.
4. **APMT/BMCT no-year dates** — keep the **verbatim** string in the raw table (year inference
   belongs only to the normalised path).
5. **Empty sailed rows (DP World)** — emit the berth code rows with blank cells (do not drop).
6. **`BERTHING_CT.pdf` / `BERTHING_GT.pdf`** — no filename date → parse `DATE:` from body.

---

## 8. UI design (preview before import)

After a PDF upload, the Data-Upload panel shows an **extraction preview** (read-only, pre-import):

```
File:            APMT_Berthing_Report_-_04-Jun-2026.pdf
Detected Terminal: APMT          Report date: 04-Jun-2026     Pages: 1

Extraction Result — Tables Found: 9
  1. ON_BERTH_VESSEL      Rows: 2    [Preview ▸]
  2. SAILED_VESSEL        Rows: 2    [Preview ▸]
  3. VESSELS_EXPECTED     Rows: 20   [Preview ▸]
  4. YARD_INVENTORY       Rows: 4    [Preview ▸]
  5. GATE_MOVEMENTS       Rows: 3    [Preview ▸]
  6. RAIL_PENDENCY        Rows: 12   [Preview ▸]
  7. CFS_PENDENCY         Rows: 35   [Preview ▸]
  8. TRAFFIC_THROUGHPUT   Rows: 3    [Preview ▸]
  9. TIME_TABLE           Rows: 2    [Preview ▸]

Validation:  pages 1/1 · tables 9 · rows 81 · missing sections: none · errors: 0
[ Confirm Import ]   [ Cancel ]
```

Each `[Preview ▸]` expands the raw table (original columns as headers, JSONB rows as-is). The
existing normalised **Vessel List / Dashboard / Timeline** tabs are unchanged; this is a new
**"Full Extract"** view on top of them. Endpoints (new, additive):
`POST /api/berthing/extract` (validate/preview, no write) and
`POST /api/berthing/extract/import` (persist verbatim) +
`GET /api/berthing/documents`, `GET /api/berthing/documents/{id}/tables`.

---

## 9. Implementation plan (phased, additive, non-breaking)

**Phase 0 — this audit** ✅ (docs, design, migration plan, accuracy targets).

**Phase 1 — DB (additive).** Add `infra/postgres/migrations/0037_berthing_report_documents.sql`
+ matching `gateway/berthing_ext._DDL` block + schema-parity test. No change to `0036` objects.

**Phase 2 — positional extractor.** New `services/berthing/full_extractor.py`:
`extract_words()` → panel segmentation → per-terminal templates (`APMT/BMCT/NSFT/DPWORLD`) →
`{table_name, original_columns, rows}`. Reuse existing `pdf_parsers.detect_terminal`. Pure,
DB-free, unit-testable. **No change** to the existing `pdf_parsers` / `upload_parsers` normalised path.

**Phase 3 — persistence + API.** `services/berthing/document_repository.py` (writes the two new
tables only) + new router endpoints (`/api/berthing/extract*`, `/documents*`). RBAC identical to
existing berthing (CONTROL_ROOM/CUSTOMS/ADMIN). Existing endpoints untouched.

**Phase 4 — UI.** New "Full Extract" tab in `web/src/screens/berthing/` with the preview above;
existing tabs untouched.

**Phase 5 — testing.** For **all 25 PDFs**, per terminal: assert every expected panel is
emitted, row counts within tolerance, zero cross-contamination on vessel tables, idempotent
re-upload. Add `tests/test_berthing_full_extract.py` (skip-if-data-absent for real files +
synthetic-text unit tests that always run, like the existing suite).

**Guardrails (unchanged, per requirements):**
- `jnpa.berthing_reports`, `jnpa.berthing_events` — **not modified**.
- Existing imported data (185 vessel-calls, 521 events) — **preserved**.
- Lifecycle logic — **untouched**.
- New capability is purely additive; the verbatim store never drops or renames source data.

---

## Appendix A — normalised vs full-extract (why both)

| | Normalised (existing, `0036`) | Full extract (this design, `0037`) |
|---|---|---|
| Purpose | Queryable vessel lifecycle, KPIs, timeline | Faithful archive of every panel on the page |
| Scope | Vessel rows only (on-berth/sailed/expected) | **All** tables (tide, yard, gate, pendency, throughput, …) |
| Fields | ~13 normalised columns | Verbatim `original_columns` + JSONB `rows` |
| Storage | `berthing_reports` / `berthing_events` | `berthing_report_documents` / `berthing_report_tables` |
| Extraction | text + regex anchors | word coordinates + panel templates |
| Data loss | intentional (only vessel fields) | **none** |

Both run from the same uploaded PDF, side by side.
