# Working agreements

- Keep code explicit, typed where practical, and maintainable. Handle network failures, privacy and timezone boundaries.
- Do not run builds, tests, linters, formatters, indexers or other checks without the user's explicit request. Explain implementation and unverified behavior candidly.
- Never create, edit, delete, restore or commit `.cursorignore`; never run commands that change it, unless the user explicitly asks to change that file.
- Never run or suggest `ya tool aisuite setup --once .`.
- Never commit secrets, Telegram sessions, `.env`, API keys, SSH keys or databases.
- Keep deployment source-based: the server fetches Git and builds its own Docker image. No container registry.
- Production deployment is disabled until the owner sets repository variable `DEPLOY_ENABLED=true`.
