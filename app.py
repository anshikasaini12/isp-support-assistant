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
        elif tag == "check-interruption":
            return handle_router_status_check(body)
        elif tag == "analyze-troubleshooting":
            return handle_analyze_troubleshooting(body)
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


_INTERRUPT_KEYWORDS = ("outage", "power cut", "down in my area", "area affected")


def handle_router_status_check(body: dict):
    router_status = get_param(body, "router_status") or ""
    text_lower = str(router_status).lower()

    if any(keyword in text_lower for keyword in _INTERRUPT_KEYWORDS):
        logger.info("Interruption detected in router_status: %r", router_status)
        return jsonify({
            "fulfillment_response": {"messages": []},
            "sessionInfo": {"parameters": {
                "is_interruption": True,
                "router_status": None,
            }},
        }), 200

    return jsonify({
        "fulfillment_response": {"messages": []},
        "sessionInfo": {"parameters": {"is_interruption": False}},
    }), 200


# Keywords/phrases that indicate a troubleshooting step was already performed
_DEVICE_MULTI_HINTS = ("laptop", "mobile", "phone", "tablet", "all devices", "every device", "all my devices")
_DEVICE_SINGLE_HINTS = ("one device", "single device", "just my laptop", "only my")
_ROUTER_RESTART_HINTS = ("restarted the modem", "restarted my router", "restarted the router",
                         "reset the modem", "reset the router", "power cycled", "rebooted the router",
                         "rebooted the modem", "restarted my modem")
_CONNECTION_TEST_HINTS = ("lan", "wi-fi", "wifi", "ethernet")
_STILL_FAILING_HINTS = ("still not working", "still not resolved", "issue is still not resolved",
                        "not resolved", "still broken", "still down", "still an issue",
                        "problem persists", "issue persists", "still having the issue")


def handle_analyze_troubleshooting(body: dict):
    """
    Parses the raw user utterance that triggered the connectivity.issue intent
    to detect troubleshooting steps already described, so the conversation can
    skip questions the user has already answered up front, and escalate
    directly to a human agent if basic troubleshooting has already failed.
    """
    raw_text = (body.get("text") or "").lower()
    logger.info("Analyzing initial troubleshooting utterance: %r", raw_text)

    params = {}

    device_count = sum(1 for hint in _DEVICE_MULTI_HINTS if hint in raw_text)
    if device_count >= 2 or "laptop/mobile" in raw_text or "laptop / mobile" in raw_text:
        params["device_scope"] = "multiple devices (mentioned upfront)"
    elif any(hint in raw_text for hint in _DEVICE_SINGLE_HINTS):
        params["device_scope"] = "one device (mentioned upfront)"

    restarted_router = any(hint in raw_text for hint in _ROUTER_RESTART_HINTS)
    tested_connections = sum(1 for hint in _CONNECTION_TEST_HINTS if hint in raw_text) >= 2

    if restarted_router or tested_connections:
        details = []
        if restarted_router:
            details.append("restarted the modem/router")
        if tested_connections:
            details.append("tested both LAN and Wi-Fi")
        params["router_status"] = f"already checked upfront ({', '.join(details)})"

    still_failing = any(hint in raw_text for hint in _STILL_FAILING_HINTS)

    # Only escalate immediately if the user has already described real
    # troubleshooting steps AND says the issue persists — not just because
    # they said "still not working" with no prior steps described.
    already_tried_something = restarted_router or tested_connections
    escalate_immediately = already_tried_something and still_failing

    params["escalate_immediately"] = escalate_immediately

    if escalate_immediately:
        text = (
            "It sounds like you've already restarted your equipment and tested "
            "both LAN and Wi-Fi connections, and the issue is still not resolved. "
            "I'll escalate this directly to our support team — they'll reach out "
            "to you shortly."
        )
    elif params.get("device_scope") or params.get("router_status"):
        text = "Got it, thanks for the details — let me pick up from there."
    else:
        text = ""

    messages = [{"text": {"text": [text]}}] if text else []

    return jsonify({
        "fulfillment_response": {"messages": messages},
        "sessionInfo": {"parameters": params},
    }), 200


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
