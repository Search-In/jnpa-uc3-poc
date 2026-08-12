# Draft emails to NLDSL

Two separate mails. **Mail 1** replies to ULIP support, closes out the queries
and requests production access — send with the test-case PDF attached.
**Mail 2** goes to `bd@nldsl.in`, which is where NLDSL asked us to send the two
items they classed as new requirements. Send Mail 1 first.

---
---

# Mail 1 — to ULIP Support

**Subject:** ULIP staging (rtoj_searchin_usr) — queries closed, test-case document attached, request for production access
**Attachment:** `ULIP_UAT_TestCases_JNPA_UC3.pdf`

---

Dear Team,

Thank you for the detailed responses. Every query we raised is now closed, and
we have re-run our full test cycle against staging with the information you
provided. **All thirteen granted APIs now return data successfully.** The
attached test-case document records each API's usage in our JNPA DTCCC
Use&nbsp;Case&nbsp;3 application, with screenshots of the running application
showing the input request and the corresponding output response.

Our confirmations against each of your points:

**1. LDB/01 — confirmed working.** Using the container you supplied
(`CXRU1145597`), the API responds correctly and we now retrieve a complete
thirteen-leg trail across vessel, rail and road, including Gateway Terminals
India (GTI), JNPT Central Parking Plaza and Khalapur toll plaza, each with
coordinates, transport mode and timestamp. This is integrated and evidenced in
the attached document.

One observation we have handled on our side, and mention only so you are aware:
on staging the API returns the same trail — for container `TCLU8538808` — for
*every* container number we send, including `CXRU1145597` and the
`NSST1234570` sample in the integration document. We take this as part of the
static test data you describe. Our application compares the container named in
the trail against the container requested and rejects a mismatch, so that one
container's port milestones can never be attributed to another.

**2. VAHAN/01 — understood.** Thank you for confirming that staging holds
static test data whose responses need not reflect production. Our application
discards any registration certificate whose registration number does not match
the number queried, so a mismatched answer can never reach a gate decision. We
would be grateful for confirmation that production `VAHAN/01` returns only the
vehicle requested.

**3. GATISHAKTI/01–03 — confirmed.** Thank you for the response structures. We
have verified our field mapping against all three and it matches exactly:

- `GATISHAKTI/01` — `road_name`, `gis_length`, `road_type`, `lane_statu`,
  `state_ut`. 33 rows retrieved for `NH-5`.
- `GATISHAKTI/02` — `infrastr_s`, `infrastr_a`, `infrastr_n`, `type_owner`,
  `storage_ca`, `type_infra`, `infrastr_v`. 13 rows retrieved for state 27.
- `GATISHAKTI/03` — `st_name`, `dist_name`, `sub_dist`, `vname`, `park_name`,
  `land_cat`, `land_avail`, `park_type`, `lat`, `lon`. 994 rows retrieved for
  state 27.

`GATISHAKTI/04` returns 59 toll plazas for Maharashtra and is fully integrated.

On road-network and corridor data with geometry: understood, and we are sending
a detailed requirement note to `bd@nldsl.in` as you have asked.

**One small documentation point for `GATISHAKTI/04`:** the integration document
shows a `vname` / `lat` / `lon` sample, whereas the API returns `plaza_name`,
`tollplazal` (latitude), `tollplaz_1` (longitude) and `nhno_new`. We have mapped
the actual field names. Updating the document would help other integrators, as a
client written to the sample receives zero rows with no error to indicate why.

**4. FASTAG/02 — understood.** Thank you for clarifying that tag availability
against a vehicle number depends on the test data configured in staging. The
tag-id path works correctly and is evidenced in the attached document.

We would still be grateful for a ruling on the pattern conflict in the
integration document for this API: the field table specifies
`^[A-Z0-9]{5,11}$|^[A-Z0-9]{17,20}$` while the 400-error sample shows
`[A-Z]{2}[0-9]{2}[A-Z]{0,5}[0-9]{4}$`. We currently validate against the looser
of the two.

**5. SARATHI/01 — working, thank you.** The test data you supplied resolves
correctly and the API is fully integrated. We retrieve the licence issue date,
the issuing state and RTO, eight classes of vehicle and the transport validity —
all fields `SARATHI/02` does not carry.

One finding worth recording: **the internal space in the DL number is
significant.** `GJ04 20120005008` resolves, while `GJ0420120005008` returns
`errorcd: -1`. Our application now preserves the spacing exactly as supplied. A
note in the integration document would save other integrators the same
diagnosis.

We also note that staging masks `bioNatName` as well as `bioFullName`
(`M*H*S*K*M*R* *O*I*`), whereas the response-schema document shows `bioNatName`
unmasked. We have not treated this as an issue given your PII position below,
but flag it in case the document is intended to reflect production behaviour.

**6. VAHAN/02 and VAHAN/03 — understood.** We accept the PII position. Both
endpoints are integrated and respond correctly; we are simply unable to
demonstrate a successful resolution, because the chassis and engine numbers
returned by `VAHAN/04` are masked and we have no other source for an unmasked
key. Those two test cases are therefore marked in the attached document as
blocked on data access rather than as failures. We are sending a requirement
note to `bd@nldsl.in` as you have asked.

**7. FASTag wallet balance — understood** and noted as outside ULIP's current
scope. Our application reports "balance not published by ULIP" rather than
displaying a figure, and no further action is required from your side.

---

## Two notes that would help other integrators

