"""
Flask webhook for Dialogflow CX.
Receives CX webhook requests, delegates to services/, returns CX-shaped
webhook responses. No business logic lives in this file.
"""
import logging
import re
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


def cx_text_response(text):
    return {
        "fulfillment_response": {
            "messages": [{"text": {"text": [text]}}]
        }
    }


def get_tag(body):
    return (body.get("fulfillmentInfo") or {}).get("tag", "")


def get_param(body, name):
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
        elif tag == "extract-zip":
            return handle_extract_zip(body)
        else:
            logger.warning("Unknown tag received: %s", tag)
            return jsonify(cx_text_response(
                "I couldn't process that request right now. Let's try something else."
            )), 200
    except Exception:
        logger.exception("Unhandled error processing webhook, tag=%s", tag)
        return jsonify(cx_text_response(
            "Something's not responding on our end. We can try again in a moment, "
            "or I can help you with something else."
        )), 200


_INTERRUPT_KEYWORDS = ("outage", "power cut", "down in my area", "area affected")


def handle_router_status_check(body):
    router_status = get_param(body, "router_status") or ""
    text_lower = str(router_status).lower()

    if any(k in text_lower for k in _INTERRUPT_KEYWORDS):
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


_ZIP_IN_TEXT_PATTERN = re.compile(r"\b\d{4,6}\b")


def handle_extract_zip(body):
    raw_text = body.get("text") or ""
    match = _ZIP_IN_TEXT_PATTERN.search(raw_text)
    zip_found = match.group(0) if match else None
    logger.info("Extracted zip from text %r: %r", raw_text, zip_found)
    return jsonify({
        "fulfillment_response": {"messages": []},
        "sessionInfo": {"parameters": {"zip_code": zip_found}},
    }), 200


_DEVICE_MULTI_HINTS = ("laptop", "mobile", "phone", "tablet", "all devices", "every device", "all my devices")
_DEVICE_SINGLE_HINTS = ("one device", "single device", "just my laptop", "only my")
_ROUTER_RESTART_HINTS = ("restarted the modem", "restarted my router", "restarted the router",
                         "reset the modem", "reset the router", "power cycled", "rebooted the router",
                         "rebooted the modem", "restarted my modem")
_CONNECTION_TEST_HINTS = ("lan", "wi-fi", "wifi", "ethernet")
_STILL_FAILING_HINTS = ("still not working", "still not resolved", "issue is still not resolved",
                        "not resolved", "still broken", "still down", "still an issue",
                        "problem persists", "issue persists", "still having the issue")


def handle_analyze_troubleshooting(body):
    raw_text = (body.get("text") or "").lower()
    logger.info("Analyzing initial troubleshooting utterance: %r", raw_text)

    params = {}

    device_count = sum(1 for h in _DEVICE_MULTI_HINTS if h in raw_text)
    if device_count >= 2 or "laptop/mobile" in raw_text or "laptop / mobile" in raw_text:
        params["device_scope"] = "multiple devices (mentioned upfront)"
    elif any(h in raw_text for h in _DEVICE_SINGLE_HINTS):
        params["device_scope"] = "one device (mentioned upfront)"

    restarted_router = any(h in raw_text for h in _ROUTER_RESTART_HINTS)
    tested_connections = sum(1 for h in _CONNECTION_TEST_HINTS if h in raw_text) >= 2

    details = []
    if restarted_router:
        details.append("restarted your modem/router")
    if tested_connections:
        details.append("tested both LAN and Wi-Fi")

    if restarted_router or tested_connections:
        params["router_status"] = "already checked upfront (" + ", ".join(details) + ")"

    still_failing = any(h in raw_text for h in _STILL_FAILING_HINTS)
    already_tried_something = restarted_router or tested_connections
    escalate_immediately = already_tried_something and still_failing
    params["escalate_immediately"] = escalate_immediately

    if escalate_immediately:
        text = (
            "It sounds like you've already " + " and ".join(details) + ", "
            "and the issue is still not resolved. I'll escalate this directly "
            "to our support team — they'll reach out to you shortly."
        )
    elif params.get("device_scope") or params.get("router_status"):
        text = "Got it, thanks for the details."
    else:
        text = ""

    messages = [{"text": {"text": [text]}}] if text else []

    return jsonify({
        "fulfillment_response": {"messages": messages},
        "sessionInfo": {"parameters": params},
    }), 200


def handle_outage_check(body):
    zip_code = get_param(body, "zip_code")
    try:
        result = check_outage(str(zip_code) if zip_code is not None else "")
    except InvalidZipError:
        logger.info("Invalid zip provided: %r", zip_code)
        return jsonify(cx_text_response(
            "That doesn't look like a valid ZIP or postal code. Could you share it again?"
        )), 200

    if result["outage"]:
        text = ("Yes, there's a known outage in your area (" + result["area"] + "). "
                "Estimated resolution time is " + result["estimatedResolution"] + ".")
    else:
        text = "Good news — no reported outage in your area (" + result["area"] + ")."

    return jsonify({
        "fulfillment_response": {"messages": [{"text": {"text": [text]}}]},
        "sessionInfo": {"parameters": {"outage_result": result}},
    }), 200


def handle_ticket_status(body):
    ticket_id = get_param(body, "ticket_id")
    try:
        result = get_ticket_status(str(ticket_id) if ticket_id is not None else "")
    except InvalidTicketIdError:
        logger.info("Invalid ticket id provided: %r", ticket_id)
        return jsonify({
            "fulfillment_response": {"messages": [{"text": {"text": [
                "That ticket ID doesn't look right. It should look like INC-10291 — could you try again?"
            ]}}]},
            "sessionInfo": {"parameters": {"lookup_success": False, "ticket_id": None}},
        }), 200
    except TicketNotFoundError:
        logger.info("Ticket not found: %r", ticket_id)
        return jsonify({
            "fulfillment_response": {"messages": [{"text": {"text": [
                "I couldn't find a ticket matching '" + str(ticket_id) + "'. Could you double check the ID?"
            ]}}]},
            "sessionInfo": {"parameters": {"lookup_success": False, "ticket_id": None}},
        }), 200

    text = "Ticket " + result["ticketId"] + " is currently " + result["status"].replace("_", " ").title() + "."
    if result["estimatedResolution"]:
        text += " Estimated resolution: " + result["estimatedResolution"] + "."

    return jsonify({
        "fulfillment_response": {"messages": [{"text": {"text": [text]}}]},
        "sessionInfo": {"parameters": {"ticket_result": result, "lookup_success": True}},
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
