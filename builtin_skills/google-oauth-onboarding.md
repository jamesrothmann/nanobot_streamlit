---
name: google-oauth-onboarding
description: Interactive Google OAuth onboarding through chat for Streamlit Cloud deployments. Use when the user wants one-time OAuth setup and a copy/paste secrets block without running local scripts.
---

# Google OAuth Onboarding In Chat

Use this sequence:

1. `google_oauth_onboarding_start(client_id, client_secret, user_email)`
2. User opens verification URL and approves access
3. `google_oauth_onboarding_finish(onboarding_id, wait_seconds)`

If needed:

- `google_oauth_onboarding_status(onboarding_id)`

## Result

`google_oauth_onboarding_finish` returns a `[google_oauth]` secrets block to paste into Streamlit secrets.
After pasting, redeploy/restart to apply.

