import os
import sys
import time
import json
import queue
import platform
import subprocess
import webbrowser
import re
import threading
import logging
import importlib
try:
    import sounddevice as sd
except ImportError:
    # Optional: text mode and Google speech fallback can work without it.
    sd = None
from vosk import Model, KaldiRecognizer
from datetime import datetime
from urllib.parse import quote_plus

# ============================================================
# OPTIONAL LIBRARIES & GRACEFUL IMPORTS
# ============================================================

try:
    sr = importlib.import_module("speech_recognition")
except ImportError:
    sr = None

try:
    import requests
except ImportError:
    requests = None

try:
    psutil = importlib.import_module("psutil")
except ImportError:
    psutil = None

try:
    pyautogui = importlib.import_module("pyautogui")
except ImportError:
    pyautogui = None

try:
    sbc = importlib.import_module("screen_brightness_control")
except ImportError:
    sbc = None

try:
    load_dotenv = importlib.import_module("dotenv").load_dotenv
except ImportError:
    load_dotenv = None

try:
    genai = importlib.import_module("google.genai")
    types = importlib.import_module("google.genai.types")
except ImportError:
    genai = None
    types = None

# Windows audio control
try:
    pycaw = importlib.import_module("pycaw.pycaw")
    AudioUtilities = pycaw.AudioUtilities
    IAudioEndpointVolume = pycaw.IAudioEndpointVolume
    CLSCTX_ALL = importlib.import_module("comtypes").CLSCTX_ALL
except ImportError:
    AudioUtilities = None
    IAudioEndpointVolume = None
    CLSCTX_ALL = None

try:
    custom_listen = importlib.import_module("core.voice").listen
except (ImportError, AttributeError):
    custom_listen = None

# ============================================================
# CONFIGURATION
# ============================================================

ASSISTANT_NAME = "AIVORA"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATING_SYSTEM = platform.system()

running = True
is_speaking = False

tts_process = None
tts_lock = threading.Lock()

if load_dotenv:
    load_dotenv(os.path.join(BASE_DIR, ".env"))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TEXT_MODE = os.getenv("TEXT_MODE", "false").lower() == "true"
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

current_session_mode = "LOCAL"

# Known Web Platforms & URLs
POPULAR_WEBSITES = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "linkedin": "https://www.linkedin.com",
    "instagram": "https://www.instagram.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "chatgpt web": "https://chat.openai.com",
    "spotify web": "https://open.spotify.com"
}

# Browser Aliases
BROWSER_ALIASES = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "safari": "Safari",
    "firefox": "Firefox",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "brave": "Brave Browser"
}

# Common Slang / Shorthand Aliases
APP_SHORTCUTS = {
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "calc": "Calculator",
    "word": "Microsoft Word",
    "excel": "Microsoft Excel",
    "ppt": "Microsoft PowerPoint",
    "powerpoint": "Microsoft PowerPoint",
    "settings": "System Settings",
    "system preferences": "System Settings",
    "preferences": "System Settings",
    "term": "Terminal",
    "whatsapp": "WhatsApp",
}

INSTALLED_APPS = {}

# ============================================================
# LOGGING
# ============================================================

LOG_FILE = os.path.join(BASE_DIR, "aivora.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("AIVORA")

# ============================================================
# AUTOMATIC APPLICATION DISCOVERY
# ============================================================

def scan_installed_applications():
    global INSTALLED_APPS
    apps = {}

    if OPERATING_SYSTEM == "Darwin":
        search_roots = [
            "/Applications",
            "/System/Applications",
            "/System/Applications/Utilities",
            os.path.expanduser("~/Applications")
        ]

        for sdir in search_roots:
            if not os.path.exists(sdir):
                continue

            try:
                for root, dirs, files in os.walk(sdir):
                    app_bundles = [d for d in dirs if d.endswith(".app")]
                    for app_dir in app_bundles:
                        app_name = app_dir[:-4]
                        clean_key = app_name.lower().strip()
                        apps[clean_key] = app_name

                    dirs[:] = [d for d in dirs if not d.endswith(".app")]
            except Exception as e:
                logger.error("Error scanning %s: %s", sdir, e)

    elif OPERATING_SYSTEM == "Windows":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-StartApps | Select-Object -ExpandProperty Name"],
                capture_output=True,
                text=True,
                timeout=6
            )
            for line in result.stdout.splitlines():
                name = line.strip()
                if name:
                    apps[name.lower()] = name
        except Exception as e:
            logger.error("Windows app indexing failed: %s", e)

    INSTALLED_APPS = apps
    logger.info("Indexed %d local system applications.", len(INSTALLED_APPS))
    return len(INSTALLED_APPS)


def resolve_installed_app(query):
    clean_query = query.strip().lower()

    if clean_query in APP_SHORTCUTS:
        target = APP_SHORTCUTS[clean_query]
        return INSTALLED_APPS.get(target.lower(), target)

    if clean_query in INSTALLED_APPS:
        return INSTALLED_APPS[clean_query]

    for key, app_title in INSTALLED_APPS.items():
        words = key.replace("-", " ").replace("_", " ").split()
        if clean_query in words:
            return app_title

    if len(clean_query) >= 3:
        for key, app_title in INSTALLED_APPS.items():
            if clean_query in key:
                return app_title

    return None

# ============================================================
# PERSISTENT CONVERSATION STORAGE & MEMORY
# ============================================================

MEMORY_FILE = os.path.join(BASE_DIR, "aivora_memory.json")
MAX_PERSISTENT_MESSAGES = 60  # Retains the last 30 full turns
MAX_OLLAMA_TURNS = 8

def load_persistent_history():
    """Loads saved conversation from disk across restarts."""
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    logger.info("Loaded %d past messages from %s", len(data), MEMORY_FILE)
                    return data
        except Exception as e:
            logger.error("Failed to read memory file: %s", e)
    return []


def save_persistent_history(history):
    """Saves conversation turns to disk."""
    try:
        trimmed = history[-MAX_PERSISTENT_MESSAGES:] if len(history) > MAX_PERSISTENT_MESSAGES else history
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to write to memory file: %s", e)


# Load history from disk on startup
unified_history = load_persistent_history()
ollama_history = [msg for msg in unified_history[-(MAX_OLLAMA_TURNS * 2):]]

gemini_client = None
gemini_chat = None

if genai and GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Gemini client initialized.")
    except Exception as e:
        logger.error("Gemini initialization failed: %s", e)
        gemini_client = None


