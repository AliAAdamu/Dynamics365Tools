# D365 DMF Tester

A **Postman-style web application** for load-testing Dynamics 365 Finance & Operations **Data Management Framework (DMF)** import and export operations.

Run up to 50 iterations in **parallel or serial** with live progress, per-execution timing, and persistent result history.

---

## Features

| Feature | Detail |
|---|---|
| **Multi-environment** | Store N environments, each with its own OAuth credentials and D365 URL |
| **Secure secrets** | Client secrets encrypted at rest with [Fernet](https://cryptography.io/en/latest/fernet/) symmetric encryption |
| **Import testing** | Upload a .zip package → get Azure Blob write URL → upload → trigger import → poll until complete |
| **Export testing** | Trigger export → poll → record execution ID |
| **Download exported package** | After a successful export run, click **Download** on any succeeded execution to fetch the SAS URL via `GetExportedPackageUrl` and stream the file directly |
| **Duplicate test plan** | Clone any existing plan with one click — useful for creating variations without re-entering all fields |
| **Load mode** | Parallel (ThreadPoolExecutor, up to 20 workers) or Serial |
| **Live progress** | Real-time execution table updated every 1.5 s while the run is active |
| **Results** | Persisted to JSON with timing stats; Chart.js bar + doughnut charts in detail view |
| **Definition Group browser** | Loads available DMF groups from D365 so you don't have to type them |

---

## Prerequisites

- Python 3.9 or later
- Network access to your D365 environment
- An **Azure App Registration** with:
  - Client credentials (client ID + secret)
  - Permission granted in D365 via *System administration → Azure Active Directory applications*

---

## Quick Start

```powershell
# 1. Install dependencies
.\install.ps1

# 2. Start the app
.\run.ps1
```

Open **http://localhost:5000** in your browser.

> To use a different port: `$env:PORT = 5100; .\run.ps1`

---

## Usage

### 1 — Add an Environment

Go to **Environments → Add Environment** and fill in:

| Field | Example |
|---|---|
| Name | `UAT` |
| Base URL | `https://contoso.sandbox.operations.dynamics.com` |
| Tenant ID | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| Client ID | `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` |
| Client Secret | `your-secret-value` |

Click **Test** to verify connectivity — it will count the available DMF definition groups.

### 2 — Create a Test Plan

Go to **Test Plans → New Plan**.

From the plans list you can also:
- **Edit** an existing plan (pencil icon)
- **Duplicate** a plan (copy icon) — creates a new plan prefixed with *"Copy of …"* with all settings pre-filled
- **Delete** a plan (trash icon)

**Import plan:**
- Set Operation = Import
- Upload a `.zip` DMF data package
- Enter the Definition Group ID (or click Browse to load from D365)
- Enter the Legal Entity (e.g. `USMF`)
- Configure poll interval / timeout

**Export plan:**
- Set Operation = Export  
- Enter the Definition Group ID and a Package Name
- Enable Re-execute to force a fresh export each iteration

### 3 — Run Tests

Go to **Run Tests**:

1. Select an **Environment** and **Test Plan**
2. Choose **Serial** (one after another) or **Parallel** (all at once)
3. Set the number of **iterations** (1–50)
4. Click **Start Run**

The execution table updates live every 1.5 seconds showing status, job status, and duration.

### 4 — View Results

Go to **Results** for a list of all runs, or click any run to see:
- Summary statistics (total, succeeded, failed, avg/min/max duration)
- Duration bar chart per iteration
- Success vs failed doughnut chart
- Full per-execution detail table

### 5 — Download an Exported Package

For **export** runs, each succeeded execution row in the detail view shows a **Download** button. Clicking it calls `GetExportedPackageUrl` against D365 and redirects your browser to the time-limited Azure Blob SAS URL so the package downloads immediately.

> Note: The SAS URL is generated fresh on each click and expires after a short period (typically 1 hour).

---

## DMF API Flow

### Import
```
POST /data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.GetAzureWriteUrl
PUT  {blobWriteUrl}                          ← upload package bytes
POST /data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.ImportFromPackage
POST /data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.GetExecutionSummaryStatus  ← poll
```

### Export
```
POST /data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.ExportToPackage
POST /data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.GetExecutionSummaryStatus  ← poll
POST /data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.GetExportedPackageUrl     ← on demand (Download button)
```

---

## File Structure

```
D365-DMF-Tester/
├── app.py                   Entry point (Flask app factory + Blueprint registration)
├── auth.py                  OAuth2 client-credentials (with in-memory token cache)
├── config_manager.py        JSON storage + Fernet encryption for secrets
├── dmf_client.py            D365 DMF REST API client
├── routes.py                All Flask route handlers (Blueprint)
├── runner.py                Parallel/serial test runner (ThreadPoolExecutor)
├── static/css/style.css     Custom Bootstrap 5 styles
├── templates/               Jinja2 HTML templates
│   ├── base.html
│   ├── index.html
│   ├── environments.html
│   ├── environment_form.html
│   ├── plans.html
│   ├── plan_form.html
│   ├── runner.html
│   ├── results.html
│   └── result_detail.html
├── data/                    Environments, plans, results (gitignored)
├── uploads/                 Uploaded import packages (gitignored)
├── requirements.txt
├── install.ps1
└── run.ps1
```

---

## Security Notes

- Client secrets are encrypted with **Fernet** before being written to `data/environments.json`.  
  The encryption key is stored in `data/.encryption.key` (gitignored).
- OAuth tokens are cached **in memory only** and never written to disk.
- No uploaded files are executed — they are read as bytes and streamed to Azure Blob Storage.
- All secrets are passed as `POST` body fields, never in URLs or query strings.
- The app binds to `127.0.0.1` only — it is not intended for production deployment.

---

## Licence

MIT
