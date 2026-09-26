# Building and installing on Windows

Two ways to get the app running. Pick one.

---

## Option A — just run it (easiest)

1. Install [Python for Windows](https://www.python.org/downloads/).
   **On the first installer screen, tick "Add python.exe to PATH".** This is the
   step people miss, and nothing works without it.
2. Double-click **`run_windows.bat`**.

The first run takes a few minutes: it sets up a private Python environment and
downloads the browser the app drives (about 150 MB, once). After that it starts in
a couple of seconds.

---

## Option B — build a proper application

This produces `LinkedInEnricher.exe`, which you can copy to other PCs that have no
Python at all.

1. Install Python as above.
2. Double-click **`build.bat`**.
3. Wait. It installs what it needs, runs the self-checks, and builds the app.
4. You end up with:

   ```
   dist\LinkedInEnricher\LinkedInEnricher.exe
   ```

To give it to a colleague, zip the whole `dist\LinkedInEnricher` folder and send
that. They unzip it and double-click the .exe — nothing else to install.

`build.bat` deliberately stops if the self-checks fail, so a broken build never
gets shipped.

---

## What gets installed where

Nothing goes into Program Files and nothing touches the registry. The app keeps
everything under your own user folder:

```
%APPDATA%\GradNext\LinkedInEnricher\
    config.json          your settings
    chrome-profile\      your LinkedIn sign-in
    cache\               profiles collected earlier
    logs\                one log file per run
```

Your LinkedIn password is not in any of these. It goes into **Windows Credential
Manager**, under `GradNext LinkedInEnricher`. You can see and remove it there, or
use *Forget my password* in the app.

To uninstall: delete the application folder, delete the `%APPDATA%\GradNext`
folder, and remove the password from Credential Manager.

---

## Known Windows wrinkles

**"Python was not found; run without arguments to install from the Microsoft
Store".** Windows ships placeholder `python.exe` and `python3.exe` files that do
nothing but print that message. They exist even when Python is properly
installed, which is why checking whether the file exists proves nothing.

Turn them off in **Settings › Apps › Advanced app settings › App execution
aliases** — switch off `python.exe` and `python3.exe` — then open a fresh
Command Prompt.

`build.bat` and `run_windows.bat` work around this by preferring the `py`
launcher, which the placeholders do not affect, so usually you will not meet
this at all.

**SmartScreen says "Windows protected your PC".** Expected for any unsigned
application. Click *More info* → *Run anyway*. Signing the exe with a code-signing
certificate is the only way to stop this, and certificates cost money.

**Antivirus quarantines the exe.** PyInstaller applications are sometimes
misidentified. If this happens, add the folder to your antivirus exclusions, or use
Option A instead, which is plain Python and never triggers it.

**The build fails at "Installing the libraries".** Almost always a company
firewall or proxy blocking pip. Try from a home connection, or ask IT for the
proxy settings.

**Your Documents folder is on OneDrive.** That works, but OneDrive can lock files
while syncing. If exporting fails with a permissions error, either close Excel or
choose a local folder under *Where to save the results*.

**The .exe will not start and shows nothing.** Run it from a Command Prompt to see
the error, or use `run_windows.bat`, which keeps the window open on a failure.

---

## Checking it works

Before a real run, try either of these — neither touches LinkedIn:

```
.venv\Scripts\python.exe tests_engine.py       (offline checks against 37 saved profiles)
.venv\Scripts\python.exe li_app.py --demo      (the whole interface, replaying saved data)
```

You can also ask the built application to report on itself, which works even when
the interface will not open:

```
dist\LinkedInEnricher\LinkedInEnricher.exe --doctor
```

That writes `%APPDATA%\GradNext\LinkedInEnricher\diagnostics.txt`. Inside the app,
**Help → Check this computer** shows the same thing. Copy it if you need to ask
for help — it is the quickest way to diagnose a problem on a PC nobody else can
see.