def get_gemini_chat():
    """Initializes Gemini chat pre-seeded with saved conversation turns."""
    global gemini_chat
    if gemini_chat is None and gemini_client:
        try:
            formatted_history = []
            if types:
                for item in unified_history[-20:]:
                    role = "user" if item.get("role") == "user" else "model"
                    content_text = item.get("content", "")
                    try:
                        part = types.Part.from_text(text=content_text)
                    except Exception:
                        part = types.Part(text=content_text)

                    formatted_history.append(types.Content(role=role, parts=[part]))

            gemini_chat = gemini_client.chats.create(
                model=GEMINI_MODEL,
                history=formatted_history if formatted_history else None,
                config=types.GenerateContentConfig(
                    system_instruction=(
                        "You are AIVORA, a fast, intelligent desktop voice assistant. "
                        "Give short, direct, and conversational responses in 1 to 3 spoken sentences. "
                        "Never use markdown formatting, bullet points, asterisks, or bold tags. "
                        "Maintain memory of previous messages and answer follow-ups naturally."
                    )
                ) if types else None
            )
            logger.info("Gemini chat session loaded with %d past messages.", len(formatted_history))
        except Exception as e:
            logger.error("Failed to pre-seed Gemini chat session: %s", e)
            try:
                gemini_chat = gemini_client.chats.create(model=GEMINI_MODEL)
            except Exception:
                gemini_chat = None
    return gemini_chat


def reset_memory():
    """Wipes memory from RAM and the JSON file."""
    global gemini_chat, ollama_history, unified_history, current_session_mode
    gemini_chat = None
    ollama_history.clear()
    unified_history.clear()
    save_persistent_history([])
    current_session_mode = "LOCAL"
    logger.info("Conversation memory wiped from RAM and disk.")
    return "Memory cleared. Starting a fresh conversation from scratch."

# ============================================================
# SPEECH OUTPUT (TTS)
# ============================================================

def clean_speech_text(text):
    clean = re.sub(r"[*_#`~]", "", text)
    clean = re.sub(r"\[.*?\]\(.*?\)", "", clean)
    return clean.strip()


def stop_current_speech():
    global tts_process, is_speaking
    with tts_lock:
        if tts_process:
            try:
                if tts_process.poll() is None:
                    tts_process.terminate()
            except Exception:
                pass
            tts_process = None
        is_speaking = False


def speak(text):
    global tts_process, is_speaking

    if not text:
        return

    raw_text = clean_speech_text(str(text))
    if not raw_text:
        return

    stop_current_speech()
    print(f"\n{ASSISTANT_NAME}: {raw_text}\n")
    logger.info("Speaking: %s", raw_text)

    if OPERATING_SYSTEM == "Darwin":
        try:
            with tts_lock:
                is_speaking = True
                tts_process = subprocess.Popen(["/usr/bin/say","-v", "Samantha","-r", "180",raw_text])

            def monitor_mac():
                global is_speaking, tts_process
                try:
                    tts_process.wait()
                except Exception:
                    pass
                finally:
                    is_speaking = False
                    tts_process = None

            threading.Thread(target=monitor_mac, daemon=True).start()
            return
        except Exception as e:
            logger.error("macOS TTS error: %s", e)

    elif OPERATING_SYSTEM == "Windows":
        try:
            safe_text = raw_text.replace("'", "''")
            command = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Rate = 0; $s.Volume = 100; "
                f"$s.Speak('{safe_text}'); $s.Dispose();"
            )
            with tts_lock:
                is_speaking = True
                tts_process = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", command],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )

            def monitor_windows():
                global is_speaking, tts_process
                try:
                    tts_process.wait()
                except Exception:
                    pass
                finally:
                    is_speaking = False
                    tts_process = None

            threading.Thread(target=monitor_windows, daemon=True).start()
            return
        except Exception as e:
            logger.error("Windows TTS error: %s", e)

    print(raw_text)

# ============================================================
# BROWSER & URL OPENING
# ============================================================

def open_url_in_browser(url, browser_name=None):
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    resolved_browser = None
    if browser_name:
        resolved_browser = BROWSER_ALIASES.get(browser_name.strip().lower(), browser_name.strip())

    try:
        if OPERATING_SYSTEM == "Darwin":
            if resolved_browser:
                subprocess.Popen(["open", "-a", resolved_browser, url])
            else:
                subprocess.Popen(["open", url])
            return True

        elif OPERATING_SYSTEM == "Windows":
            if resolved_browser:
                win_exec = "chrome" if "chrome" in resolved_browser.lower() else "msedge"
                subprocess.Popen(["cmd", "/c", "start", win_exec, url], shell=False)
            else:
                os.startfile(url)
            return True
        else:
            webbrowser.open(url)
            return True
    except Exception as e:
        logger.error("Failed to open URL: %s", e)
        return False


def google_search(query):
    query = query.strip()
    if not query:
        return "Please tell me what to search for."
    url = f"https://www.google.com/search?q={quote_plus(query)}"
    if open_url_in_browser(url):
        return f"Searching Google for {query}."
    return "I could not open the browser."


def youtube_search(query):
    query = query.strip()
    if not query:
        return "Please tell me what to search for on YouTube."
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    if open_url_in_browser(url):
        return f"Searching YouTube for {query}."
    return "I could not open YouTube."


def open_gmail_draft(recipient="", subject="", body=""):
    compose_url = (
        "https://mail.google.com/mail/u/0/?view=cm&fs=1"
        f"&to={quote_plus(recipient.strip())}"
        f"&su={quote_plus(subject.strip())}"
        f"&body={quote_plus(body.strip())}"
    )
    try:
        if OPERATING_SYSTEM == "Darwin":
            subprocess.Popen(["open", compose_url])
            return "Gmail draft opened in your browser. Review it before sending."
        if open_url_in_browser(compose_url):
            return "Gmail draft opened in your browser. Review it before sending."
    except Exception as e:
        logger.error("Failed to open Gmail draft: %s", e)
    return "I could not open Gmail."

# ============================================================
# ADVANCED BROWSER TAB CLOSING (MACOS)
# ============================================================