**`Accept: application/json` appears to be mandatory.** A request without it —
including the `Accept: */*` sent by default by most HTTP client libraries —
receives an `HTTP 400` with an **empty body**, giving no indication of the
cause. The identical request with the header succeeds.

**`HTTP 412` is returned before the credential is evaluated.** We observed 412
(`"Access denied Please contact ULIP support!"`) both for an unregistered
caller IP and for an unrecognised username, while a wrong password for a valid
username returns `401`. Documenting this distinction would let integrators tell
an allow-list problem from a credential problem immediately.

---

## Request for production access

Our integration is complete across all thirteen granted APIs, with automated
tests pinned against both the documented contracts and the live staging
responses. The attached document records 33 test cases: **26 passed**, 5 are
blocked solely on staging data access (the masked VAHAN identifiers and two
SARATHI licence states), and 2 record behaviour that differs from our original
expectation and is explained in place. No test failed because of a defect in
the integration.

**We therefore request production API access for the same thirteen APIs.**
Please advise the process, the production endpoint, and whether our calling IP
`65.2.212.121` should also be registered for production, or whether a different
address should be used.

We would be glad to join a call if that would help.

Thank you again for your support throughout.

Best regards,

[Name]
[Designation]
Search-In Solutions
JNPA DTCCC — Use Case 3
[Phone] · [Email]

---
---

# Mail 2 — to bd@nldsl.in

**Subject:** New requirement note — road-network geometry and unmasked vehicle identifiers for JNPA DTCCC (Use Case 3)

---

Dear Team,

ULIP Support has asked us to write to you with two requirements that fall
outside our current API grant. We are the systems integrator for the JNPA
Direct Trade Coordination & Control Centre (DTCCC), Use Case 3 — truck
movement, gate automation and vehicle intelligence at Jawaharlal Nehru Port. We
hold staging access for account `rtoj_searchin_usr` and have completed our
integration of all thirteen granted APIs.

## Requirement 1 — road network and corridor data with geometry

**What we need.** Road-network geometry for the corridors serving JNPA:
polylines or ordered coordinate sequences for national and state highways,
with segment identity, so that a road can be drawn and a position can be
placed *along* it.

**Why.** JNPA handles a very large share of India's container traffic, and
almost all of it arrives and leaves by road on a small number of corridors,
principally NH-348. The DTCCC's purpose is to see congestion forming on those
corridors and act before it reaches the gate. To do that we must project live
truck positions — which we obtain from FASTag toll crossings and LDB container
movements, both through ULIP — onto the road they are travelling on. That
requires the road's shape.

**The gap.** Of the granted APIs, `GATISHAKTI/01` returns road attributes
(name, type, lane status, GIS length) but no coordinates; `GATISHAKTI/02`
returns storage infrastructure; `GATISHAKTI/03` returns industrial parks with
point coordinates. None returns road geometry. We can therefore describe a
highway but cannot place a truck on it, which limits corridor congestion
analysis to point observations rather than continuous flow.

**What this would enable.** Corridor-level congestion and bottleneck detection
ahead of the port gate; estimated time of arrival at the gate computed along
the actual route rather than as a straight line; identification of the specific
stretch where delay is accumulating; and evidence-based diversion advice to
transporters. Each of these is a stated DTCCC objective.

**Specifically, we would request** either `GATISHAKTI/05` (national corridors),
which the integration documents describe but which is not in our grant, if it
carries geometry; or any equivalent API exposing road-network geometry, ideally
filterable by state and by NH number. GeoJSON or an ordered coordinate list
would both suit us.

## Requirement 2 — unmasked chassis and engine numbers

**What we need.** For `VAHAN/02` (lookup by chassis number) and `VAHAN/03`
(lookup by engine number) to be usable, we need chassis and engine numbers that
are not masked — either unmasked in the `VAHAN/04` response for our account, or
the ability to query `VAHAN/02` and `VAHAN/03` with an unmasked value we capture
physically at the gate.

**Why.** These two APIs exist to answer one question: *which vehicle is this,
when the number plate cannot be trusted?* That situation is routine at a port
gate — a damaged, obscured, repainted or duplicated plate — and it is precisely
the situation in which correct identification matters most, because it is also
how a vehicle evades enforcement. The chassis and engine numbers are stamped on
the vehicle and can be read physically by gate staff.

**The gap.** `VAHAN/04` returns these fields masked (`ME4JF509AH70*****`,
`JF50E760*****`). A masked value cannot be used as a lookup key, so today the
two APIs can only return "not found" for any vehicle we encounter. We have no
alternative source for an unmasked identifier.

**What this would enable.** Positive vehicle identification at the gate when
the plate is unreadable or disputed; a cross-check between the chassis and
engine numbers to detect a tampered vehicle; and support for law-enforcement
follow-up, which currently cannot proceed on a masked identifier.

**We recognise this is PII-sensitive** and are not asking for a blanket
relaxation. We would welcome any controlled arrangement — restriction to these
two APIs, audit logging of every such lookup on our side, a data-sharing
undertaking, or gating the capability behind the port operator's authorisation.
We already log every ULIP call we make and can share that audit trail.

## About us

The integration is complete and in operation against staging, covering FASTag,
LDB, VAHAN, SARATHI and GatiShakti. We have submitted a full test-case document
with screenshots to ULIP Support in support of our production-access request,
and would be happy to share it with you, or to walk through the use case on a
call, if that would help you assess these requirements.

Best regards,

[Name]
[Designation]
Search-In Solutions
JNPA DTCCC — Use Case 3
[Phone] · [Email]
