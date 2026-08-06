# Notice — Simulated Port Data API now accessible

**From:** JNPA Digital Twin Programme
**Contact:** dtinfo@jnport.gov.in
**Subject:** Access to the Simulated Port Data API for proof-of-concept evaluation

---

## 1. Purpose

The Simulated Port Data API is now available to bidders participating in the JNPA Digital Twin
proof of concept. It provides programmatic access to simulated equivalents of the sample data pack
already shared, so that data integration can be built and demonstrated without dependency on live
port systems.

## 2. Endpoint

```
https://dt.jnpa.in/poc-api-data-access/
```

The interface is available over HTTPS only.

## 3. Access credentials

Each participating organisation is issued a **client key**, derived from the electronic mail address
registered with JNPA for this procurement. The key is exchanged at the token endpoint for a bearer
token valid for one hour; the token is presented on all subsequent requests.

Keys are issued on request to **dtinfo@jnport.gov.in**. Please state:

1. the organisation name;
2. the single electronic mail address to be registered for API access.

A key is bound to the registered address. Requests presenting an unregistered key are refused.

## 4. What is available

Thirteen data groups corresponding to the folders of the sample data pack, covering the marine
vessel spine, customs, EDI messages, shipping line documents, gate documentation, rail, transport,
CFS and empty yard, and the daily terminal reports.

Data is retrieved in two stages. A group query returns indexed records containing metadata and a
file reference. The file reference is then exchanged for the file itself at a separate endpoint.

Responses are limited to data published at or before the instant a request is received.

## 5. Materials supplied

| Item | Description |
|---|---|
| `JNPA_API_Reference.pdf` | Interface specification: authentication, endpoints, parameters, responses, error codes |
| `JNPA_DigitalTwin.postman_collection.json` | Postman collection covering every endpoint, with assertions |
| `API_EXAMPLES.html` | Worked examples with actual requests and responses |
| `keygen.py`, `KEY_GENERATION.md` | Client key derivation and verification |

## 6. Conditions of use

1. The client key is a credential. It must not be shared outside the organisation to which it was
   issued, nor committed to any shared or public repository.
2. Access is provided solely for the purpose of evaluating and demonstrating a proposed solution
   under this proof of concept.
3. All data served is simulated. It does not represent actual vessel, cargo, customs or commercial
   activity at Jawaharlal Nehru Port, and must not be represented as such.
4. Requests are subject to a per-organisation rate limit. Sustained excessive load may result in
   access being suspended.
5. JNPA may amend or withdraw access at any point during the proof of concept.

## 7. Availability and support

The interface is provided on a best-effort basis for the duration of the proof of concept. Planned
interruptions will be notified in advance to the registered address.

Queries relating to access, credentials or interface behaviour should be directed to
**dtinfo@jnport.gov.in**, quoting the organisation name and, where relevant, the `requestId` value
returned in the response concerned.

---

*Issued by the JNPA Digital Twin Programme. This notice and the accompanying materials are
confidential and intended solely for organisations participating in the proof of concept.*
