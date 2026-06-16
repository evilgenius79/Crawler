# Running PersonalSearch on Windows

Helper scripts for hosting the crawler on a Windows machine (e.g. a Windows VM
on unraid). No Docker required — it's just Python.

## One-time setup

1. Install **Python 3.11+** from <https://www.python.org/downloads/> and tick
   **"Add python.exe to PATH"** during install.
2. Put this project somewhere permanent, e.g. `C:\crawler`.
3. Double-click **`setup.bat`** — it creates a virtual environment and installs
   everything.

## Everyday use

| Script | What it does |
|--------|--------------|
| `start-search.bat` | Launches the search UI at <http://localhost:8000>. Keep the window open. |
| `crawl.bat` | Crawls site(s), following links outward. Double-click it and it asks for the URL(s), or pass them: `crawl.bat https://example.com`. |
| `recrawl.bat` | Re-fetches anything older than a day (used by the scheduled task). |

Examples (from a Command Prompt in this folder, or just double-click):

```bat
crawl.bat https://example.com https://news.ycombinator.com
start-search.bat
```

To store the index on another drive, uncomment and edit the `CRAWLER_DATA_DIR`
line near the top of each `.bat` file (set it to the same path in all of them).

## Reaching the UI from other PCs

The server listens on all interfaces, so browse to `http://<VM-IP>:8000` from
another machine. Allow the port through the firewall once (PowerShell as Admin):

```powershell
New-NetFirewallRule -DisplayName "PersonalSearch" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
```

## Run automatically (Task Scheduler)

Two ready-made tasks are included. **Edit the paths inside each XML first**
(they contain `C:\path\to\crawler` placeholders), then in **Task Scheduler →
Action → Import Task…** select the file.

- **`PersonalSearch-WebUI.xml`** — starts the search UI at logon and keeps it
  running (restarts on failure).
- **`PersonalSearch-Recrawl.xml`** — runs `recrawl.bat` daily at 3:00 AM.

> The WebUI task uses an interactive logon trigger, which is simplest when the VM
> auto-logs-in. For a truly headless service (runs with no one logged in), set the
> task to *"Run whether user is logged on or not"* (it will ask for the account
> password), or install it as a Windows service with a tool like
> [NSSM](https://nssm.cc/).

## Optional: JavaScript rendering

```bat
.venv\Scripts\activate
pip install playwright
playwright install chromium
```

Then add `--render-js` to a crawl, e.g. `crawl.bat https://some-spa.com` after
editing the flag into the script.
