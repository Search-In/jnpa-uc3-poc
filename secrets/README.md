# `secrets/` — gateway credential mount source

This directory is bind-mounted **read-only** into the gateway container at
`/run/secrets/firebase` (see the `gateway` service `volumes:` in
`docker-compose.yml`). Put runtime secrets that must live *outside* the image
here. **Nothing in this folder except this README is committed** (`.gitignore`).

## Firebase Admin SDK (FCM push + Phone-Auth verify)

1. Download the service-account JSON from the Firebase console:
   **Project settings → Service accounts → Generate new private key**
   (project `jnpa3-e23e8`).

2. Drop it here, e.g. `secrets/firebase-adminsdk.json`.

3. In your env file (`.env.prod` on the box, or `.env.local`) set:

   ```dotenv
   FIREBASE_PROJECT_ID=jnpa3-e23e8
   FIREBASE_SERVICE_ACCOUNT_PATH=/run/secrets/firebase/firebase-adminsdk.json
   ```

4. Recreate the gateway so it picks up the mount + env:

   ```bash
   deploy/jnpa-uc3.sh up            # or: docker compose ... up -d gateway
   ```

`gateway/firebase.py` loads the key lazily and logs `firebase_initialised` /
`firebase_boot ready:true` once it is valid. If the file is absent the gateway
logs `firebase_not_configured` and FCM stays disabled — WebPush + WebSocket
delivery are unaffected (graceful fallback).

The host directory can be relocated with `FIREBASE_SECRETS_DIR` (defaults to
`./secrets`).
