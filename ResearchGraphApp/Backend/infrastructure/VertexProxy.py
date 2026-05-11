"""
VertexProxy.py — Local FastAPI proxy that bridges the Graphify CLI
(which speaks the Google AI Studio / OpenAI-compatible REST protocol)
to Google Cloud Vertex AI via the ``google-genai`` SDK and ADC.

Graphify's ``gemini`` backend sends OpenAI-compatible chat.completions
requests to ``https://generativelanguage.googleapis.com/v1beta/openai/``.
This proxy intercepts those requests on localhost:8000, translates them
to Vertex AI calls using ADC credentials, and returns a conforming
OpenAI-compatible response.

Lifecycle: started by ``test_backend.sh`` before ``main.py`` runs,
           killed on script exit via ``trap``.
"""

import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from google import genai
from google.genai import types

app = FastAPI(title="ResearchBot Vertex AI Proxy")

# Initialise the Vertex AI client once at module level.
_client = genai.Client(
    vertexai=True,
    project=os.getenv("GOOGLE_CLOUD_PROJECT_ID"),
    location="global",
)


@app.post("/{path:path}")
async def proxy(path: str, request: Request):
    """
    Catch-all POST handler.  Graphify sends OpenAI-style
    ``/chat/completions`` requests here.  We forward the
    contents to Vertex AI and return an OpenAI-shaped response.
    """
    body = await request.json()

    # --- Extract model --------------------------------------------------
    model_name = body.get("model", "gemini-2.5-flash")

    # --- Build contents from OpenAI messages ----------------------------
    messages = body.get("messages", [])
    system_text = None
    contents = []

    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("content", "")

        if role == "system":
            system_text = text
            continue

        contents.append(
            types.Content(
                role="user" if role == "user" else "model",
                parts=[types.Part.from_text(text=text)],
            )
        )

    # --- Build generation config ----------------------------------------
    gen_config = types.GenerateContentConfig(
        temperature=body.get("temperature", 0),
        max_output_tokens=65536,
    )
    if system_text:
        gen_config.system_instruction = system_text

    # Check for JSON mode request
    if body.get("response_format", {}).get("type") == "json_object":
        gen_config.response_mime_type = "application/json"

    # --- Call Vertex AI -------------------------------------------------
    try:
        response = _client.models.generate_content(
            model=model_name,
            contents=contents,
            config=gen_config,
        )
        text_out = response.text or ""
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(exc), "type": "proxy_error"}},
        )

    # --- Return OpenAI-compatible response ------------------------------
    return JSONResponse(content={
        "id": f"proxy-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_out,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })
