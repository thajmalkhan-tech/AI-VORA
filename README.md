# AI-VORA

A Python desktop voice assistant that works offline first. Speak a command to open apps and websites, control tabs, adjust volume and brightness, or chat with an AI. If you are offline, it talks to a local model. If you are online, it can search and answer with Gemini.

## Features

- **Offline voice recognition** with [Vosk](https://alphacephei.com/vosk/), with Google Speech Recognition as an online fallback
- **Local AI chat** through [Ollama](https://ollama.com) (default model `llama3.2`), no internet needed
- **Online search and follow-up chat** with Google Gemini, with multi-turn memory
- **Persistent memory:** conversation history is saved to `aivora_memory.json` and restored on restart ("clear memory" wipes it)
- **App control:** auto-discovers installed apps, then opens, closes and switches between them
- **Browser control:** open websites, open and close tabs, switch tabs by number or name (Chrome and Safari on macOS)
- **Search shortcuts:** Google and YouTube searches by voice
- **Email drafts:** opens a pre-filled Gmail compose window for you to review and send
- **System tools:** volume, brightness, screenshots, CPU and RAM status, camera, show desktop, time and date
- **Text mode:** type commands instead of speaking them

## Example commands

```
open YouTube on Chrome
close WhatsApp
switch to tab 2 on Safari
search google for python tutorials
search youtube for lo-fi music
draft email to someone@example.com subject Meeting body See you at 5
set volume to 40
brightness 70
take a screenshot
what time is it
system status
clear memory
```

## Setup

**Requirements:** Python 3.10 or newer, a microphone, and macOS or Windows.

1. Clone the repo and install dependencies:
   ```
   git clone https://github.com/thajmalkhan-tech/ai-vora.git
   cd ai-vora
   pip install -r requirements.txt
   ```
2. Download a Vosk model from <https://alphacephei.com/vosk/models>, unzip it, and rename the folder to `vosk-model` in the project folder.
3. Copy `.env.example` to `.env` and add your Gemini API key (optional, only needed for online search).
4. For local AI chat, install [Ollama](https://ollama.com) and pull a model: `ollama pull llama3.2`.
5. Run it:
   ```
   python AI_VORA.py
   ```

Set `TEXT_MODE=true` in `.env` to type commands instead of speaking.

## How it decides what to do

1. Local OS commands (apps, tabs, volume and so on) are matched first with a command parser.
2. Phrases like "search for", "latest news" or "who won" go to Gemini, and open a Google search too.
3. Follow-up questions in an online session stay with Gemini.
4. Everything else goes to the local Ollama model.

## Platform support

| Feature | macOS | Windows |
| --- | --- | --- |
| Voice, local AI, Gemini | Yes | Yes |
| Open and close apps | Yes | Yes |
| Named tab switching and tab closing by name | Yes | No |
| Volume control | Yes | Yes (needs `pycaw`) |
| Brightness control | Limited (library support varies) | Yes (needs `screen-brightness-control`) |

macOS gets the most complete support because tab control uses AppleScript. Windows falls back to keyboard shortcuts through `pyautogui`.

## Author

Thajmal Khan: [GitHub](https://github.com/thajmalkhan-tech) · [LinkedIn](https://www.linkedin.com/in/thajmalkhan/)
