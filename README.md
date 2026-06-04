# Daily Reservation Agent

Serverless AI reservation briefing agent for restaurant reservation emails.

## Gmail OAuth scopes

The agent needs these Gmail scopes:

- `https://www.googleapis.com/auth/gmail.modify`
- `https://www.googleapis.com/auth/gmail.compose`

Because `gmail.compose` is required for Gmail draft creation, regenerate `token.json`
locally after this scope change and update the `GOOGLE_TOKEN_JSON` GitHub Secret
with the new token JSON.