def close_browser_tab_by_keyword(keyword, browser_name=None):
    """
    Searches open tabs in Chrome and Safari on macOS and closes matching tabs.
    """
    if OPERATING_SYSTEM != "Darwin":
        return False

    closed = False
    keyword_clean = keyword.strip().lower()

    # 1. Search & close in Google Chrome
    if not browser_name or "chrome" in browser_name.lower():
        chrome_script = f'''
        tell application "System Events"
            set isRunning to (exists (processes where name is "Google Chrome"))
        end tell
        if isRunning then
            tell application "Google Chrome"
                set closedAny to false
                repeat with w in windows
                    repeat with i from (count of tabs of w) to 1 by -1
                        set t to tab i of w
                        set t_url to (URL of t) as text
                        set t_title to (title of t) as text
                        if (t_url contains "{keyword_clean}") or (t_title contains "{keyword_clean}") then
                            close t
                            set closedAny to true
                        end if
                    end repeat
                end repeat
                return closedAny
            end tell
        else
            return false
        end if
        '''
        try:
            res = subprocess.run(["osascript", "-e", chrome_script], capture_output=True, text=True)
            if "true" in res.stdout.lower():
                closed = True
        except Exception as e:
            logger.error("Chrome tab close error: %s", e)

    # 2. Search & close in Safari
    if not browser_name or "safari" in browser_name.lower():
        safari_script = f'''
        tell application "System Events"
            set isRunning to (exists (processes where name is "Safari"))
        end tell
        if isRunning then
            tell application "Safari"
                set closedAny to false
                repeat with w in windows
                    repeat with i from (count of tabs of w) to 1 by -1
                        set t to tab i of w
                        set t_url to (URL of t) as text
                        set t_name to (name of t) as text
                        if (t_url contains "{keyword_clean}") or (t_name contains "{keyword_clean}") then
                            close t
                            set closedAny to true
                        end if
                    end repeat
                end repeat
                return closedAny
            end tell
        else
            return false
        end if
        '''
        try:
            res = subprocess.run(["osascript", "-e", safari_script], capture_output=True, text=True)
            if "true" in res.stdout.lower():
                closed = True
        except Exception as e:
            logger.error("Safari tab close error: %s", e)

    return closed

# ============================================================
# SYSTEM PROCESS TERMINATOR (CROSS-PLATFORM)
# ============================================================

def terminate_process_or_app(app_name):
    """
    Robust process terminator that combines AppleScript, psutil, and pkill
    to bypass macOS -1743 TCC authorization blocks.
    """
    clean_name = app_name.strip()
    closed = False

    if OPERATING_SYSTEM == "Darwin":
        # Step 1: Graceful AppleScript Quit
        safe_name = clean_name.replace('\\', '\\\\').replace('"', '\\"')
        script = f'''
        tell application "System Events"
            if exists (processes where name is "{safe_name}") then
                tell application "{safe_name}" to quit
                return true
            end if
        end tell
        return false
        '''
        try:
            res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2)
            if "true" in res.stdout.lower():
                closed = True
        except Exception:
            pass

        # Step 2: Direct Process Termination via psutil (Bypasses AppleScript permissions)
        if psutil:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    pname = (proc.info['name'] or "").lower()
                    pcmd = " ".join(proc.info['cmdline'] or []).lower()
                    target_low = clean_name.lower()

                    if (target_low == pname or 
                        target_low in pname or 
                        f"/{target_low}.app" in pcmd):
                        proc.terminate()
                        closed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Step 3: Unix pkill fallback
        if not closed:
            res = subprocess.run(["pkill", "-ilf", clean_name], capture_output=True)
            if res.returncode == 0:
                closed = True

        if closed:
            return f"Closed {clean_name}."
        return f"I could not find {clean_name} running."

    elif OPERATING_SYSTEM == "Windows":
        if psutil:
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    pname = (proc.info['name'] or "").lower()
                    if clean_name.lower() in pname or (clean_name.lower() + ".exe") == pname:
                        proc.terminate()
                        closed = True
                except Exception:
                    pass

        if not closed:
            subprocess.run(["taskkill", "/F", "/IM", f"{clean_name}.exe"], capture_output=True)
            closed = True

        return f"Closed {clean_name}."

    return f"Closing {clean_name} is not supported on this OS."

# ============================================================
# MASTER APPLICATION & TAB CLOSER
# ============================================================

def close_application(target_name, specific_browser=None):
    """
    Intelligent Closer:
    - Closes matching browser tabs if target is a website (YouTube, WhatsApp Web, etc.)
    - Quits desktop application if target is installed locally
    """
    clean_target = target_name.strip().lower()

    # 1. Target is a known website or domain
    if clean_target in POPULAR_WEBSITES or "." in clean_target:
        # Check if user has an installed desktop app with the same name (e.g. WhatsApp.app)
        resolved_app = resolve_installed_app(clean_target)
        app_running = False

        if resolved_app and psutil:
            for proc in psutil.process_iter(['name']):
                try:
                    if resolved_app.lower() in (proc.info['name'] or "").lower():
                        app_running = True
                        break
                except Exception:
                    pass

        if app_running:
            return terminate_process_or_app(resolved_app)
        else:
            # Close the matching tab in Chrome or Safari
            if close_browser_tab_by_keyword(clean_target, specific_browser):
                b_label = f" on {specific_browser}" if specific_browser else ""
                return f"Closed {clean_target} tab{b_label}."
            return f"I couldn't find an open tab for {clean_target}."

    # 2. Target is a desktop application
    resolved_app = resolve_installed_app(clean_target) or clean_target
    return terminate_process_or_app(resolved_app)

# ============================================================
# TAB & APPLICATION SWITCHING
# ============================================================

def switch_tab(direction="next", browser=None):
    if OPERATING_SYSTEM == "Darwin":
        browser_name = BROWSER_ALIASES.get((browser or "").strip().lower(), browser or "")
        if browser_name:
            subprocess.run(["open", "-a", browser_name], check=False)
        bracket = "]" if direction == "next" else "["
        script = f'tell application "System Events" to keystroke "{bracket}" using {{command down, shift down}}'
        subprocess.run(["osascript", "-e", script], check=False)
        browser_label = f" on {browser_name}" if browser_name else ""
        return f"Switched to {direction} tab{browser_label}."

    else:
        if pyautogui:
            if direction == "next":
                pyautogui.hotkey("ctrl", "tab")
            else:
                pyautogui.hotkey("ctrl", "shift", "tab")
            return f"Switched to {direction} tab."
        return "Keyboard automation library is not installed."


