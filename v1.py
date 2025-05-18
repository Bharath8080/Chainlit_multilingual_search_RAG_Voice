import os
import io
import wave
import numpy as np
import audioop
from dotenv import load_dotenv

import chainlit as cl
from openai import AsyncOpenAI

load_dotenv()

# === API Client Setup ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# === Silence Detection ===
SILENCE_THRESHOLD = 1500
SILENCE_TIMEOUT = 1800.0

# === Tools ===
@cl.step(type="tool")
async def speech_to_text(audio_file):
    response = await openai_client.audio.transcriptions.create(
        model="gpt-4o-mini-transcribe",
        file=audio_file,
        language="en"
    )
    return response.text

@cl.step(type="tool")
async def text_to_speech(text: str):
    response = await openai_client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="nova",
        input=text,
    )
    buffer = io.BytesIO()
    buffer.name = "response.wav"
    buffer.write(response.content)
    buffer.seek(0)
    return buffer.name, buffer.read()

@cl.step(type="tool")
async def generate_text_answer(transcription):
    history = cl.user_session.get("message_history", [])

    # If history is empty, add a system prompt to enforce English response
    if not history:
        history.append({
            "role": "system",
            "content": "You are a helpful assistant that always responds only in English."
        })

    history.append({"role": "user", "content": transcription})

    response = await openai_client.chat.completions.create(
        model="gpt-4o",
        messages=history,
        temperature=0.7
    )

    message = response.choices[0].message
    history.append(message)
    cl.user_session.set("message_history", history)

    return message.content

# === Session Events ===
@cl.on_chat_start
async def start():
    cl.user_session.set("message_history", [])
    await cl.Message(content="🎤 Welcome! Press `P` to talk.").send()

@cl.on_audio_start
async def on_audio_start():
    cl.user_session.set("silent_duration_ms", 0)
    cl.user_session.set("is_speaking", False)
    cl.user_session.set("audio_chunks", [])
    return True

@cl.on_audio_chunk
async def on_audio_chunk(chunk: cl.InputAudioChunk):
    audio_chunks = cl.user_session.get("audio_chunks")
    if audio_chunks is not None:
        audio_chunks.append(np.frombuffer(chunk.data, dtype=np.int16))

    if chunk.isStart:
        cl.user_session.set("last_elapsed_time", chunk.elapsedTime)
        cl.user_session.set("is_speaking", True)
        return

    time_diff = chunk.elapsedTime - cl.user_session.get("last_elapsed_time")
    cl.user_session.set("last_elapsed_time", chunk.elapsedTime)

    energy = audioop.rms(chunk.data, 2)
    if energy < SILENCE_THRESHOLD:
        silent_duration = cl.user_session.get("silent_duration_ms") + time_diff
        cl.user_session.set("silent_duration_ms", silent_duration)
        if silent_duration >= SILENCE_TIMEOUT and cl.user_session.get("is_speaking"):
            cl.user_session.set("is_speaking", False)
            await process_audio()
    else:
        cl.user_session.set("silent_duration_ms", 0)
        cl.user_session.set("is_speaking", True)

async def process_audio():
    audio_chunks = cl.user_session.get("audio_chunks", [])
    if not audio_chunks:
        return

    combined = np.concatenate(audio_chunks)
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(combined.tobytes())

    wav_buffer.seek(0)
    cl.user_session.set("audio_chunks", [])

    audio_file = ("audio.wav", wav_buffer, "audio/wav")
    transcription = await speech_to_text(audio_file)

    await cl.Message(
        author="You",
        type="user_message",
        content=transcription,
        elements=[cl.Audio(content=wav_buffer.getvalue(), mime="audio/wav")]
    ).send()

    answer = await generate_text_answer(transcription)
    _, output_audio = await text_to_speech(answer)

    await cl.Message(
        content=answer,
        elements=[cl.Audio(content=output_audio, mime="audio/wav", auto_play=True)]
    ).send()

@cl.on_message
async def on_message(_):
    await cl.Message(content="🎤 Press `P` and talk to interact with voice.").send()
