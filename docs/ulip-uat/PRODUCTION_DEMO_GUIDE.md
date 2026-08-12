# ULIP production demo guide — 13 granted APIs

**Live URL:** https://traffic-three.searchintech.in
**Every value below was confirmed answering on 2026-08-13.** Ops detail lives in
`PRODUCTION_CUTOVER.md`; this file is the demo script.

---

## Before you start — 4 checks, 3 minutes

1. **Set the header toggle to LIVE.** The app boots in **DEMO** and the toggle sits next
   to the search bar. In DEMO nothing below reaches ULIP. This is the single most likely
   way the demo goes wrong.
2. **Smoke-test the grant** (from a machine that can tunnel to the whitelisted IP):
   ```bash
   ssh -i ~/Downloads/jnpa3.pem -D 127.0.0.1:1081 -N ec2-user@65.2.212.121 &
   set -a && . ./.env.local && set +a
   ULIP_PROXY=socks5://127.0.0.1:1081 python3 scripts/ulip_smoke.py --state-id 27
   ```
   Exit `0` = 13/13. Exit `2` = the egress IP has fallen off NLDSL's allowlist — stop and
   contact ULIP support; nothing live will work.
3. **Confirm today's FASTag vehicle** (see the ⚠️ below — this expires daily).
4. **System Health** → all green, `Gateway` and `Database (RDS)` healthy.

---

## Coverage — 8 of the 13 APIs have a screen

Say this up front rather than let it be discovered. Five are real and working but have no
UI, by design.

| API | Screen | Where |
|---|---|---|
| `FASTAG/01` | FASTag → **Transactions** | `/fastag` |
| `FASTAG/02` | FASTag → **Tag Status** | `/fastag` |
| `GATISHAKTI/04` | FASTag → **Toll Enroute** | `/fastag` |
| `LDB/01` | System Health → Integrations → **LDB** | `/health?tab=integrations` |
| `VAHAN/04` | Vehicle Management → RC Lookup → **Registration** | `/vehicles?tab=rc-lookup` |
| `VAHAN/02` | Vehicle Management → RC Lookup → **Chassis** | same |
| `VAHAN/03` | Vehicle Management → RC Lookup → **Engine** | same |
| `SARATHI/02` | Vehicle & Driver Intelligence → **Driver** | `/intelligence` |

| No screen | Why | Show it via |
|---|---|---|
| `VAHAN/01` | automatic retry behind `VAHAN/04`; never independently visible | — |
| `SARATHI/01` | needs a date of birth; no screen has that input | API |
| `GATISHAKTI/01` · `02` · `03` | backend reference data — road attributes, storage depots, industrial parks. Nothing on screen consumes them | API |

---

## ⚠️ The one step that expires

`FASTAG/01` keeps **72 hours** of history. As of this pass:

- **`CG07BC9186`** → 4 crossings (Pundag, Patrachauli, Anjan Dham, Lodam), **56–61 hours
  old**. They age out at roughly **19:20 IST on 13 Aug**. After that this vehicle shows an
  empty table.
- `MH14LE9625` → 0 crossings.

**On the morning of the demo, find a vehicle that answers today:**
```bash
ULIP_PROXY=socks5://127.0.0.1:1081 python3 scripts/ulip_smoke.py --state-id 27 | head -6
```
If the FASTAG/01 line shows 0 crossings, use the **History** tab instead — it reads rows
already persisted in RDS and is immune to the 72-hour window. Say plainly that it is
stored history, not a live fetch; do not present it as live.

---

## The demo — a truck arriving at the gate

Eight steps, one narrative. Roughly 10 minutes.

### 1. Who is this vehicle? — `VAHAN/04`
`/vehicles?tab=rc-lookup` → **Registration** tab → type **`MH14LE9625`** → **Look up**.

Expect the full certificate: Motor Car, PETROL, RTO Pimpri Chinchwad (MH14), fitness to
**2039-02-13**, insurance to **2027-02-11**, PUC to **2026-09-08**, blacklist **CLEAR**.