def switch_to_named_tab(tab_target, browser=None):
    """
    Switches to a tab by name or index across Safari and Google Chrome.
    Searches across all open windows and brings the matching window forward.
    """
    if OPERATING_SYSTEM != "Darwin":
        return "Named tab switching is only supported on macOS."

    clean_target = tab_target.strip().lower()
    clean_target = re.sub(r"^(?:the\s+)?tab\s+", "", clean_target).strip()
    safe_name = clean_target.replace("\\", "\\\\").replace('"', '\\"')

    # Detect numeric tab index
    target_index = None
    if clean_target.isdigit():
        target_index = int(clean_target)

    # Determine which browsers to search
    target_browsers = []
    if browser:
        resolved = BROWSER_ALIASES.get(browser.strip().lower(), browser.strip())
        if "chrome" in resolved.lower():
            target_browsers = ["Google Chrome"]
        elif "safari" in resolved.lower():
            target_browsers = ["Safari"]
    else:
        # Check active/running browsers automatically
        if psutil:
            running_procs = [p.info['name'] for p in psutil.process_iter(['name']) if p.info['name']]
            if any("Google Chrome" in p for p in running_procs):
                target_browsers.append("Google Chrome")
            if any("Safari" in p for p in running_procs):
                target_browsers.append("Safari")
        if not target_browsers:
            target_browsers = ["Google Chrome", "Safari"]

    for b in target_browsers:
        if b == "Google Chrome":
            if target_index is not None:
                script = f'''
                tell application "Google Chrome"
                    activate
                    if (count of windows) > 0 then
                        tell front window
                            if (count of tabs) >= {target_index} then
                                set active tab index to {target_index}
                                return "FOUND"
                            end if
                        end tell
                    end if
                end tell
                return "NOT_FOUND"
                '''
            else:
                script = f'''
                tell application "Google Chrome"
                    activate
                    repeat with w in windows
                        set tab_idx to 0
                        repeat with t in tabs of w
                            set tab_idx to tab_idx + 1
                            set t_title to (title of t as text)
                            set t_url to (URL of t as text)
                            if (t_title contains "{safe_name}") or (t_url contains "{safe_name}") then
                                set active tab index of w to tab_idx
                                set index of w to 1
                                return "FOUND"
                            end if
                        end repeat
                    end repeat
                end tell
                return "NOT_FOUND"
                '''
        else:  # Safari
            if target_index is not None:
                script = f'''
                tell application "Safari"
                    activate
                    if (count of windows) > 0 then
                        tell front window
                            if (count of tabs) >= {target_index} then
                                set current tab to tab {target_index}
                                return "FOUND"
                            end if
                        end tell
                    end if
                end tell
                return "NOT_FOUND"
                '''
            else:
                script = f'''
                tell application "Safari"
                    activate
                    repeat with w in windows
                        repeat with t in tabs of w
                            set t_name to (name of t as text)
                            set t_url to (URL of t as text)
                            if (t_name contains "{safe_name}") or (t_url contains "{safe_name}") then
                                set current tab of w to t
                                set index of w to 1
                                return "FOUND"
                            end if
                        end repeat
                    end repeat
                end tell
                return "NOT_FOUND"
                '''

        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if "FOUND" in res.stdout:
            return f"Switched to tab {tab_target} on {b}."

    return f"I could not find an open tab for {tab_target}."


def open_new_tab(browser=None):
    if OPERATING_SYSTEM == "Darwin":
        browser_name = BROWSER_ALIASES.get((browser or "").strip().lower(), browser or "")
        if browser_name:
            subprocess.run(["open", "-a", browser_name], check=False)
        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "t" using command down'],
            check=False
        )
        return f"Opened a new tab{f' on {browser_name}' if browser_name else ''}."
    else:
        if pyautogui:
            pyautogui.hotkey("ctrl", "t")
            return "Opened a new tab."
        return "Keyboard automation library is not installed."


def switch_application(target_app=None):
    if target_app:
        resolved = resolve_installed_app(target_app) or target_app.strip()
        try:
            if OPERATING_SYSTEM == "Darwin":
                script = f'tell application "{resolved}" to activate'
                res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
                if res.returncode != 0:
                    subprocess.Popen(["open", "-a", resolved])
                return f"Switched to {resolved}."
            elif OPERATING_SYSTEM == "Windows":
                subprocess.Popen(["cmd", "/c", "start", "", resolved], shell=False)
                return f"Switched to {resolved}."
            return f"Could not switch to {resolved}."
        except Exception as e:
            logger.error("App switch error: %s", e)
            return f"Could not switch to {resolved}."

    try:
        if OPERATING_SYSTEM == "Darwin":
            subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 48 using command down'], check=False)
            return "Switched application."
        elif pyautogui:
            pyautogui.hotkey("alt", "tab")
            return "Switched application."
        return "Application switching is not configured."
    except Exception as e:
        logger.error("Generic app switch error: %s", e)
        return "Could not switch application."


def close_tab():
    if OPERATING_SYSTEM == "Darwin":
        subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "w" using command down'], check=False)
        return "Tab closed."
    elif pyautogui:
        pyautogui.hotkey("ctrl", "w")
        return "Tab closed."
    return "Keyboard control is not installed."


def close_window():
    if OPERATING_SYSTEM == "Darwin":
        subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "w" using {command down, shift down}'], check=False)
        return "Window closed."
    elif pyautogui:
        pyautogui.hotkey("alt", "f4")
        return "Window closed."
    return "Keyboard control is not installed."


def close_current_app():
    if OPERATING_SYSTEM == "Darwin":
        subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke "q" using command down'], check=False)
        return "Closed application."
    elif pyautogui:
        pyautogui.hotkey("alt", "f4")
        return "Closed application."
    return "Keyboard control is not installed."

# ============================================================
# OPEN TARGET (APP OR WEBSITE)
# ============================================================

def open_target(target_name, specific_browser=None):
    clean_target = target_name.strip().lower()

    if clean_target in POPULAR_WEBSITES:
        url = POPULAR_WEBSITES[clean_target]
        open_url_in_browser(url, specific_browser)
        browser_label = f" on {specific_browser}" if specific_browser else ""
        return f"Opening {clean_target}{browser_label}."

    if re.search(r"^[\w\-]+(\.[\w\-]+)+", clean_target) and not clean_target.endswith(".app"):
        open_url_in_browser(clean_target, specific_browser)
        browser_label = f" on {specific_browser}" if specific_browser else ""
        return f"Opening {clean_target}{browser_label}."

    resolved_app = resolve_installed_app(clean_target) or target_name.strip()

    try:
        if OPERATING_SYSTEM == "Darwin":
            res = subprocess.run(["open", "-a", resolved_app], capture_output=True, text=True)
            if res.returncode == 0:
                return f"Opening {resolved_app}."
            return f"I could not find an application named {target_name}."
        elif OPERATING_SYSTEM == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", resolved_app], shell=False)
            return f"Opening {resolved_app}."
        return f"I could not launch {resolved_app}."
    except Exception as e:
        logger.error("Open application failed: %s", e)
        return f"I could not launch {resolved_app}."

# ============================================================
# HARDWARE & SYSTEM STATUS
# ============================================================

def get_time():
    return datetime.now().strftime("%I:%M %p")


def get_date():
    return datetime.now().strftime("%A, %d %B %Y")


def system_status():
    if not psutil:
        return f"Operating system: {platform.system()} {platform.release()}"
    try:
        cpu = psutil.cpu_percent(interval=0.3)
        ram = psutil.virtual_memory().percent
        return f"CPU usage is {cpu:.0f} percent. RAM usage is {ram:.0f} percent."
    except Exception as e:
        logger.error("System status error: %s", e)
        return "I could not read the system status."


