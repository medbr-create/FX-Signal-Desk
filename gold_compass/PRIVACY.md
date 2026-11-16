# Privacy and security rules — Gold Trade Compass

This project is currently on a **PUBLIC GitHub repository**. All committed source code, branches, pull requests, commit messages, and publicly uploaded Actions artifacts are accessible to others. Making the repository private later does not undo copies made while it was public.

## Allowed in this public project
- Analysis code, technical strategy logic and synthetic test fixtures **only with the user's approval that the strategy is public during development**.
- Public historical XAUUSD market quote data and aggregate, nonpersonal research figures.
- Documentation without secrets, account identifiers, actual transactions, private screenshots or credentials.

## Never commit or upload
- FundingPips, Exness, MT5, broker account identifiers, passwords, investor passwords, tokens or API keys.
- Real account balances, deposit/withdrawal history, payout documents, actual trade logs, names, email addresses, phone numbers, IDs or screenshots of accounts.
- Telegram bot tokens, chat IDs, notification endpoints or other personal integrations.
- Private CSV/JSON historical imports from a personal account, unless all sensitive metadata is stripped and the user explicitly approves.
- Files in `private/`, `personal/`, `trade-journal/`, or any secrets files.

Use GitHub Actions secrets ONLY if a user explicitly authorizes adding a service later. The current read-only Gold Compass prototype uses no personal broker secrets, APIs or trade credentials.

**Limits of protection:** `.gitignore` is an accident-prevention convenience, not a security mechanism; users can forcibly commit ignored files. If a credential is ever committed to a public repo, removing it from the latest commit or making the repo private is not sufficient: rotate/revoke it and consider its history exposed. Review every commit and Actions log before making the tool public.
