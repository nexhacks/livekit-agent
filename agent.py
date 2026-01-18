import asyncio
import json
import logging
import os
import time
import urllib.request
from typing import Any

from dotenv import load_dotenv
from openai.types.beta.realtime.session import TurnDetection

from livekit import api as lk_api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RunContext,
    cli,
    function_tool,
    room_io,
)
from livekit.plugins import openai
from livekit.protocol.sip import CreateSIPParticipantRequest

load_dotenv(".env.local")

logger = logging.getLogger("voice-transcriber")
logger.setLevel(logging.INFO)

OUTBOUND_PHONE_NUMBER = "+14083103927"
EVENTS_API_BASE_URL = os.getenv("CLEARANCE_API_BASE_URL", "https://clearance-phi.vercel.app")
EVENTS_API_PATH = "/api/events"

TRIGGER_PHRASES: dict[str, str] = {
    "weapon drawn": "weapon_drawn",
    "weapon out": "weapon_drawn",
    "gun drawn": "weapon_drawn",
    "shots fired": "shots_fired",
    "man down": "man_down",
    "officer down": "officer_down",
    "suspect down": "suspect_down",
    "camera blocked": "camera_blocked",
    "camera obscured": "camera_blocked",
}


def _extract_trigger_events(text: str) -> set[str]:
    lowered = text.lower()
    return {event for phrase, event in TRIGGER_PHRASES.items() if phrase in lowered}


def _post_event_sync(url: str, payload: dict[str, Any]) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8")
        return response.status, body


class TranscriberAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a passive speech-to-text transcriber. "
                'If you hear the phrase "drop the weapon", call the tool `foo` '
                "with the transcript and room name. "
                'If you hear the phrase "shots fired", call the tool '
                "`dispatch_outbound_call` with the transcript. "
                "Otherwise do not respond or speak."
            ),
        )
        self.room_name: str | None = None

    @function_tool()
    async def foo(
        self,
        context: RunContext,
        transcript: str,
        room_name: str,
    ) -> dict[str, Any]:
        logger.warning(
            "Trigger detected in room %s: %s",
            room_name,
            transcript,
        )
        return {"ok": True}

    @function_tool()
    async def dispatch_outbound_call(
        self,
        context: RunContext,
        transcript: str,
    ) -> dict[str, Any]:
        return await _dispatch_outbound_call(
            transcript=transcript,
            room_name=self.room_name,
        )


async def _dispatch_outbound_call(
    transcript: str,
    room_name: str | None,
) -> dict[str, Any]:
    trunk_id = os.getenv("LIVEKIT_SIP_TRUNK_ID")
    if not trunk_id:
        raise RuntimeError("Missing LIVEKIT_SIP_TRUNK_ID for outbound SIP calls.")
    target_room = room_name or os.getenv("LIVEKIT_SIP_ROOM_NAME", "sip-alerts")
    participant_identity = f"sip-alert-{int(time.time())}"

    request = CreateSIPParticipantRequest(
        sip_trunk_id=trunk_id,
        sip_call_to=OUTBOUND_PHONE_NUMBER,
        room_name=target_room,
        participant_identity=participant_identity,
        participant_name="Shots Fired Alert",
        krisp_enabled=True,
        wait_until_answered=False,
    )

    livekit_api = lk_api.LiveKitAPI()
    try:
        participant = await livekit_api.sip.create_sip_participant(request)
    finally:
        await livekit_api.aclose()

    logger.warning(
        "Outbound call dispatched to %s (room=%s, participant=%s) for: %s",
        OUTBOUND_PHONE_NUMBER,
        target_room,
        participant_identity,
        transcript,
    )
    return {"participant": str(participant)}

server = AgentServer()