def take_screenshot():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(BASE_DIR, f"AIVORA_screenshot_{timestamp}.png")
    try:
        if OPERATING_SYSTEM == "Darwin":
            subprocess.run(["screencapture", "-x", filename], check=True)
        elif pyautogui:
            image = pyautogui.screenshot()
            image.save(filename)
        else:
            return "Screenshot support is not installed."
        return "Screenshot saved."
    except Exception as e:
        logger.error("Screenshot error: %s", e)
        return "I could not take the screenshot."


def open_camera():
    try:
        if OPERATING_SYSTEM == "Windows":
            subprocess.Popen(["explorer.exe", "microsoft.windows.camera:"])
            return "Opening the camera."
        elif OPERATING_SYSTEM == "Darwin":
            subprocess.Popen(["open", "-a", "Photo Booth"])
            return "Opening the camera."
        return "Camera control is not configured."
    except Exception as e:
        logger.error("Camera error: %s", e)
        return "I could not open the camera."


def show_desktop():
    try:
        if OPERATING_SYSTEM == "Windows" and pyautogui:
            pyautogui.hotkey("win", "d")
            return "Showing the desktop."
        elif OPERATING_SYSTEM == "Darwin":
            subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 103'], check=False)
            return "Showing the desktop."
        return "Show desktop not configured."
    except Exception as e:
        logger.error("Show desktop error: %s", e)
        return "I could not show the desktop."


def set_brightness(value):
    if not sbc:
        return "Brightness control is not installed."
    try:
        value = max(0, min(100, int(value)))
        sbc.set_brightness(value)
        return f"Brightness set to {value} percent."
    except Exception as e:
        logger.error("Brightness error: %s", e)
        return "I could not change brightness."


def set_volume(level):
    try:
        level = max(0, min(100, int(level)))
        if OPERATING_SYSTEM == "Windows" and AudioUtilities:
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
            volume.SetMasterVolumeLevelScalar(level / 100, None)
            return f"Volume set to {level} percent."
        elif OPERATING_SYSTEM == "Darwin":
            subprocess.run(["osascript", "-e", f"set volume output volume {level}"], check=True)
            return f"Volume set to {level} percent."
        return "Volume control unavailable."
    except Exception as e:
        logger.error("Volume error: %s", e)
        return "I could not change the volume."


def change_volume(amount):
    try:
        if OPERATING_SYSTEM == "Darwin":
            result = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True,
                text=True
            )
            current = int(result.stdout.strip())
            new_level = max(0, min(100, current + amount))
            subprocess.run(["osascript", "-e", f"set volume output volume {new_level}"])
            return f"Volume set to {new_level} percent."
        elif OPERATING_SYSTEM == "Windows" and AudioUtilities:
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)
            current = int(volume.GetMasterVolumeLevelScalar() * 100)
            new_level = max(0, min(100, current + amount))
            volume.SetMasterVolumeLevelScalar(new_level / 100, None)
            return f"Volume set to {new_level} percent."
        return "Volume adjust not supported."
    except Exception as e:
        logger.error("Change volume error: %s", e)
        return "I could not adjust the volume."

# ============================================================
# SEARCH & AI LOGIC
# ============================================================

def is_online_search_trigger(command):
    text = command.lower().strip()
    search_phrases = [
        "search for", "search online", "look up", "find online",
        "search the internet", "search internet", "on the internet",
        "latest news", "current news", "news about", "today's news",
        "today news", "latest info", "current info", "latest information",
        "what happened today", "who is the current", "what is the latest",
        "tell me about the latest", "who won", "score of"
    ]
    return any(phrase in text for phrase in search_phrases)


def extract_search_query(command):
    original = command.strip()
    text = original.lower().strip()
    patterns = [
        "search google for ", "search google ", "google search for ", "google search ",
        "search youtube for ", "search youtube ", "youtube search for ", "youtube search ",
        "search online for ", "search online ", "search the internet for ", "search the internet ",
        "search for ", "search ", "look up ", "find online ", "find on the internet ",
        "news about ", "latest news about ", "latest information about ", "current information about "
    ]
    for pattern in patterns:
        if text.startswith(pattern):
            return original[len(pattern):].strip()
    return original


def ask_gemini_with_memory(prompt, is_new_search=False):
    global unified_history
    chat = get_gemini_chat()

    if is_new_search:
        clean_query = extract_search_query(prompt)
        open_url_in_browser(f"https://www.google.com/search?q={quote_plus(clean_query)}")
    else:
        clean_query = prompt.strip()

    if not chat:
        speak("Gemini online client is not connected. Opening web search.")
        open_url_in_browser(f"https://www.google.com/search?q={quote_plus(clean_query)}")
        return

    try:
        logger.info("Sending message to Gemini chat session: %s", clean_query)
        response = chat.send_message(clean_query)
        answer = response.text.strip() if response and response.text else ""

        if answer:
            # Save turn to disk
            unified_history.append({"role": "user", "content": clean_query})
            unified_history.append({"role": "assistant", "content": answer})
            save_persistent_history(unified_history)
            speak(answer)
        else:
            speak("I received an empty response from online search.")
    except Exception as e:
        logger.error("Gemini session error: %s", e)
        speak("I encountered an issue with the online search session.")


def ask_ollama_with_memory(prompt):
    global ollama_history,unified_history
    if not requests:
        return "The requests library is not installed."

    prompt = str(prompt).strip()
    if not prompt:
        return ""

    ollama_history.append({"role": "user", "content": prompt})
    unified_history.append({"role": "user", "content": prompt})

    if len(ollama_history) > MAX_OLLAMA_TURNS * 2:
        ollama_history = ollama_history[-(MAX_OLLAMA_TURNS * 2):]

    messages = [
        {
            "role": "system",
            "content": (
                "You are AIVORA, a fast desktop voice assistant. "
                "Give short, natural conversational answers in 1 to 2 sentences. "
                "Do not use markdown, lists, or asterisks. "
                "Remember the context of previous messages."
            )
        }
    ] + ollama_history

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.5}
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=25)
        response.raise_for_status()
        data = response.json()
        answer = data.get("message", {}).get("content", "").strip()

        if answer:
            ollama_history.append({"role": "assistant", "content": answer})
            unified_history.append({"role": "assistant", "content": answer})
            save_persistent_history(unified_history)
            return answer
        return "I received an empty response from local AI."
    except requests.exceptions.ConnectionError:
        return "I cannot reach Ollama. Please make sure it is running."
    except Exception as e:
        logger.error("Ollama error: %s", e)
        return "I had trouble processing that locally."

# ============================================================
# NATURAL LANGUAGE COMMAND PARSER
# ============================================================