**Point at:** the `VAHAN/04` and `LIVE_PRIMARY` badges, top right of the card. That is the
national register answering, not our database.
**Say:** the owner name is masked `A****A S******A C*****E` — VAHAN masks it for every
user; we store it exactly as received and never re-mask or use it as a key.

### 2. Is the driver licensed? — `SARATHI/02`
`/intelligence` → **Driver** tab → type **`GJ04 20120005008`** → **Search**.

Expect the DL card: status **VALID**, `LIVE_PRIMARY`, valid to **2027-09-04**, class list
led by *Motor Cycle with Gear(Non Transport)* — 3 classes.

**Say:** *Issued* and *Issuing RTO* are blank here because `SARATHI/02` does not carry
them; `SARATHI/01` does, and needs the holder's date of birth (step 9).

### 3. Is its FASTag good? — `FASTAG/02`
`/fastag` → type **`MH14LE9625`** → **Search** → **Tag Status** tab.

Expect one row: tag `34161FA820328EE82ED1BDC0`, **Activated**, class VC4, bank 608116,
issued 2024-02-14.

Lookup by tag ID also works: **Tag Status** → the Tag-ID box →
**`34161FA8203286140F4064E0`** → **Check Tag** → returns `MAT735701`.

### 4. Where has it been? — `FASTAG/01`
Same screen → **Transactions** tab, with **today's confirmed vehicle** (see ⚠️ above —
`CG07BC9186` if before ~19:20 IST on 13 Aug).

Expect toll crossings with plaza name, geocode, lane direction and timestamp.

**Say before clicking:** only the last 72 hours exist upstream; the poller accumulates
history beyond that.

### 5. What is on its route? — `GATISHAKTI/04`
Same screen → **Toll Enroute** tab. Source state must be typed **`Maharashtra`** (the name
maps to LGD code 27 — "Mumbai" or "MH" will not resolve).

Expect **59 NHAI plazas** — Anewadi (NH-48), Arjunali (NH-160), Baswant (Pimpalgaon)… each
with coordinates.

**Say:** cost is deliberately blank — no granted API publishes a tariff, and a made-up fare
would be read as real money. Note the registry is NHAI state-wide, so there is **no JNPA /
Nhava Sheva plaza in it**; don't promise one.

### 6. Where is its container? — `LDB/01`
`/health?tab=integrations` → **LDB** tab → **`TCLU8538808`** → **Track**.

**Say before clicking: this takes 15–20 seconds.** That pause is the national gateway, not
the app. Announce it first; silence during a demo reads as a hang.

Expect the LDB card **LIVE / Configured: Yes**, then the tracking card with the **ULIP**
badge — Status IDLE, Location *Raigad/Gateway Terminals India (GTI)*, Last Event **PORT
OUT** — and **Movement History: 13** legs: PORT OUT, PORT IN, GATE OUT, TOLL PLAZA CROSSED
(Khalapur, Mumbai–Pune Expressway), ICD IN (CONCOR Aurangabad), CFS OUT (Navkar, Panvel).

**Use `TCLU8538808`, not `CXRU1145597`.** Both work, but `CXRU1145597` measured **35.5 s**
against 14.5 s — long enough that the browser may give up first.

### 7. The plate is unreadable — `VAHAN/02` and `VAHAN/03`
Same RC Lookup screen → **Chassis** tab → `ME4JF509AH707` → **Look up**.

Expect: *"VAHAN has no vehicle registered against this number."*

**This is the correct answer, and it is worth showing deliberately.** VAHAN masks chassis
and engine numbers (`MBLHAR087JHK*****`), so a masked value cannot be used as a key. The
endpoints work — they simply have nothing to match. We have asked NLDSL (`bd@nldsl.in`) for
unmasked access. Two honest sentences here are better than skipping the tab and being asked.

### 8. Is any of this invented? — the integrity story
- `/fastag` → **Balance** tab: *"Wallet balance is not published by ULIP"* — an explicit
  refusal, not a zero and not a guess.
