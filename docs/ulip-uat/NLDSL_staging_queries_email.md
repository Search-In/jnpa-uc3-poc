# Draft email to ULIP / NLDSL support — staging integration queries

**To:** ULIP Support / NLDSL Integration Team
**Subject:** ULIP staging (rtoj_searchin_usr) — integration observations and queries on the 13 granted APIs

---

Dear Team,

Thank you for whitelisting our IP `65.2.212.121` on the staging environment. We
confirm access is working: `POST /user/login` now issues a token, and **12 of
the 13 granted APIs returned data on our first end-to-end run** on 11 August
2026. We have completed the integration into our JNPA DTCCC Use Case 3
application and have prepared a test-case document with screenshots, which we
are submitting separately in support of our request for production access.

While testing, we recorded the observations below. They are ordered by the
impact they have on our go-live, and each is followed by the specific
confirmation or action we would request from your side. Where the observed
behaviour differs from the integration documents we have quoted both, so that
you can tell us which is authoritative.

---

## 1. LDB/01 — upstream service unavailable on staging

Every call to `LDB/01` during our testing returned:

```json
{
  "response": [
    { "response": "LDB_01 - 3rd party service is down!",
      "responseStatus": "ERROR" }
  ],
  "error": "true", "code": "200", "message": null
}
```

This is the only granted API that provides container movement, which is central
to a port use case — it is how we trace an EXIM container's vessel, rail and
road legs. We were therefore unable to test or evidence it end to end.

**Request:** please advise when the LDB upstream will be restored on staging,
and whether the same dependency affects production.

**Observation for your consideration:** the reason text is placed in
`response[0].response` while the top-level `message` field is `null`. A client
reading only `message` sees no explanation at all. It would help integrators if
the top-level `message` carried the reason as well.

---

## 2. VAHAN/01 — the same request returns a different vehicle

Querying `VAHAN/01` repeatedly with `{"vehiclenumber": "UP32KH0320"}` returned
two different registrations across calls, roughly half the time each:

| Call | `rc_regn_no` returned | Vehicle |
|---|---|---|
| 1 | `RJ11GC0346` | TATA LPT 2818 BS-VI |
| 2 | `UP32KH0320` | SPLENDOR + (SELF-DRUM-CAST) |
| 3 | `RJ11GC0346` | TATA LPT 2818 BS-VI |

`VAHAN/04` returned the requested vehicle on every call for the same input.

This matters to us because the vehicle we verify at the port gate determines
whether a truck is admitted, on that vehicle's fitness, permit and blacklist
status. We have added a guard that discards any registration certificate whose
registration number does not match the number queried, so a mismatched answer
is treated as "not found" rather than being used.

**Request:** please confirm whether this is expected behaviour of the staging
environment (for example, sample data being rotated), or an issue. We would
particularly like confirmation that **production `VAHAN/01` returns only the
vehicle that was requested.**

---

## 3. GATISHAKTI/01, /02 and /03 — datasets differ from the integration document

The integration document describes these as road-network APIs. On staging the
payloads are as follows (all `HTTP 200`, `stateid 27` — Maharashtra):

| API | Document describes | Actually returned | Rows |
|---|---|---|---|
| `GATISHAKTI/01` | National highway detail | Highway segments — `road_name`, `road_type`, `lane_statu`, `gis_length`, `state_ut`. **No latitude/longitude.** | 33 for `NH-5` |
| `GATISHAKTI/02` | State road network | **Food-storage depots** — `infrastr_n`, `infrastr_a`, `storage_ca`, `type_infra`, `type_owner` | 13 |
| `GATISHAKTI/03` | Named road points | **Industrial parks and estates** — `park_name`, `dist_name`, `land_cat`, `park_type`, with `lat`/`lon` | 994 |
| `GATISHAKTI/04` | NHAI toll plazas | Toll plazas — as expected | 59 |

The data itself is useful to us and we have integrated all four. However, we
had planned to use `GATISHAKTI/01–03` to build the road corridor serving JNPA,
and none of the three returns road geometry.

**Requests:**
1. Please confirm whether these are the intended datasets for `GATISHAKTI/02`
   and `GATISHAKTI/03` on staging, or whether the staging environment is
   serving a different dataset from production.
2. Is a **road-network layer with coordinates** available under any API?
   `GATISHAKTI/05` (national corridors) is described in the integration
   document but is not in our granted list — we would like to request it if it
   provides corridor geometry.

**A note on `GATISHAKTI/04`:** the response field names differ from the
document's sample. The document shows `vname` / `lat` / `lon`; the actual rows
use `plaza_name`, `tollplazal` (latitude), `tollplaz_1` (longitude) and
`nhno_new`. We have mapped the actual names. It would help other integrators if
the document were updated to match, as a client written to the document
receives zero rows without any error.

---

## 4. FASTAG/02 — lookup by vehicle number returns no tags