def clean_input_command(raw_text):
    cleaned = raw_text.strip()
    noise_pattern = r"^(?:sorry|please|hey\s+aivora|aivora|can\s+you|could\s+you|would\s+you|just)\s+"
    while re.search(noise_pattern, cleaned, re.IGNORECASE):
        cleaned = re.sub(noise_pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def process_local_command(command):
    global running, current_session_mode

    cleaned_command = clean_input_command(command)
    text = cleaned_command.lower().strip()

    if not text:
        return False, ""

    # Refresh app index
    if text in ["refresh apps", "reload apps", "scan applications", "update apps"]:
        count = scan_installed_applications()
        return True, f"Refreshed application index. Found {count} applications."

    # Memory reset
    if text in ["new topic", "reset chat", "clear memory", "forget that", "start over", "clear history"]:
        return True, reset_memory()

    # 1. Close Specific Tab / Window / App Keywords
    if text in ["close tab", "close this tab"]:
        return True, close_tab()

    if text in ["close window", "close this window"]:
        return True, close_window()

    if text in ["close app", "close current app", "close this app", "quit app", "quit current app"]:
        return True, close_current_app()

    # 2. Targeted Browser Tab/App Closing: "close YouTube on Chrome", "close WhatsApp in Safari"
    close_browser_target_match = re.match(
        r"^(?:close|quit|kill)\s+(.+?)\s+(?:on|in|from|using)\s+([a-zA-Z\s]+)$",
        cleaned_command,
        re.IGNORECASE
    )
    if close_browser_target_match:
        target = close_browser_target_match.group(1).strip()
        browser = close_browser_target_match.group(2).strip()
        return True, close_application(target, specific_browser=browser)

    # 3. Standard Closing: "close YouTube", "close Chrome", "quit VS Code"
    close_match = re.match(r"^(?:close|quit|kill)\s+(.+)$", cleaned_command, re.IGNORECASE)
    if close_match:
        target = close_match.group(1).strip()
        if target not in ["aivora"]:
            return True, close_application(target)

    # 4. Targeted Tab Switching on Chrome or Safari
    tab_browser_match = re.match(
        r"^(?:switch|next|change|previous|prev|back)\s+tab\s+(?:on|in|with)\s+([a-zA-Z\s]+)$",
        text
    )
    if tab_browser_match:
        browser = tab_browser_match.group(1).strip()
        direction = "prev" if any(w in text for w in ["previous", "prev", "back"]) else "next"
        return True, switch_tab(direction=direction, browser=browser)

    # 5. General Tab Switching
    if any(q == text for q in ["switch tab", "next tab", "change tab", "switch to next tab"]):
        return True, switch_tab(direction="next")

    if any(q == text for q in ["previous tab", "prev tab", "back tab", "last tab"]):
        return True, switch_tab(direction="prev")

    named_tab_match = re.match(
        r"^(?:switch\s+to|go\s+to|select)\s+(?:the\s+)?tab\s+(.+?)\s+(?:on|in)\s+(safari|chrome|google\s+chrome)$",
        cleaned_command,
        re.IGNORECASE
    )
    if named_tab_match:
        return True, switch_to_named_tab(
            named_tab_match.group(1),
            named_tab_match.group(2)
        )

    named_tab_match = re.match(
        r"^(?:switch\s+to|go\s+to|select)\s+(.+?)\s+tab\s+(?:on|in)\s+(safari|chrome|google\s+chrome)$",
        cleaned_command,
        re.IGNORECASE
    )
    if named_tab_match:
        return True, switch_to_named_tab(
            named_tab_match.group(1),
            named_tab_match.group(2)
        )

    new_tab_match = re.match(
        r"^(?:(?:open|launch|start|create)\s+)?(?:(?:a|the)\s+)?new\s+tab(?:\s+(?:on|in|with|using)\s+([a-zA-Z\s]+))?$",
        cleaned_command,
        re.IGNORECASE
    )
    if new_tab_match:
        browser = new_tab_match.group(1)
        return True, open_new_tab(browser=browser.strip() if browser else None)

    if text == "new tab":
        return True, open_new_tab()

    draft_email_prefix = r"^(?:write|draft|compose|create)\s+(?:an?\s+)?email\s+to\s+"
    draft_patterns = [
        draft_email_prefix + r"(.+?)\s+subject\s+(.+?)\s+body\s+(.+)$",
        draft_email_prefix + r"(.+?)\s+subject\s+(.+)$",
        draft_email_prefix + r"(.+)$",
    ]
    for pattern in draft_patterns:
        draft_email_match = re.match(pattern, cleaned_command, re.IGNORECASE)
        if draft_email_match:
            fields = draft_email_match.groups()
            recipient = fields[0].strip()
            subject = fields[1].strip() if len(fields) > 1 else ""
            body = fields[2].strip() if len(fields) > 2 else ""
            return True, open_gmail_draft(recipient, subject, body)

    # 6. Targeted App Switching
    app_switch_match = re.match(r"^(?:switch\s+to|switch\s+app\s+to|change\s+to)\s+(.+)$", cleaned_command, re.IGNORECASE)
    if app_switch_match:
        target_app = app_switch_match.group(1).strip()
        return True, switch_application(target_app)

    # 7. Generic App Switching
    if any(q == text for q in ["switch app", "switch application", "next app", "change app", "next application"]):
        return True, switch_application()

    # 8. Direct Google & YouTube Searches
    for p in ["search google for ", "search google ", "google search for ", "google search "]:
        if text.startswith(p):
            return True, google_search(cleaned_command[len(p):].strip())

    for p in ["search youtube for ", "search youtube ", "youtube search for ", "youtube search ", "play on youtube "]:
        if text.startswith(p):
            return True, youtube_search(cleaned_command[len(p):].strip())

    # 9. Targeted Browser Launching: "open YouTube on Chrome", "open WhatsApp in Safari"
    browser_launch_match = re.match(
        r"^(?:open|launch|start|go to)\s+(.+?)\s+(?:on|in|with|using)\s+([a-zA-Z\s]+)$",
        cleaned_command,
        re.IGNORECASE
    )
    if browser_launch_match:
        target = browser_launch_match.group(1).strip()
        browser = browser_launch_match.group(2).strip()
        if browser.lower() in BROWSER_ALIASES:
            return True, open_target(target, specific_browser=browser)

    if any(q in text for q in ["open camera", "start camera", "launch camera"]):
        return True, open_camera()

    # 10. Standard Opening: Matches any local app or website
    open_match = re.match(r"^(?:open|launch|start|go to)\s+(.+)$", cleaned_command, re.IGNORECASE)
    if open_match:
        target = open_match.group(1).strip()
        return True, open_target(target)

    # 11. Generic Web Search
    for p in ["search for ", "search "]:
        if text.startswith(p):
            return True, google_search(cleaned_command[len(p):].strip())

    # 12. System Status & Time
    if any(q in text for q in ["what time is it", "current time", "tell me the time"]) or text == "time":
        return True, f"The time is {get_time()}."

    if any(q in text for q in ["what is today's date", "what is the date", "today's date", "tell me the date"]) or text == "date":
        return True, f"Today is {get_date()}."

    if any(q in text for q in ["system status", "system information", "system info", "cpu usage", "ram usage"]):
        return True, system_status()

    if any(q in text for q in ["take a screenshot", "take screenshot", "screenshot", "capture screen"]):
        return True, take_screenshot()

    if "show desktop" in text or "show the desktop" in text:
        return True, show_desktop()

    # 13. Volume & Brightness
    vol_match = re.search(r"(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})", text)
    if vol_match:
        return True, set_volume(int(vol_match.group(1)))

    if any(q in text for q in ["increase volume", "volume up", "turn up volume"]):
        return True, change_volume(10)

    if any(q in text for q in ["decrease volume", "volume down", "turn down volume"]):
        return True, change_volume(-10)

    br_match = re.search(r"(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})", text)
    if br_match:
        return True, set_brightness(int(br_match.group(1)))

    # 14. Shutdown & Exit
    if text in ["exit", "quit", "goodbye", "stop", "shutdown aivora", "close aivora"]:
        running = False
        return True, "Goodbye."

    return False, ""

# ============================================================
# OFFLINE VOSK SPEECH RECOGNITION
# ============================================================

VOSK_MODEL_PATH = os.path.join(BASE_DIR, "vosk-model")

try:
    vosk_model = Model(VOSK_MODEL_PATH)
    print("[Vosk] Offline speech recognition ready.")
except Exception as e:
    vosk_model = None
    logger.error("Vosk model loading failed: %s", e)
    print(f"[Vosk Error] {e}")


def offline_listen(timeout=5, phrase_time_limit=8):
    """
    Offline speech recognition using Vosk + sounddevice.
    Uses the MacBook Pro microphone when available.
    Works without Wi-Fi.
    """

    if vosk_model is None:
        print("\n[Vosk Error] Offline model is not loaded.")
        return ""

    if sd is None:
        print("\n[Vosk Error] sounddevice is not installed.")
        return ""

    audio_queue = queue.Queue()

    def callback(indata, frames, time_info, status):
        if status:
            logger.warning("Microphone status: %s", status)

        audio_queue.put(bytes(indata))

    try:
        # ----------------------------------------------------
        # Find microphone
        # ----------------------------------------------------

        devices = sd.query_devices()

        input_device_index = None
        input_device = None

        # Prefer MacBook built-in microphone
        preferred_names = [
            "MacBook Pro Microphone",
            "MacBook Pro",
            "Built-in Microphone",
            "Built-in Audio"
        ]

        for preferred in preferred_names:

            for index, device in enumerate(devices):

                device_name = device["name"]

                if (
                    preferred.lower() in device_name.lower()
                    and device["max_input_channels"] > 0
                ):
                    input_device = device
                    input_device_index = index
                    break

            if input_device is not None:
                break

        # ----------------------------------------------------
        # If preferred microphone was not found,
        # use the first available input device.
        # ----------------------------------------------------

        if input_device is None:

            for index, device in enumerate(devices):

                if device["max_input_channels"] > 0:

                    input_device = device
                    input_device_index = index
                    break

        if input_device is None:

            print("\n[Vosk Error] No microphone found.")
            return ""

        print(
            f"\n[Vosk] Microphone: "
            f"{input_device['name']}"
        )

        # ----------------------------------------------------
        # Audio settings
        # ----------------------------------------------------

        sample_rate = 16000

        print(
            f"[Vosk] Sample rate: "
            f"{sample_rate} Hz"
        )

        recognizer = KaldiRecognizer(
            vosk_model,
            sample_rate
        )

        recognizer.SetWords(False)

        # ----------------------------------------------------
        # Start listening
        # ----------------------------------------------------

        print(
            "\rListening...               ",
            end="",
            flush=True
        )

        start_time = time.time()

        speech_started = False
        speech_start_time = None

        with sd.RawInputStream(
            samplerate=sample_rate,
            blocksize=4000,
            device=input_device_index,
            dtype="int16",
            channels=1,
            callback=callback
        ):

            while True:

                elapsed = time.time() - start_time

                # ------------------------------------------------
                # No speech detected within timeout
                # ------------------------------------------------

                if (
                    not speech_started
                    and elapsed >= timeout
                ):

                    print(
                        "\r" + " " * 60 + "\r",
                        end="",
                        flush=True
                    )

                    return ""

                # ------------------------------------------------
                # Maximum speech duration
                # ------------------------------------------------

                if (
                    speech_started
                    and speech_start_time is not None
                ):

                    speech_duration = (
                        time.time()
                        - speech_start_time
                    )

                    if speech_duration >= phrase_time_limit:

                        break

                # ------------------------------------------------
                # Get microphone audio
                # ------------------------------------------------

                try:

                    data = audio_queue.get(
                        timeout=0.2
                    )

                except queue.Empty:

                    continue

                # ------------------------------------------------
                # Vosk recognition
                # ------------------------------------------------

                if recognizer.AcceptWaveform(data):

                    result = json.loads(
                        recognizer.Result()
                    )

                    text = result.get(
                        "text",
                        ""
                    ).strip()

                    if text:
                        return text

                else:

                    partial_result = json.loads(
                        recognizer.PartialResult()
                    )

                    partial = partial_result.get(
                        "partial",
                        ""
                    ).strip()

                    if (
                        partial
                        and not speech_started
                    ):

                        speech_started = True

                        speech_start_time = (
                            time.time()
                        )

        # ----------------------------------------------------
        # Final Vosk result
        # ----------------------------------------------------

        result = json.loads(
            recognizer.FinalResult()
        )

        text = result.get(
            "text",
            ""
        ).strip()

        return text

    except Exception as e:

        logger.exception(
            "Offline Vosk microphone error"
        )

        print(
            f"\n[Vosk Error: {e}]"
        )

        return ""
# ============================================================
# VOICE & TEXT LISTENERS
# ============================================================

def listen_voice(recognizer=None, mic=None, timeout=5, phrase_time_limit=8):
    """
    AIVORA hybrid voice recognition.

    1. Vosk = offline recognition
    2. Google Speech Recognition = online fallback

    Vosk allows AIVORA to work without Wi-Fi.
    Google can help when Vosk cannot understand a command.
    """

    if is_speaking:
        return ""

    # ========================================================
    # 1. OFFLINE FIRST: VOSK
    # ========================================================

    try:
        command = offline_listen(
            timeout=timeout,
            phrase_time_limit=phrase_time_limit
        )

        if command and command.strip():
            print(f"\rYou: {command}               ")
            return command.strip()

    except Exception as e:
        logger.error("Vosk recognition failed: %s", e)

    # ========================================================
    # 2. ONLINE FALLBACK: GOOGLE
    # ========================================================

    if recognizer is not None and mic is not None and sr is not None:
        try:
            with mic as source:
                print("\rOnline recognition...       ", end="", flush=True)

                audio = recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=phrase_time_limit
                )

            command = recognizer.recognize_google(
                audio,
                language="en-IN"
            )

            if command:
                print(f"\rYou: {command}               ")
                return command.strip()

        except sr.WaitTimeoutError:
            pass

        except sr.UnknownValueError:
            pass

        except sr.RequestError as e:
            logger.info("Google Speech unavailable: %s", e)

        except Exception as e:
            logger.error("Online speech recognition failed: %s", e)

    # ========================================================
    # 3. NOTHING WORKED
    # ========================================================

    return ""


