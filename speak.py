#!/usr/bin/env python3
"""Speak - Voice to Text for macOS using MLX Whisper."""

import sys
import time
import threading
from typing import Optional

import numpy as np
import sounddevice as sd
from pynput.keyboard import Controller

# Quartz for event tap
import Quartz
from Foundation import NSObject, NSRunLoop, NSDefaultRunLoopMode
from PyObjCTools import AppHelper

# =============================================================================
# Configuration Constants
# =============================================================================

# Model
MODEL_NAME = "mlx-community/whisper-large-v3-turbo"

# Audio settings
SAMPLE_RATE = 16000  # Whisper expects 16kHz
CHANNELS = 1         # Mono audio

# Hotkeys (with Option/Alt modifier) - using macOS keycodes (US-QWERTY)
KEYCODE_R = 15  # Option+R - push-to-talk (hold to record)
KEYCODE_T = 17  # Option+T - toggle (press to start/stop)

# Audio feedback
BEEP_VOLUME = 0.3       # 0.0 to 1.0, set to 0 to disable beeps
BEEP_DURATION = 0.1     # 100ms beeps

# Transcription
MIN_RECORDING_DURATION = 0.3  # Ignore recordings shorter than 300ms
LANGUAGE = "en"               # English only, hardcoded

# Global reference to mlx_whisper module (lazy loaded)
mlx_whisper = None

# Global daemon reference for hotkey callbacks
_daemon = None


# =============================================================================
# Audio Feedback System
# =============================================================================

def generate_beep(frequency: float, duration: float, volume: float) -> np.ndarray:
    """Generate a sine wave beep."""
    t = np.linspace(0, duration, int(SAMPLE_RATE * duration), dtype=np.float32)
    # Apply fade in/out to avoid clicks (10ms fade)
    fade_samples = int(SAMPLE_RATE * 0.01)
    wave = np.sin(2 * np.pi * frequency * t) * volume
    # Fade in
    wave[:fade_samples] *= np.linspace(0, 1, fade_samples)
    # Fade out
    wave[-fade_samples:] *= np.linspace(1, 0, fade_samples)
    return wave


def play_beep(beep_type: str):
    """Play a feedback beep. Types: 'start', 'stop', 'done'"""
    if BEEP_VOLUME <= 0:
        return

    if beep_type == 'start':
        beep = generate_beep(880, BEEP_DURATION, BEEP_VOLUME)
    elif beep_type == 'stop':
        beep = generate_beep(440, BEEP_DURATION, BEEP_VOLUME)
    elif beep_type == 'done':
        # Double beep
        beep1 = generate_beep(660, BEEP_DURATION, BEEP_VOLUME)
        silence = np.zeros(int(SAMPLE_RATE * 0.05), dtype=np.float32)
        beep2 = generate_beep(660, BEEP_DURATION, BEEP_VOLUME)
        beep = np.concatenate([beep1, silence, beep2])
    else:
        return

    # Play asynchronously to not block
    sd.play(beep, SAMPLE_RATE)


# =============================================================================
# Quartz Event Tap for Global Hotkeys
# =============================================================================

_event_tap = None
_run_loop_source = None


def _event_tap_callback(proxy, event_type, event, refcon):
    """Callback for Quartz event tap - intercepts keyboard events."""
    global _daemon

    if _daemon is None:
        return event

    # Handle tap disabled event
    if event_type == Quartz.kCGEventTapDisabledByTimeout:
        # Re-enable the tap
        if _event_tap:
            Quartz.CGEventTapEnable(_event_tap, True)
        return event

    if event_type == Quartz.kCGEventTapDisabledByUserInput:
        return event

    # Only process key events
    if event_type not in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
        return event

    # Get event info
    keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
    flags = Quartz.CGEventGetFlags(event)

    # Check if Option is held
    option_down = (flags & Quartz.kCGEventFlagMaskAlternate) != 0

    if not option_down:
        return event

    # Check for key repeat
    is_repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)

    # Option+R: Push-to-talk
    if keycode == KEYCODE_R:
        if event_type == Quartz.kCGEventKeyDown and not is_repeat:
            threading.Thread(target=_daemon._handle_push_to_talk_down, daemon=True).start()
        elif event_type == Quartz.kCGEventKeyUp:
            threading.Thread(target=_daemon._handle_push_to_talk_up, daemon=True).start()
        return None  # Suppress the event

    # Option+T: Toggle
    if keycode == KEYCODE_T:
        if event_type == Quartz.kCGEventKeyDown and not is_repeat:
            threading.Thread(target=_daemon._handle_toggle, daemon=True).start()
        return None  # Suppress the event

    return event


def start_event_tap():
    """Start the Quartz event tap for global hotkeys."""
    global _event_tap, _run_loop_source

    # Create event tap
    # kCGHIDEventTap: Receive events at the lowest level
    # kCGHeadInsertEventTap: Insert at the head of the event tap list
    _event_tap = Quartz.CGEventTapCreate(
        Quartz.kCGHIDEventTap,                    # Tap location
        Quartz.kCGHeadInsertEventTap,             # Placement
        Quartz.kCGEventTapOptionDefault,          # Options (can modify events)
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp),
        _event_tap_callback,
        None
    )

    if _event_tap is None:
        print("ERROR: Failed to create event tap!")
        print("Make sure Accessibility permissions are granted:")
        print("  System Settings → Privacy & Security → Accessibility")
        return False

    # Create run loop source
    _run_loop_source = Quartz.CFMachPortCreateRunLoopSource(None, _event_tap, 0)

    # Add to current run loop
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(),
        _run_loop_source,
        Quartz.kCFRunLoopCommonModes
    )

    # Enable the tap
    Quartz.CGEventTapEnable(_event_tap, True)

    print("  ✓ Event tap created successfully")
    return True


