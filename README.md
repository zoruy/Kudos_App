# Internal Kudos

A dependency-free Python web application that implements the approved Kudos specification: employee recognition, a paginated public feed, duplicate/rate-limit protection, and administrator moderation with an audit trail.

## Run locally

Requires Python 3.11+ (the project is currently using Python 3.14). No package installation is required.

```powershell
.\.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:8000`. The first run creates `kudos.db` and four development users. The login selector is a local-development stand-in for the organisation's SSO; production must integrate the existing identity provider and remove this route.

Choose **Riley Taylor (admin)** to access moderation. Choose any other employee to send and view kudos.

## Test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

## Notes

- `SPECIFICATION.md` is the approved functional and technical design.
- Runtime data (`kudos.db`) is deliberately ignored by Git; the application initialises its schema on startup.
- Production deployment should set the database location through `KUDOS_DB`, use the company authentication middleware, terminate TLS at the edge, and run schema migrations through the organisation's migration workflow.
