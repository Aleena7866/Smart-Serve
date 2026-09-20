# SmartServe Email Confirmation Setup

SmartServe sends HTML confirmation emails for:

- Successful customer registration
- Successful provider registration
- Successful email/password login
- Successful Google login for an existing account
- New Google account creation and first login

Email delivery uses Gmail-compatible SMTP over STARTTLS. Authentication is never blocked if SMTP is unavailable; the error is logged and the user can still continue into SmartServe.

## Local configuration

Set these variables in `.env`:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-gmail-app-password
SMTP_FROM_EMAIL=your-email@gmail.com
SMTP_FROM_NAME=SmartServe
APP_BASE_URL=http://127.0.0.1:5000
```

For Gmail, `SMTP_PASSWORD` should be a Google App Password, not the normal Gmail password.

## Production

Use HTTPS for `APP_BASE_URL` and configure the SMTP credentials as deployment secrets/environment variables instead of committing `.env` to source control.
