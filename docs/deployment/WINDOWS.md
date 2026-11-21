# Windows deployment

> **Status:** these steps are written from the Python and Node requirements and the configuration reference. They were
> **not run on a Windows host** in the release environment. The paths and PowerShell commands follow the
> documented behaviour of the server. Verify them on your machine before relying on them.

## Requirements

- Python 3.13 (from python.org, or `winget install Python.Python.3.13`).
- Node.js 20 or newer, only to build the console.
- PowerShell 5.1 or PowerShell 7.

## Install

Run these in PowerShell from the folder that contains `backend` and `console`:

```powershell
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If activation is blocked, allow scripts for the current user only:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Choose a data directory outside the source tree:

```powershell
$env:JARVISMIND_DATA_DIR = "$env:LOCALAPPDATA\JarvisMind\data"
python -m jarvismind init-db
```

`init-db` creates the database once. It refuses to run again on the same directory.

## Start

```powershell
$env:JARVISMIND_DATA_DIR = "$env:LOCALAPPDATA\JarvisMind\data"
python -m jarvismind serve
```

To build and serve the console:

```powershell
cd ..\console
npm ci
npm run build
cd ..\backend
$env:JARVISMIND_CONSOLE_DIR = (Resolve-Path ..\console\dist).Path
python -m jarvismind serve
```

Open `http://127.0.0.1:8777/console/`.

## Owner token

The token is in `$env:LOCALAPPDATA\JarvisMind\data\.jarvismind_token`:

```powershell
Get-Content "$env:LOCALAPPDATA\JarvisMind\data\.jarvismind_token"
```

Windows does not apply POSIX mode bits. The file is still stored in the data directory of your user profile, so
only your account and administrators can read it by default. Do not place the data directory in a shared folder.

## Start at sign-in (optional)

Create a scheduled task that runs `python -m jarvismind serve` from the backend folder at sign-in, with the same
environment variables. A service wrapper such as NSSM also works. Keep the server bound to `127.0.0.1`.

## Verify

```powershell
Invoke-RestMethod http://127.0.0.1:8777/health
```

## Known differences

- File permissions are not enforced the way they are on Linux and macOS. Restrict the data directory with NTFS
  permissions if other users share the machine.
- Paths in `JARVISMIND_DATA_DIR` are resolved against the current directory. Use absolute paths in scheduled tasks.
