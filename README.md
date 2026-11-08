# Church website backend

This dependency-free Python backend serves the existing website, stores contact
and prayer requests in SQLite, provides a token-protected admin inbox, and
starts M-Pesa Daraja STK Push requests in Kenyan shillings.

## Run locally

Python 3.10 or later is required. From the website folder, set a private admin
token and start the server:

```powershell
$env:ADMIN_API_TOKEN = (python -c "import secrets; print(secrets.token_urlsafe(32))")
python .\backend\server.py
```

Open `http://127.0.0.1:8000` for the website and
`http://127.0.0.1:8000/admin` for the admin inbox. The website must be opened
through this server (not as a `file://` page) so its forms can reach the API.
Messages and donation records are stored in `backend/church.db`.

The admin inbox token is the value of `ADMIN_API_TOKEN`. Do not share it or
commit it; the server requires at least 32 characters. The admin page keeps it
in the current browser tab's session storage.

## Configure M-Pesa

Set the Daraja credentials as environment variables using `backend/.env.example`
as a reference. Start with `M_PESA_ENV=sandbox`. Set
`M_PESA_CALLBACK_URL` to the public HTTPS address ending in
`/api/mpesa/callback`; a local-only address cannot receive Safaricom callbacks.
For live payments, configure production Daraja credentials and a properly
deployed HTTPS server. In a hosted environment, set `HOST=0.0.0.0` and terminate
HTTPS at a trusted reverse proxy or hosting platform. Use
`M_PESA_TRANSACTION_TYPE=CustomerBuyGoodsOnline` for a Till number; the default
is `CustomerPayBillOnline`.

Payments are not marked paid based on the browser response. The backend checks
the Daraja callback against the STK Push Query API before marking a donation
paid. Unverified callbacks remain visible as `callback_received` in the admin
inbox and must not be treated as confirmed donations.

## API

- `POST /api/contact` — save a validated contact or prayer request.
- `POST /api/donations` — initiate an M-Pesa STK Push.
- `POST /api/mpesa/callback` — Daraja callback endpoint.
- `GET /api/admin/messages` — list recent inbox messages (Bearer admin token).
- `GET /api/admin/donations` — list recent gifts (Bearer admin token).
- `GET /api/health` — health check.

The admin endpoints return up to 500 records. Contact and prayer-request text
may contain sensitive information; restrict access to the machine and admin
token, deploy behind HTTPS, and apply an appropriate retention policy.
