## LiveKit Voice Agent

Passive speech-to-text agent for LiveKit rooms that detects safety trigger phrases, posts events to the Clearance API, and optionally dispatches outbound SIP calls when "shots fired" is detected.

### What it does
- Transcribes audio in a LiveKit room using OpenAI Realtime.
- Watches transcripts and incoming text streams on `video.description`.
- Publishes trigger events to the Clearance `/api/events` endpoint.
- Initiates an outbound SIP call on "shots fired".

### Triggers
The agent looks for the following phrases in transcripts and text streams:
- weapon drawn / weapon out / gun drawn → `weapon_drawn`
- shots fired → `shots_fired`
- man down → `man_down`
- officer down → `officer_down`
- suspect down → `suspect_down`
- camera blocked / camera obscured → `camera_blocked`

### Prerequisites
- Python 3.11+
- LiveKit server URL + API key/secret
- OpenAI API key with Realtime access
- (Optional) LiveKit SIP trunk ID for outbound calls

### Setup
Install dependencies (using `uv` or your preferred tool):
```bash
uv sync
```

Create `.env.local` in this directory:
```bash
LIVEKIT_URL=wss://your-livekit-host
LIVEKIT_API_KEY=your_livekit_api_key
LIVEKIT_API_SECRET=your_livekit_api_secret
OPENAI_API_KEY=your_openai_api_key

# Optional: Clearance events API (defaults to https://clearance-phi.vercel.app)
CLEARANCE_API_BASE_URL=https://your-clearance-app

# Optional: SIP outbound call settings
LIVEKIT_SIP_TRUNK_ID=your_sip_trunk_id
LIVEKIT_SIP_ROOM_NAME=sip-alerts
```

### Run
```bash
uv run python agent.py dev
```

The agent registers as `clearance-agent-gemini` and will join rooms it is assigned to by your LiveKit Agent infrastructure.

### Notes
- Outbound calls dial the hardcoded number in `agent.py` (`OUTBOUND_PHONE_NUMBER`). Update it before production use.
- Events are posted to `${CLEARANCE_API_BASE_URL}/api/events` with the transcript and room name.
- Text stream handling expects `video.description` topics for camera-side text input.