- `/health` → **Integrations**: `LDB` reads **LIVE**, `PDP` and `RMS-TAS` read **MOCK**.
  Every panel declares its own provenance.
- Chassis/engine returning empty rather than a plausible-looking vehicle.

**This is the strongest part of the demo.** The system says what it does not know.

---

## 9. The five APIs without a screen

Every `/api/*` route needs a bearer token. Get one:

```bash
TOKEN=$(curl -s -X POST https://traffic-three.searchintech.in/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<user>","password":"<password>"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
```

(The UI stores the same token in localStorage under `jnpa_uc3_token`. Swagger at
`/api/docs` is auth-gated, so it cannot be opened by plain navigation.)

```bash
B=https://traffic-three.searchintech.in
H="Authorization: Bearer $TOKEN"

# SARATHI/01 — the richer licence: issue date, issuing state and RTO
curl -s -H "$H" "$B/api/vahan/dl/GJ04%2020120005008?dob=1987-05-26"
#   -> date_of_issue 2012-03-07, state Gujarat, rto_code GJ33, 3 classes

# GATISHAKTI/01 — national-highway attributes            -> 33 rows for NH-5
curl -s -H "$H" "$B/api/gatishakti/roads?nh_no=NH-5&limit=5"
# GATISHAKTI/02 — food-storage depots by state           -> 13 rows for 27
# GATISHAKTI/03 — industrial parks with coordinates      -> 497 unique for 27
curl -s -H "$H" "$B/api/gatishakti/road-points?state_id=27&limit=5"
# GATISHAKTI/04 — NHAI toll plazas                       -> 59 for 27
curl -s -H "$H" "$B/api/gatishakti/toll-plazas?state_id=27&limit=5"
```

`VAHAN/01` cannot be demonstrated on its own: it fires only when `VAHAN/04` misses, and the
two map to the same record, so the card cannot tell you which replied.

---

## What not to promise

| Question | Honest answer |
|---|---|
| "Can it show FASTag balance?" | No. No granted ULIP API publishes a wallet balance — confirmed by NLDSL as outside scope. |
| "Look up a truck by chassis number?" | The endpoint works, but VAHAN masks chassis and engine, so nothing matches. Raised with `bd@nldsl.in`. |
| "Draw the route on a map?" | No granted API returns road geometry — `GATISHAKTI/01` has attributes but no coordinates. Raised as a requirement. |
| "Show the owner / driver name?" | Masked at source for every ULIP user, including `SARATHI/01`. |
| "Is toll history building up?" | Not yet. The poller sweeps `core.gate_event`, which the simulator fills with synthetic plates production NETC has never seen. It will accumulate once real vehicles pass the gate. |
| "Why did that take 20 seconds?" | LDB/01 aggregates a container's whole trail upstream; 14–36 s measured. Everything else is sub-second. |

---

## If it goes wrong

| Symptom | Cause | Do |
|---|---|---|
| Every screen shows tidy but unfamiliar data | Header toggle left on **DEMO** | Switch to **LIVE** |
| FASTag Transactions empty | Crossings aged past 72 h | Use the **History** tab, and say it is stored history |
| Container tracking fails or spins | Slow container (`CXRU1145597` ~35 s) | Use `TCLU8538808`; retry once |
| Toll Enroute lists nothing | Source state not `Maharashtra`, or registry unseeded | Type the full state name; re-seed per `PRODUCTION_CUTOVER.md` |
| Everything ULIP fails at once | `412` — egress IP off the allowlist | Contact ULIP support; demo the DEMO mode instead |

---

## Two deployment notes

- **The frontend LDB timeout is still 35 s in the deployed bundle.** The fix raising it to
  70 s is committed but needs a **web container rebuild** to take effect. Until then, slow
  containers can abort in the browser even though the gateway answers — which is why step 6
  specifies `TCLU8538808`.
- **The Toll Enroute fix is hot-copied onto the box**, not yet part of a built image. It is
  committed; fold it into the next proper deploy so a rebuild does not silently revert it.