def listen_text(prompt_text="You: "):
    """Read a command from the terminal in text mode."""
    try:
        command = input(prompt_text).strip()
        if command:
            print(f"\rYou: {command}               ")
        return command
    except EOFError:
        return ""
    except KeyboardInterrupt:
        raise

# ============================================================
# MAIN RUNTIME LOOP
# ============================================================

def main():

    global running
    global current_session_mode

    # ========================================================
    # VOICE ENGINE SETUP
    # ========================================================

    recognizer = None
    mic = None

    # Vosk is our offline voice engine
    vosk_available = (
        vosk_model is not None
    )

    # Google Speech Recognition is our online fallback
    google_available = False

    if not TEXT_MODE and sr is not None:

        try:

            recognizer = sr.Recognizer()

            recognizer.energy_threshold = 300
            recognizer.dynamic_energy_threshold = True

            mic = sr.Microphone()

            google_available = True

            logger.info(
                "Google Speech Recognition available."
            )

        except Exception as e:

            logger.warning(
                "Google Speech Recognition unavailable: %s",
                e
            )

            recognizer = None
            mic = None

    # ========================================================
    # FINAL VOICE MODE DECISION
    # ========================================================

    active_voice_mode = (
        not TEXT_MODE
        and (
            vosk_available
            or google_available
        )
    )

    # ========================================================
    # APPLICATION INDEX
    # ========================================================

    total_apps = scan_installed_applications()

    # ========================================================
    # STARTUP INFORMATION
    # ========================================================

    print("\n" + "=" * 60)
    print("                      AIVORA")
    print("=" * 60)

    print(
        f" Operating System    : {OPERATING_SYSTEM}"
    )

    print(
        f" Indexed Local Apps  : {total_apps} applications loaded"
    )

    print(
        f" Persistent Memory   : "
        f"{len(unified_history)} past messages restored"
    )

    print(
        f" Gemini Multi-turn   : "
        f"{'Active' if gemini_client else 'Disabled'}"
    )

    print(
        f" Local Ollama Engine : {OLLAMA_MODEL}"
    )

    print(
        f" Vosk Offline Voice  : "
        f"{'Available' if vosk_available else 'Unavailable'}"
    )

    print(
        f" Google Online Voice : "
        f"{'Available' if google_available else 'Unavailable'}"
    )

    print(
        f" Active Input Mode   : "
        f"{'VOICE' if active_voice_mode else 'TEXT'}"
    )

    print("=" * 60 + "\n")

    # ========================================================
    # OPTIONAL GOOGLE MICROPHONE CALIBRATION
    # ========================================================

    if (
        active_voice_mode
        and recognizer is not None
        and mic is not None
    ):

        try:

            print(
                "Calibrating microphone... ",
                end="",
                flush=True
            )

            with mic as source:

                recognizer.adjust_for_ambient_noise(
                    source,
                    duration=1.0
                )

            print("Done.\n")

        except Exception as e:

            print(
                f"Skipped ({e})\n"
            )

    # ========================================================
    # START AIVORA
    # ========================================================

    logger.info(
        "AIVORA started."
    )

    speak(
        "AIVORA is ready."
    )

    while is_speaking:
        time.sleep(0.05)

    # ========================================================
    # MAIN COMMAND LOOP
    # ========================================================

    while running:

        try:

            # ------------------------------------------------
            # VOICE MODE
            # ------------------------------------------------

            if active_voice_mode:

                command = listen_voice(
                    recognizer,
                    mic,
                    timeout=5,
                    phrase_time_limit=8
                )

            # ------------------------------------------------
            # TEXT MODE
            # ------------------------------------------------

            else:

                command = listen_text()

            if not command:
                continue

            command = command.strip()

            if not command:
                continue

            logger.info(
                "User command: %s",
                command
            )

            # =================================================
            # 1. LOCAL OS COMMANDS
            # =================================================

            handled, response = process_local_command(
                command
            )

            if handled:

                if response:
                    speak(response)

                while is_speaking:
                    time.sleep(0.05)

                continue

            # =================================================
            # 2. EXPLICIT ONLINE SEARCH
            # =================================================

            if is_online_search_trigger(command):

                current_session_mode = "ONLINE"

                ask_gemini_with_memory(
                    command,
                    is_new_search=True
                )

                while is_speaking:
                    time.sleep(0.05)

                continue

            # =================================================
            # 3. ONLINE FOLLOW-UP
            # =================================================

            if (
                current_session_mode == "ONLINE"
                and gemini_client
            ):

                logger.info(
                    "Continuing online Gemini session."
                )

                ask_gemini_with_memory(
                    command,
                    is_new_search=False
                )

                while is_speaking:
                    time.sleep(0.05)

                continue

            # =================================================
            # 4. LOCAL OFFLINE AI
            # =================================================

            current_session_mode = "LOCAL"

            response = ask_ollama_with_memory(
                command
            )

            if response:

                speak(response)

                while is_speaking:
                    time.sleep(0.05)

        # =====================================================
        # KEYBOARD INTERRUPT
        # =====================================================

        except KeyboardInterrupt:

            running = False

            break

        # =====================================================
        # RUNTIME ERROR
        # =====================================================

        except Exception as e:

            logger.exception(
                "Main loop runtime error: %s",
                e
            )

            print(
                f"\nAIVORA error: {e}"
            )

            time.sleep(0.5)

    # ========================================================
    # SHUTDOWN
    # ========================================================

    stop_current_speech()

    print(
        "\nAIVORA stopped."
    )

    logger.info(
        "AIVORA stopped."
    )

if __name__ == "__main__":
    main()