def stop_event_tap():
    """Stop and clean up the event tap."""
    global _event_tap, _run_loop_source

    if _event_tap:
        Quartz.CGEventTapEnable(_event_tap, False)
        _event_tap = None

    if _run_loop_source:
        Quartz.CFRunLoopRemoveSource(
            Quartz.CFRunLoopGetCurrent(),
            _run_loop_source,
            Quartz.kCFRunLoopCommonModes
        )
        _run_loop_source = None


# =============================================================================
# Main Daemon Class
# =============================================================================

class SpeakDaemon:
    """Voice-to-text daemon with global hotkey support."""

    def __init__(self):
        global _daemon
        _daemon = self

        # Model state
        self.model_loaded = threading.Event()

        # Recording state
        self.is_recording = False
        self.audio_buffer = []
        self.audio_lock = threading.Lock()
        self.stream = None

        # Keyboard state
        self.toggle_mode_active = False
        self.push_to_talk_held = False
        self.keyboard_controller = Controller()

        # Start loading model in background thread
        threading.Thread(target=self._load_model, daemon=True).start()

    def _load_model(self):
        """Load MLX Whisper model and warm it up."""
        global mlx_whisper
        print("Loading MLX Whisper model...")
        import mlx_whisper as mw
        mlx_whisper = mw

        # Warm up with dummy audio (first inference is always slower)
        dummy = np.zeros(SAMPLE_RATE, dtype=np.float32)
        mlx_whisper.transcribe(dummy, path_or_hf_repo=MODEL_NAME)

        print(f"Model ready: {MODEL_NAME}")
        self.model_loaded.set()

    def _audio_callback(self, indata, frames, time_info, status):
        """Called by sounddevice for each audio chunk."""
        if status:
            print(f"Audio status: {status}", file=sys.stderr)
        if self.is_recording:
            with self.audio_lock:
                self.audio_buffer.append(indata.copy())

    def start_recording(self):
        """Begin audio capture."""
        if self.is_recording:
            return

        with self.audio_lock:
            self.audio_buffer = []

        self.is_recording = True
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=np.float32,
            callback=self._audio_callback,
        )
        self.stream.start()
        play_beep('start')
        print("🎤 Recording...")

    def stop_recording(self) -> Optional[np.ndarray]:
        """Stop capture and return audio array."""
        if not self.is_recording:
            return None

        self.is_recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        play_beep('stop')

        with self.audio_lock:
            if not self.audio_buffer:
                return None
            audio = np.concatenate(self.audio_buffer, axis=0).flatten()
            self.audio_buffer = []

        print("⏹️  Stopped")
        return audio

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe audio to text using MLX Whisper."""
        if not self.model_loaded.is_set():
            print("Waiting for model...")
            self.model_loaded.wait()

        print("🔄 Transcribing...")
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=MODEL_NAME,
            language=LANGUAGE,
            fp16=True,  # Use FP16 for faster inference on Apple Silicon
        )
        text = result.get("text", "").strip()
        print(f"✅ Result: {text}")
        return text

    def type_text(self, text: str):
        """Type text into the currently focused application."""
        if not text:
            return
        play_beep('done')
        time.sleep(0.05)  # Small delay to ensure app has focus
        self.keyboard_controller.type(text)

    def process_recording(self):
        """Stop recording, transcribe, and type result."""
        audio = self.stop_recording()

        # Ignore very short recordings (accidental presses)
        if audio is None or len(audio) < SAMPLE_RATE * MIN_RECORDING_DURATION:
            print("Recording too short, ignoring")
            return

        # Run transcription in background thread to keep hotkeys responsive
        def _transcribe_and_type():
            text = self.transcribe(audio)
            if text:
                self.type_text(text)

        threading.Thread(target=_transcribe_and_type, daemon=True).start()

    def _handle_push_to_talk_down(self):
        """Handle Option+R key down."""
        if not self.toggle_mode_active and not self.push_to_talk_held:
            self.push_to_talk_held = True
            self.start_recording()

    def _handle_push_to_talk_up(self):
        """Handle Option+R key up."""
        if self.push_to_talk_held:
            self.push_to_talk_held = False
            if self.is_recording and not self.toggle_mode_active:
                self.process_recording()

    def _handle_toggle(self):
        """Handle Option+T key down."""
        if self.toggle_mode_active:
            self.toggle_mode_active = False
            self.process_recording()
        else:
            self.toggle_mode_active = True
            self.start_recording()

    def run(self):
        """Main entry point."""
        print("=" * 50)
        print("🎙️  Speak - Voice to Text for macOS")
        print("=" * 50)
        print()
        print("Hotkeys:")
        print("  Option+R  - Push-to-talk (hold to record)")
        print("  Option+T  - Toggle (press to start/stop)")
        print()
        print("Press Ctrl+C to quit")
        print()

        # Start event tap for hotkeys
        print("Setting up hotkeys...")
        if not start_event_tap():
            print("Failed to set up hotkeys. Check Accessibility permissions.")
            return
        print()

        # Wait for model before accepting input
        if not self.model_loaded.is_set():
            print("Loading model (first run downloads ~800MB)...")
            self.model_loaded.wait()

        print("✅ Ready! Start speaking...")
        print()

        try:
            # Run the CFRunLoop to process events
            AppHelper.runConsoleEventLoop()
        except KeyboardInterrupt:
            pass
        finally:
            stop_event_tap()
            print("\nGoodbye!")


def main():
    """Entry point with platform check."""
    if sys.platform != "darwin":
        print("Error: This tool only works on macOS")
        sys.exit(1)

    daemon = SpeakDaemon()
    daemon.run()


if __name__ == "__main__":
    main()
