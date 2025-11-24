# SmartPhysio Demo Assistant

Quick setup and run instructions for the project.

Prerequisites
- Python 3.10+ installed on Windows

Create & activate virtual environment (PowerShell)
```
Set-Location 'C:\Users\1999j\4'
python -m venv .venv
.\.venv\Scripts\Activate
```

Install requirements (already done if you used the provided `.venv`):
```
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Run the script
```
# (optional) point the vibration client to your ESP32 IP or hostname
$env:VIBRATION_HOST = 'http://192.168.4.10'
python .\5PhysioAudio.py
```

Notes
- A frozen copy of installed packages is saved as `requirements-lock.txt`.
- If you have issues with mDNS (`esp32-haptic.local`), use the ESP32 IP address in `VIBRATION_HOST`.
- Keep `.venv` activated while running the script so the installed packages are used.
