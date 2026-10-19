# Deploying the hosted on-call market

The app is one Streamlit script (`streamlit_app.py` plus the `app/` package) that stores
its data in a Postgres database. In production it runs on Streamlit Community Cloud (free)
against a free Neon Postgres database; locally it runs against a SQLite file. This guide is
written for the owner and goes step by step. Budget about an hour the first time.

## What you need

- A GitHub account (the code is deployed from a **public** repository).
- A Neon account (https://neon.tech, free tier).
- A Streamlit Community Cloud account (https://share.streamlit.io, sign in with GitHub).
- Python 3.11, 3.12 or 3.13 locally to try it before deploying (3.14 also works for local runs,
  but Community Cloud offers 3.12, so pick that there).

## 1. Run it locally first

From the repository folder:

```bash
python -m pip install -r requirements.txt
```

```bash
python -m streamlit run streamlit_app.py
```

The browser opens at http://localhost:8501. Without any configuration the app uses
`oncall_local.db`, a SQLite file in the folder (gitignored). Create a cycle on the landing
page and walk through the organiser guide (`docs/organiser-guide.md`).

To run the app's tests (about 30 seconds):

```bash
python -m pytest tests/test_app_repo.py tests/test_app_adapter.py tests/test_app_prefs.py tests/test_app_smoke.py -q
```

(On PowerShell the pattern `tests/test_app_*.py` is not expanded, which is why the files
are listed.)

## 2. Create the database on Neon

1. Sign in to Neon and create a project (free tier). Pick the region closest to the group.
   The default database name `neondb` is fine.
2. On the project dashboard click **Connect** (or "Connection details"). Choose the
   **pooled** connection and copy the connection string. It looks like
   `postgresql://neondb_owner:XXXX@ep-something-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require`.
3. Treat this string as a password. Do not put it in any file that is committed; it goes
   into the Streamlit secrets box in step 4.

The app accepts `postgresql://` and `postgres://` URLs and adds the driver name itself.

## 3. Put the code on GitHub

1. Create a new **public** repository on GitHub (any name, e.g. `oncall-market`). Public is
   required for the free Streamlit tier.
2. Push the `main` branch of this folder to it:

```bash
git remote add origin https://github.com/<you>/oncall-market.git
```

```bash
git push -u origin main
```

Before pushing, check that `git status` shows no database file, no `.streamlit/secrets.toml`
and no filled-in preferences sheet: `.gitignore` excludes `*.db`, `.streamlit/secrets.toml`,
`schedule.csv`, `summary.csv`, `preferences.csv` and `budgets.csv`. The repository must
contain code and the example sheet only.

## 4. Deploy on Streamlit Community Cloud

1. Sign in at https://share.streamlit.io with GitHub and click **Create app** (or "New app").
2. Choose "Deploy a public app from GitHub", then:
   - Repository: the one you just pushed.
   - Branch: `main`.
   - Main file path: `streamlit_app.py`.
   - App URL: pick a short subdomain, e.g. `oncall-market`; the app will live at
     `https://oncall-market.streamlit.app`.
3. Open **Advanced settings**:
   - Python version: **3.12**.
   - Secrets: paste one line, with your Neon string inside the quotes:

     ```toml
     DATABASE_URL = "postgresql://neondb_owner:XXXX@ep-something-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
     ```

4. Click **Deploy**. The first build installs the pinned packages from `requirements.txt`
   and takes a few minutes. The first visit to the app creates the tables in Neon (there is
   no migration step; the schema is created with `create_all`).
5. Open the app. You should see the landing page with "Create a new cycle".

The secrets can be changed later under the app's **Settings > Secrets**; the app restarts
when they change.

## 5. Set the app address once

Create your first cycle from the landing page. The organiser link is shown once; save it.
Under **Setup** the field "App address used to build the links" is prefilled from the page
address (for example `https://oncall-market.streamlit.app`). Check it and save: every
personal link and the public link are built from it.

## 6. Things to know about the free tiers

- **The app sleeps.** Community Cloud puts an app to sleep after a period without visitors.
  The next visitor sees a "this app has gone to sleep" page with a button to wake it, and
  waits about a minute. Tell the group this is normal. Opening the app yourself shortly
  before you send the links avoids most of it.
- **The database sleeps too.** Neon's free tier suspends the database after a few idle
  minutes; the first query afterwards takes a few seconds. The app checks each connection
  before using it, so this only shows as a short delay.
- **Solving takes longer than on your laptop.** The free host's CPU is slower; count on up
  to a minute per seed when you click "Run the market", and stay on the page until it finishes.
- **No accounts.** Anyone who has the app's address can create a cycle, and anyone who has
  a cycle's organiser link can manage it. This was accepted for the MVP: the address is not
  advertised, the links are unguessable, and nothing sensitive beyond names and availability
  is stored. Do not post the organiser link anywhere; if a participant's link leaks, use
  "New link" on the Status tab.
- **No emails are stored.** The organiser sends the links by hand.
- **Resource limits.** The free tier gives the app about 1 GB of memory; this app needs far
  less. Neon's free tier gives 0.5 GB of storage; a cycle uses a few hundred kilobytes.

## 7. Back up after each cycle

The free database is not backed up for you. After publishing, download the four CSVs from
the **Publish** tab (`schedule.csv`, `summary.csv`, `preferences.csv`, `edits.csv`) and keep
them. `preferences.csv` is in the command-line tool's format, so the cycle can always be
re-solved with `python oncall_market.py solve preferences.csv`.

## 8. Updating the app

Push to `main`; Community Cloud redeploys automatically within a minute or two. Run the
tests before pushing. If the database schema ever changes (new columns), the app does not
migrate existing tables: either add the column in the Neon SQL editor or create a fresh
database and point `DATABASE_URL` at it after downloading the CSVs.

## 9. Running locally against Neon (optional)

To work on the real data from your machine, create `.streamlit/secrets.toml` (gitignored)
with the same `DATABASE_URL = "..."` line, or set the `DATABASE_URL` environment variable.
Resolution order: the Streamlit secret, then the environment variable, then the local
SQLite file.

## Troubleshooting

- "This organiser link is not valid": the token is wrong or the cycle was deleted; check
  the saved link for missing characters.
- An error mentioning `psycopg` or "could not connect": the secret is missing or wrong;
  re-copy the connection string from Neon and make sure it is inside double quotes.
- "Automatic packages are not available yet": the number of responders is not 7 or the
  period is not 9 weekdays and the automatic package module is not installed yet. Paste a
  packages JSON on the Run market tab (see README.md, "Changing the system").
- The app shows an old version after a push: open the app's menu (bottom right) and choose
  "Reboot app".
