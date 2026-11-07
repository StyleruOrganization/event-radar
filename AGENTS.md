# Working agreements

- Keep code explicit, typed where practical, and maintainable. Handle network failures, privacy and timezone boundaries.
- Do not run builds, tests, linters, formatters, indexers or other checks without the user's explicit request. Explain implementation and unverified behavior candidly.
- Never create, edit, delete, restore or commit `.cursorignore`; never run commands that change it, unless the user explicitly asks to change that file.
- Never run or suggest `ya tool aisuite setup --once .`.
- Never commit secrets, Telegram sessions, `.env`, API keys, SSH keys or databases.
- Keep deployment source-based: the server fetches Git and builds its own Docker image. No container registry.
- Production deployment is disabled until the owner sets repository variable `DEPLOY_ENABLED=true`.
- Only OWNER_TELEGRAM_ID in a private bot chat may change settings or trigger AI. An explicitly owner-selected private group/channel may receive cached owner reports; its members cannot control the bot or trigger requests. Never expose the home address, personal travel estimate, or reviews in shared output.
- AI uses the official Sign in with ChatGPT OAuth flow. Never add API-key billing fallback, copy Codex auth, or scrape browser credentials.
- Runtime OAuth credentials rotate on the server; never send them through GitHub Secrets or include them in database backups.

- Collect channel posts and invoke AI only while preparing a scheduled or explicitly requested report. Preserve the last successful report boundary on any failure; batch publication analysis.
- Preserve local subscriptions/settings when migrating to the server; use consistent SQLite backup and refuse overwriting an existing destination.
