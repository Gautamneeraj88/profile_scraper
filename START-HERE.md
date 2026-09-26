# Start here

## The short way

**Double-click `SETUP.bat`.** It finds or installs Python, sets everything up,
builds the app, and puts a shortcut on your Desktop. 10–20 minutes, mostly
downloading. Then double-click `DEMO.bat` to try it with example data and no
LinkedIn account.

If anything looks wrong, `CHECK.bat` reports what is and is not set up.

Everything below explains the same thing step by step, and what to do when a
step misbehaves.

---

A walkthrough, in order. Steps 1–4 do not touch LinkedIn at all, so you can get
the whole thing working before you risk anything with your account.

Set aside about 20 minutes for the first time. Most of it is waiting for
downloads.

---

## 1. Install Python

Download it from <https://www.python.org/downloads/> and run the installer.

**On the very first installer screen, tick "Add python.exe to PATH".**
It is a small checkbox near the bottom and it is easy to miss. Nothing else in
this guide works without it.

To check it worked, open **Command Prompt** (press the Windows key, type `cmd`,
press Enter) and type:

```
python --version
```

You want to see `Python 3.10` or higher. Two other answers are possible:

**"Python was not found; run without arguments to install from the Microsoft
Store"** — Windows is intercepting the command with a placeholder that does
nothing. This happens on most new PCs and is not your fault. Fix it:

> **Settings › Apps › Advanced app settings › App execution aliases**
> — turn **off** both `python.exe` and `python3.exe`.

Then close Command Prompt, open a new one, and check `python --version` again.
Try `py --version` too; if that one answers properly, everything here will work
regardless of the aliases.

**"python is not recognized"** — the PATH checkbox was missed. Run the
installer again and choose *Modify*, then *Add python.exe to PATH*.

---

## 2. Build the app

Unzip this folder somewhere ordinary — your Desktop or Documents is fine. Avoid
Program Files.

Open the folder and **double-click `build.bat`**.

It will:

1. set up a private Python environment, so nothing else on your PC is touched
2. install the libraries it needs
3. download the browser it drives (about 150 MB, once only)
4. run its own self-checks
5. build the application

When it finishes you will have:

```
dist\LinkedInEnricher\LinkedInEnricher.exe
```

**If the self-checks fail it stops deliberately and does not build.** That is the
tool protecting you from a broken copy, not a problem with your PC. Send the
text in the window to whoever gave you this.

> Don't want to build an exe? Double-click **`run_windows.bat`** instead. Same
> app, run straight from the source. Use this if antivirus interferes.

---

## 3. Check it landed properly on your PC

In Command Prompt, in that same folder:

```
dist\LinkedInEnricher\LinkedInEnricher.exe --doctor
```

This prints what the app found on your machine and writes the same thing to a
file. **Please send that file back** — it is the only way to confirm the parts
that could not be tested before you got it:

```
%APPDATA%\GradNext\LinkedInEnricher\diagnostics.txt
```

