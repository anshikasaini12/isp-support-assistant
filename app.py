"""
Flask webhook for Dialogflow CX.
Receives CX webhook requests, delegates to services/, returns CX-shaped
webhook responses. No business logic lives in this file.
"""
import logging
from flask import Flask, request, jsonify

from services.outage_service import check_outage, InvalidZipError
from services.ticket_service import (
    get_ticket_status,
    InvalidTicketIdError,
    TicketNotFoundError,
)

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("isp-webhook")


def cx_text_response(text: str) -> dict:
    """Wrap a plain string into the shape Dialogflow CX expects back."""
    return {
        "fulfillment_response": {
            "messages": [{"text": {"text": [text]}}]
        }
    }


def get_tag(body: dict) -> str:
    """CX sends a 'fulfillmentInfo.tag' telling us which webhook this is for."""
    return (body.get("fulfillmentInfo") or {}).get("tag", "")


def get_param(body: dict, name: str):
    """Read a session/page parameter value out of a CX webhook request."""
    params = (body.get("sessionInfo") or {}).get("parameters") or {}
    return params.get(name)


@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}
    tag = get_tag(body)
    logger.info("Received webhook call, tag=%s", tag)

    try:
        if tag == "check-outage":
            return handle_outage_check(body)
        elif tag == "check-ticket":
            return handle_ticket_status(body)
        else:
            logger.warning("Unknown tag received: %s", tag)
            return jsonify(cx_text_response(
                "I couldn't process that request right now. Let's try something else."
            )), 200

    except Exception:
        # Catch-all: never let a raw 500 / stack trace go back to Dialogflow.
        logger.exception("Unhandled error processing webhook, tag=%s", tag)
        return jsonify(cx_text_response(
            "Something's not responding on our end. We can try again in a moment, "
            "or I can help you with something else."
        )), 200


def handle_outage_check(body: dict):
    zip_code = get_param(body, "zip_code")
    try:
        result = check_outage(str(zip_code) if zip_code is not None else "")
    except InvalidZipError:
        logger.info("Invalid zip provided: %r", zip_code)
        return jsonify(cx_text_response(
            "That doesn't look like a valid ZIP or postal code. Could you share it again?"
        )), 200

    if result["outage"]:
        text = (
            f"Yes, there's a known outage in your area ({result['area']}). "
            f"Estimated resolution time is {result['estimatedResolution']}."
        )
    else:
        text = f"Good news — no reported outage in your area ({result['area']})."

    return jsonify({
        "fulfillment_response": {"messages": [{"text": {"text": [text]}}]},
        "sessionInfo": {"parameters": {"outage_result": result}},
    }), 200


def handle_ticket_status(body: dict):
    ticket_id = get_param(body, "ticket_id")
    try:
        result = get_ticket_status(str(ticket_id) if ticket_id is not None else "")
    except InvalidTicketIdError:
        logger.info("Invalid ticket id provided: %r", ticket_id)
        return jsonify({
            "fulfillment_response": {"messages": [{"text": {"text": [
                "That ticket ID doesn't look right. It should look like INC-10291 — "
                "could you try again?"
            ]}}]},
            "sessionInfo": {"parameters": {"lookup_success": False, "ticket_id": None}},
        }), 200
    except TicketNotFoundError:
        logger.info("Ticket not found: %r", ticket_id)
        return jsonify({
            "fulfillment_response": {"messages": [{"text": {"text": [
                f"I couldn't find a ticket matching '{ticket_id}'. Could you double check the ID?"
            ]}}]},
            "sessionInfo": {"parameters": {"lookup_success": False, "ticket_id": None}},
        }), 200

    text = (
        f"Ticket {result['ticketId']} is currently {result['status'].replace('_', ' ').title()}."
    )
    if result["estimatedResolution"]:
        text += f" Estimated resolution: {result['estimatedResolution']}."

    return jsonify({
        "fulfillment_response": {"messages": [{"text": {"text": [text]}}]},
        "sessionInfo": {"parameters": {"ticket_result": result, "lookup_success": True}},
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