@server.rtc_session(agent_name="clearance-agent-gemini")
async def entrypoint(ctx: JobContext):
    ctx.log_context_fields = {"room": ctx.room.name}

    agent = TranscriberAgent()
    agent.room_name = ctx.room.name

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model="gpt-realtime",
            voice="alloy",
            modalities=["text"],
            turn_detection=TurnDetection(
                type="server_vad",
                threshold=0.5,
                prefix_padding_ms=300,
                silence_duration_ms=500,
                create_response=True,
                interrupt_response=False,
            ),
        ),
    )

    active_text_tasks: set[asyncio.Task] = set()

    async def _handle_text_stream(reader, participant_identity: str) -> None:
        info = reader.info
        stream_id = getattr(info, "id", None)
        stream_id = stream_id or getattr(info, "stream_id", None)
        size = getattr(info, "size", None)
        logger.info(
            "Text stream received from %s (topic=%s, id=%s, size=%s)",
            participant_identity,
            info.topic,
            stream_id,
            size,
        )
        try:
            text = await reader.read_all()
        except Exception as exc:
            logger.warning("Text stream read failed: %s", exc)
            return
        if text:
            logger.info("Text stream content: %s", text)
            matched_events = _extract_trigger_events(text)
            if matched_events:
                for event in matched_events:
                    if event == "shots_fired":
                        continue
                    asyncio.create_task(
                        _send_event_to_chain(
                            event=event,
                            transcript=text,
                            room_name=ctx.room.name,
                        )
                    )
            if "shots_fired" in matched_events:
                logger.warning(
                    "Audio trigger detected in text stream (room=%s): %s",
                    ctx.room.name,
                    text,
                )
                await _send_event_to_chain(
                    event="shots_fired",
                    transcript=text,
                    room_name=ctx.room.name,
                )
                asyncio.create_task(
                    _dispatch_outbound_call(
                        transcript=text,
                        room_name=ctx.room.name,
                    )
                )

    def _register_text_stream(reader, participant_identity: str) -> None:
        task = asyncio.create_task(_handle_text_stream(reader, participant_identity))
        active_text_tasks.add(task)
        task.add_done_callback(lambda t: active_text_tasks.discard(t))

    ctx.room.register_text_stream_handler(
        "video.description",
        _register_text_stream,
    )

    @session.on("user_input_transcribed")
    def _on_transcript(transcript) -> None:
        text = (transcript.transcript or "").strip()
        if not text:
            return
        is_final = bool(getattr(transcript, "is_final", False))
        logger.info("Transcript%s: %s", " (final)" if is_final else "", text)
        if not is_final:
            return

        matched_events = _extract_trigger_events(text)
        for event in matched_events:
            asyncio.create_task(
                _send_event_to_chain(
                    event=event,
                    transcript=text,
                    room_name=ctx.room.name,
                )
            )

    disconnected = asyncio.Event()

    @ctx.room.on("disconnected")
    def _on_disconnected() -> None:
        disconnected.set()

    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=room_io.RoomOptions(
            audio_input=True,
            text_output=True,
            text_input=False,
            audio_output=False,
            video_input=False,
        ),
    )
    await ctx.connect()
    await disconnected.wait()


async def _send_event_to_chain(
    event: str,
    transcript: str,
    room_name: str,
) -> None:
    if not EVENTS_API_BASE_URL:
        logger.warning("Missing CLEARANCE_API_BASE_URL; skipping event publish.")
        return

    url = f"{EVENTS_API_BASE_URL.rstrip('/')}{EVENTS_API_PATH}"
    payload = {
        "event": event,
        "cameraDetails": room_name,
        "roomName": room_name,
        "transcript": transcript,
        "source": "livekit-voice-agent",
    }
    try:
        status, body = await asyncio.to_thread(_post_event_sync, url, payload)
        if status >= 300:
            logger.warning("Event publish failed (%s): %s", status, body)
        else:
            logger.info("Event published (%s): %s", status, body)
    except Exception as exc:
        logger.warning("Event publish error: %s", exc)


if __name__ == "__main__":
    cli.run_app(server)