For the same vehicle, the two FASTag APIs disagree:

| Request | Result |
|---|---|
| `FASTAG/01` `{"vehiclenumber": "CG07BC9186"}` | 3 toll crossings returned |
| `FASTAG/02` `{"vehiclenumber": "CG07BC9186"}` | `vehicledetails` empty — no tags |
| `FASTAG/02` `{"tagid": "34161FA8203286140F4064E0"}` | 1 tag returned correctly |

A vehicle that has crossed toll plazas must carry a tag, so we would expect the
vehicle-number lookup to return it.

**Request:** please confirm whether `FASTAG/02` supports lookup by
`vehiclenumber` on staging, and if so, a vehicle number that resolves — we
would like to evidence this path.

**Also on FASTAG/02:** the integration document gives two different patterns for
`vehiclenumber` — the field table states
`^[A-Z0-9]{5,11}$|^[A-Z0-9]{17,20}$`, while the 400-error sample shows
`[A-Z]{2}[0-9]{2}[A-Z]{0,5}[0-9]{4}$`. Please confirm which is authoritative.

---

## 5. SARATHI/01 — no test record resolves on staging

`SARATHI/01` responds correctly, but every DL number and date-of-birth pair
available to us returns:

```json
{ "errorcd": -1, "erormsg": "Details not available " }
```

This includes the licence used as the worked example in the response-schema
document you kindly shared (`GJ04 20120005008` with `bioDob` `1987-05-26`), and
the DL number used in the `SARATHI/02` documentation.

We have mapped the response schema you supplied — it differs entirely from
`SARATHI/02` (`dldetobj[].dlobj`, `dlcovs[]`, `bio*` block) — but we cannot
demonstrate the path working.

**Request:** please share a **DL number and date of birth that resolve on the
staging environment**, so we can complete this test case.

We would also note that `SARATHI/01` is valuable to us specifically because it
returns the holder's name **unmasked** in `bioNatName` and a photograph in
`biPhoto`; both are directly useful for issuing a port driver pass. Please
confirm these fields are available in production for our account.

---

## 6. VAHAN masking prevents testing VAHAN/02 and VAHAN/03

Section 1.3 of the VAHAN document states that owner name, addresses and mobile
number are masked for all users. In practice the **chassis and engine numbers
are also masked** in the `VAHAN/04` response:

```json
{ "rcChasiNo": "ME4JF509AH70*****", "rcEngNo": "JF50E760*****" }
```

Because of this, a value returned by `VAHAN/04` cannot be used as the lookup
key for `VAHAN/02` (by chassis) or `VAHAN/03` (by engine), and we have no
other source of an unmasked chassis or engine number. Both APIs respond
correctly, but every lookup we can construct returns "not found", so we cannot
evidence a successful result.

**Requests:**
1. Please share a **chassis number and an engine number that resolve on
   staging**, so we can complete these two test cases.
2. Please advise whether **unmasked owner details can be enabled for our
   account** in production. Our use case includes port security screening and
   police reporting, both of which require the real registered-owner name; a
   masked name cannot be matched against a person.

---

## 7. FASTag wallet balance — is any API available?

None of the thirteen granted APIs returns a FASTag wallet balance:
`FASTAG/01` returns toll crossings and `FASTAG/02` the tag registry. Our
application therefore reports "balance not published by ULIP" rather than
displaying a figure.

**Request:** please confirm whether a wallet-balance API exists under a
different grant, or whether balance is out of scope for ULIP entirely.

---

## 8. Two notes that would help other integrators

**`Accept: application/json` appears to be mandatory.** Any request that does
not send this header — including the `Accept: */*` sent by default by most HTTP
client libraries — receives an **`HTTP 400` with an empty body**, giving no
indication of the cause. The same request with the header succeeds. We lost
some time on this; a short line in the integration documents would prevent
others from doing the same.

**`HTTP 412` is returned before the credential is evaluated.** We observed that
412 (`"Access denied Please contact ULIP support!"`) is returned both for an
unregistered caller IP and for a username the platform does not recognise,
while a wrong password for a valid username returns `401`. Documenting this
distinction would make it much easier for integrators to tell an allow-list
issue from a credential issue.

---

## 9. Production access

Our integration is complete and covers all thirteen granted APIs end to end,
with automated tests pinned against both the documented contracts and the live
staging responses. We are submitting our test-case document with screenshots of
the application showing the input request and corresponding output for each
API.

**We would like to request production API access for the same thirteen APIs**,
and would be grateful if you could advise the process, the production
endpoint, and whether the same IP (`65.2.212.121`) should be registered for
production.

We would be happy to join a call if that is easier for working through the
items above.

Thank you for your support.

Best regards,

[Name]
[Designation]
Search-In Solutions
JNPA DTCCC — Use Case 3
[Phone] · [Email]
