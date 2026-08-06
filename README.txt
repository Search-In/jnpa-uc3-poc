JNPA DIGITAL TWIN — SIMULATED PORT DATA API
Proof of Concept · Materials for participating organisations

Endpoint      https://dt.jnpa.in/poc-api-data-access/
Contact       dtinfo@jnport.gov.in

CONTENTS

  NOTICE_API_ACCESS.md
      Notice of availability, how to obtain a client key, and conditions of use.
      Read this first.

  JNPA_API_Reference.pdf
      Interface specification. Authentication, endpoints, parameters, response
      fields, error codes and a worked sequence.

  JNPA_DigitalTwin.postman_collection.json
      Postman collection covering every endpoint, with assertions. Import it,
      set clientKey in the collection variables, run "0 · Authentication"
      first; the token is captured automatically.

  API_EXAMPLES.html
      Worked examples with actual requests and responses. Open in a browser.

  keygen.py  /  KEY_GENERATION.md
      Derive and verify a client key from the registered email address.
      Python 3.6 or later; no additional packages required.

GETTING STARTED

  1. Use the key_generation method to obtain the key, based on your registered email address on next cloud.
  2. Exchange the key for a bearer token at /v2/auth/token.
  3. List the data groups at /v2/groups.
  4. Retrieve records at /v2/groups/{group}/records.
  5. Retrieve a file at /v2/files/{fileRef} using the reference in a record.

All timestamps are Asia/Kolkata (+05:30). All data is simulated.
