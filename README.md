# Speak - Voice to Text for macOS

Dictate text anywhere on your Mac using local AI (no cloud, no API keys).

## Requirements

- macOS with Apple Silicon (M1/M2/M3/M4)
- Python 3.10+

## Installation

```bash
# Clone or cd into the speak directory
cd speak

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
# Activate venv if not already
source venv/bin/activate

# Run the daemon
python speak.py
```

First run will download the Whisper model (~800MB).

### Hotkeys

| Hotkey | Mode | Description |
|--------|------|-------------|
| Option+R | Push-to-talk | Hold while speaking, release to transcribe |
| Option+T | Toggle | Press to start recording, press again to stop |

### Permissions

macOS will prompt for:
1. **Microphone access** - Required for recording
2. **Accessibility access** - Required for global hotkeys and typing

Go to System Settings → Privacy & Security to grant these.

## Configuration

Edit the top of `speak.py` to customize:

- `BEEP_VOLUME` - Feedback sound volume (0.0 to 1.0, or 0 to disable)
- `PUSH_TO_TALK_KEY` - Change from 'r' to another key
- `TOGGLE_KEY` - Change from 't' to another key

## Troubleshooting

**Hotkeys not working?**
- Grant Accessibility permission to Terminal (or your terminal app)
- System Settings → Privacy & Security → Accessibility

**No audio recording?**
- Grant Microphone permission to Terminal
- System Settings → Privacy & Security → Microphone
