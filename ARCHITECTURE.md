# Architecture

```mermaid
flowchart TB
  Human[Presenter: joins and approves each action]
  subgraph Online[Online meeting transport]
    Meet[Google Meet or Teams web tab]
    Remote[Other meeting participants]
    Meet <--> Remote
  end
  subgraph Browser[Chrome on Windows]
    Picker[User initiated tab picker: video and audio]
    Worklet[Local AudioWorklet: mono PCM at actual sample rate]
    Dashboard[Dashboard: transcript, evidence, review, exports]
    Playback[HTML audio element: approved reply WAV]
    Picker --> Worklet
    Dashboard --> Playback
  end
  subgraph Local[Local machine: inference and storage]
    API[FastAPI: 127.0.0.1:8765]
    Gate[Host, Origin, cookie and session token checks]
    ASR[Bounded queue: one ASR worker, resample to 16 kHz]
    Whisper[faster-whisper small.en CPU int8]
    Session[Isolated meeting session and stable segment IDs]
    Retrieve[Local transcript read and search]
    LLM[llama.cpp: 127.0.0.1:8080]
    Validate[Exact IDs and quotes plus fallible semantic review]
    Review[Presenter approves, edits or rejects tasks]
    Export[Fixed session exports: Markdown and CSV]
    TTS[Fixed PowerShell script: Windows System.Speech]
    Temp[Temporary per-session text and WAV files]
    API --> Gate
    Gate --> ASR --> Whisper --> Session
    Gate --> Session
    Session --> Retrieve --> LLM --> Validate
    Validate --> Review --> Export
    Gate --> TTS --> Temp
  end
  Human --> Dashboard
  Human --> Meet
  Meet -->|tab audio selected by presenter| Picker
  Worklet -->|token-protected local WebSocket| API
  Dashboard <-->|local HTTP| API
  Temp -->|local WAV response, deleted after playback or Stop| Playback
  Playback -->|present dashboard tab with audio| Meet
  TTS -.->|pause ingestion until playback ends or Stop| ASR
```

Captured video is retained only to keep Chrome's sharing session alive; no frames are processed, rendered, recorded, saved or sent. Captured audio never connects to browser speakers. Eight-second ASR windows overlap by one second; backlog and dropped intervals are visible. Stop invalidates queued/in-flight results before a new capture can accept them.

The model has transcript read/search context only. It receives no shell, network, email, calendar, browser or arbitrary-file tool. Ask/Update are explicit button actions. The backend validates exact citations and separately asks the same local model to review semantic support. That review can be wrong, so presentation and export remain subject to human review.

Speech requires a selected visible answer and explicit approval. Windows writes a WAV; the dashboard plays it, allowing the meeting's tab-audio presentation to carry the reply. The server pauses ASR ingestion during output and records the omitted interval. Meeting audio travels through the online meeting service; model inference, transcript state and generated reports stay local.