(Paste that path into File Explorer's address bar to find it.)

Two lines in it matter most:

- `Frozen build: True`
- `Password store:` — should name a Windows backend, not "none"

If the password store says "none", saved passwords will silently not stick, and
you will have to type your password each run. Worth knowing before you rely on
it.

---

## 4. Try the whole thing with no LinkedIn

```
dist\LinkedInEnricher\LinkedInEnricher.exe --demo
```

This opens the real interface and replays 37 saved profiles. Nothing connects to
LinkedIn; no account needed. Click around freely — you cannot break anything.

Have a go at all three tabs:

- **Settings** — change a column heading, add a column, remove one
- **Run** — press Start, then Pause, then Resume, then Stop
- **Results** — sort a column, edit a cell by double-clicking, press Export

This is the best way to learn the tool. Do it before step 5.

---

## 5. Your first real run — three people only

Start the app normally (double-click the `.exe`, or `run_windows.bat`).

### Settings tab, working down the five numbered cards

**1. Where your candidates are**
Pick *A Google Sheet link* and paste the link, or *An Excel or CSV file on this
computer* and choose the file. Press **Check I can read it** — it will tell you
how many candidates it found.

If your sheet already has the column headings you want, press **Use the column
names from this file** and it will match them up for you.

**2. Your LinkedIn account**
Your own email and password. Not anyone else's — see the warning at the bottom
of this page.

Tick *Remember my password on this computer* if you want; it goes into Windows
Credential Manager, not into any file here. **Forget my password** removes it.

**3. What to collect, and what to call each column**
This is the heart of the tool. Each row is one column in your output:

- the tick decides whether it is included
- the middle box is the heading, and you can type anything you like in it
- the right-hand box chooses which LinkedIn detail fills it

**Standard set** gives you the nine usual GradNext columns. **Add a whole
group...** adds everything in a category at once.

Watch the line at the bottom of this card. It says how many LinkedIn pages each
candidate will cost. Asking for a name and a job title costs 1 page; asking for
everything costs 14. Fewer pages is faster and much safer for your account, so
only tick what you will actually use.

Anything marked as not yet verified is a field that has never been confirmed
against a live profile. Leave those alone at first.

**4. How carefully to work**
For this first run:

- **Only do the first** → `3`
- Tick **Show the browser while it works**

Leave the waiting times alone. The defaults are deliberately unhurried because
racing through profiles is what gets accounts restricted.

**5. Where to save the results**
Choose a folder and a file name. **Your original sheet or file is never
modified** — new files are always written alongside.

### Then: the Run tab

Press **Start**.

A browser window will open and the app will type your details in. **LinkedIn will
very likely ask you to prove it is you** — a code by email, or a puzzle. That is
normal for a new sign-in. Complete it yourself in that window, taking as long as
you need; the app waits. This is why step 5 says to show the browser.

After that, watch it work. You will see each candidate as it goes, a progress
bar, and an estimate of the time left. **Pause** and **Stop** both take effect
immediately, and anything already collected is kept.

### Finally: the Results tab

Every candidate, in a table. Amber rows want checking by a human — usually
someone matched by name rather than by a LinkedIn link, where the tool is not
certain it found the right person. Check those against the `Match Confidence`
and `Needs Review` columns.

You can correct any cell by double-clicking it. Your corrections are what gets
exported, not the original scraped value.

Press **Export...** to write the file.

---

## 6. Then the full run

Once the three test rows look right, set **Only do the first** back to
`all of them`, and untick *Show the browser* if you would rather it worked
quietly in the background.

At roughly 10 seconds a candidate, 200 candidates is about half an hour. Leave
it running.

If it stops early saying LinkedIn paused the session: that is the tool backing
off on purpose. Everything collected is saved. Wait a few hours and start it
again — it picks up where it left off and will not re-fetch what it already has.

---

## Two things worth being clear about

**Use your own LinkedIn account, never a shared one.** The tool signs in as
whoever's details are entered, and automated collection like this is against
LinkedIn's user agreement — an account doing it can be restricted. That risk is
yours to accept knowingly, and it is the reason the waiting times are set high
and the whole thing runs one profile at a time.

**Email and phone numbers are usually not there.** LinkedIn hides them unless
someone has chosen to make them public, which most people have not. Blank cells
in those columns are the normal outcome, not a fault.

---

## When something goes wrong

`INSTALL-WINDOWS.md` in this folder covers the common Windows problems:
SmartScreen warnings, antivirus, company firewalls blocking the install,
OneDrive locking files.

For anything else, run this and send the file it names:

```
dist\LinkedInEnricher\LinkedInEnricher.exe --doctor
```

Inside the app, **Help → Check this computer** shows the same information.

---

## Doing it by hand, if `build.bat` will not cooperate

The batch files are a convenience. If one misbehaves, these five commands do
exactly the same thing. Run them in Command Prompt, in this folder, one at a
time.

Use `py` if it works on your PC; otherwise use `python`.

```
py -m venv .venv
.venv\Scripts\python.exe -m pip install PySide6 openpyxl playwright keyring pyinstaller
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe tests_engine.py
.venv\Scripts\python.exe li_app.py --demo
```

If the fourth line ends in `passed`, the tool works on your PC. The fifth opens
the interface with saved data and no LinkedIn.

You never have to build the `.exe` at all — `.venv\Scripts\python.exe li_app.py`
runs the same application. The `.exe` only exists so it can be copied to a PC
with no Python on it.
