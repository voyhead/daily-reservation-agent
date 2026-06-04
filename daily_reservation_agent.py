import os.path
import base64
import json
import requests
from datetime import date, datetime
from bs4 import BeautifulSoup
from dotenv import dotenv_values
from openai import OpenAI

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

PROCESSED_EMAILS_FILE = "processed_emails.json"

MAX_EMAILS_PER_RUN = 10
EMAIL_BODY_LIMIT_FOR_AI = 2000
EMAIL_BODY_PREVIEW_LIMIT = 1000


env = dotenv_values(".env")

TELEGRAM_BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = env.get("OPENAI_API_KEY")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in .env")

if not OPENAI_API_KEY:
    raise ValueError("Missing OPENAI_API_KEY in .env")

client = OpenAI(api_key=OPENAI_API_KEY)


def get_gmail_service():
    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("credentials.json"):
                raise FileNotFoundError(
                    "Missing credentials.json. Put it in this project folder."
                )

            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json",
                SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_header(headers, name):
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value")
    return None


def decode_base64url(data):
    if not data:
        return ""

    missing_padding = len(data) % 4
    if missing_padding:
        data += "=" * (4 - missing_padding)

    return base64.urlsafe_b64decode(data.encode("utf-8"))


def extract_text_from_payload(payload):
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")

    if data:
        raw_bytes = decode_base64url(data)

        if mime_type == "text/html":
            html = raw_bytes.decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")
            return soup.get_text("\n", strip=True)

        return raw_bytes.decode("utf-8", errors="ignore")

    parts = payload.get("parts", [])

    plain_texts = []
    html_texts = []
    other_texts = []

    for part in parts:
        part_mime = part.get("mimeType", "")
        text = extract_text_from_payload(part)

        if not text:
            continue

        if part_mime == "text/plain":
            plain_texts.append(text)
        elif part_mime == "text/html":
            html_texts.append(text)
        else:
            other_texts.append(text)

    # Prefer text/plain to avoid duplicated plain + html versions.
    if plain_texts:
        return "\n".join(plain_texts)

    if html_texts:
        return "\n".join(html_texts)

    return "\n".join(other_texts)

def clean_email_thread(text: str) -> str:
    if not text:
        return ""

    cut_markers = [
        "\nOn ",
        "\r\nOn ",
        "\n> ",
        "\r\n> ",
        "\nFrom:",
        "\r\nFrom:",
        "\nSent:",
        "\r\nSent:",
        "\n-----Original Message-----",
        "\r\n-----Original Message-----",
        "\n________________________________",
        "\r\n________________________________",
    ]

    cleaned_text = text

    for marker in cut_markers:
        index = cleaned_text.find(marker)
        if index != -1:
            cleaned_text = cleaned_text[:index]

    return cleaned_text.strip()


def load_processed_email_ids() -> set:
    if not os.path.exists(PROCESSED_EMAILS_FILE):
        return set()

    with open(PROCESSED_EMAILS_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    return set(data.get("processed_email_ids", []))


def save_processed_email_ids(processed_email_ids: set) -> None:
    data = {
        "processed_email_ids": sorted(list(processed_email_ids))
    }

    with open(PROCESSED_EMAILS_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def get_latest_chat_id():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    response = requests.get(url, timeout=10)
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError("getUpdates failed")

    results = data.get("result", [])

    if not results:
        raise RuntimeError("No updates found. Send a message to the bot first.")

    latest_update = results[-1]
    message = latest_update.get("message")

    if not message:
        raise RuntimeError("Latest update has no message")

    return message["chat"]["id"]


def send_telegram_message(chat_id, text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    response = requests.post(url, json=payload, timeout=10)

    print("Telegram status:", response.status_code)

    if response.status_code != 200:
        print(response.text)
        raise RuntimeError("Telegram message failed")

    print("Telegram message sent successfully.")


def find_unread_booking_emails(service, max_results: int = MAX_EMAILS_PER_RUN) -> list:
    query = 'is:unread newer_than:7d (booking OR reservation OR reserve OR "book a table" OR "table for")'

    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=max_results
    ).execute()

    messages = result.get("messages", [])

    if not messages:
        return []

    email_list = []

    for msg in messages:
        message_id = msg["id"]

        message = service.users().messages().get(
            userId="me",
            id=message_id,
            format="full"
        ).execute()

        headers = message.get("payload", {}).get("headers", [])

        sender = get_header(headers, "From")
        subject = get_header(headers, "Subject")
        email_date = get_header(headers, "Date")
        snippet = message.get("snippet", "")
        body_text = clean_email_thread(
    extract_text_from_payload(message.get("payload", {}))
)

        email_list.append(
            {
                "id": message_id,
                "from": sender,
                "subject": subject,
                "date": email_date,
                "snippet": snippet,
                "body": body_text,
            }
        )

    return email_list


def analyze_booking_email_with_ai(email_body: str, subject: str) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": [
                    "new_booking",
                    "change_booking",
                    "cancel_booking",
                    "general_question",
                    "unknown"
                ]
            },
            "name": {"type": ["string", "null"]},
            "date": {"type": ["string", "null"]},
            "time": {"type": ["string", "null"]},
            "party_size": {"type": ["integer", "null"]},
            "phone": {"type": ["string", "null"]},
            "email": {"type": ["string", "null"]},
            "special_requests": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"]},
            "missing_fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["name", "date", "time", "party_size", "phone"]
                }
            },
            "confidence": {"type": "number"},
            "reply_draft": {"type": "string"}
        },
        "required": [
            "intent",
            "name",
            "date",
            "time",
            "party_size",
            "phone",
            "email",
            "special_requests",
            "notes",
            "missing_fields",
            "confidence",
            "reply_draft"
        ],
        "additionalProperties": False
    }

    limited_body = email_body[:EMAIL_BODY_LIMIT_FOR_AI]

    response = client.responses.create(
        model="gpt-4o-mini",
        input=[
            {
                "role": "system",
                "content": (
    "You are an AI reservation briefing assistant for restaurant staff. "
    "You analyze customer emails and produce structured booking information plus a reply draft for human staff review. "
    "The final decision and sending are done by a human staff member. "
    "This reply draft is not sent automatically. "
    "Do not invent missing details. "
    "If a detail is not present, use null. "
    f"Today's date is {date.today().isoformat()}. "
    "Do not convert vague or ambiguous dates into a specific calendar date unless the year is clear from the email or can be safely inferred from today's date. "
    "If the customer writes a date like 'Saturday 12th June', preserve that phrase in the date field instead of guessing the year. "
    "If the weekday and date appear inconsistent, mention this in notes. "
    "Required booking fields for a new booking are name, date, time, party_size, and phone. "
    "For change_booking or cancel_booking, extract whatever identifying details are available. "
    "If required details are missing for a new booking, include them in missing_fields. "

    "Write a concise restaurant reply draft for human staff to review before sending. "
    "For new_booking, if booking date, booking time, and party size are available, use this exact reply format:\n\n"
    "Dear [customer name],\n\n"
    "Your booking has been confirmed for [party number] people on [booking date, booking day] at [booking time].\n"
    "[If the customer's phone number is missing, add this line: Please leave your contact number.]\n\n"
    "Kind regards,\n"
    "Arisu Restaurant\n\n"

    "If the customer name is missing, use 'Dear customer,'. "
    "If the customer's phone number is already provided, do not include 'Please leave your contact number.' "
    "If booking date, booking time, or party size is missing, do not use the confirmation sentence. Instead, ask only for the missing details. "
    "For change_booking or cancel_booking, do not use the confirmation format. Acknowledge the request and say the team will check the booking details. "
    "Never leave placeholders such as [customer name], [party number], [booking date], [booking day], or [booking time] in the final reply draft. "
    "Do not mention that you are an AI. "
    "Use standard capitalization in the reply draft, for example 'Saturday' instead of 'saturday'. "
    "If there is an obvious ordinal typo in the date such as 12st, 12nd, or 12rd, mention it in notes but use the corrected form such as 12th in the reply draft. "
),
            },
            {
                "role": "user",
                "content": f"""
Subject:
{subject}

Customer email:
{limited_body}
""".strip(),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "booking_email_analysis",
                "schema": schema,
                "strict": True,
            }
        },
    )

    raw_json = response.output_text
    print("AI analysis JSON:")
    print(raw_json)

    return json.loads(raw_json)


def build_status(analysis: dict) -> str:
    missing_fields = analysis.get("missing_fields", [])

    if missing_fields:
        return "Missing " + ", ".join(missing_fields)

    return "Ready to review"


def format_summary_message(total_found: int, new_count: int, skipped_count: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""
Daily reservation briefing

Run time: {now}
Unread booking-related emails found: {total_found}
New emails processed: {new_count}
Already processed emails skipped: {skipped_count}
""".strip()


def format_email_alert(index: int, email_data: dict, analysis: dict) -> str:
    body_preview = email_data["body"][:350]
    status = build_status(analysis)

    return f"""
Booking #{index} — {status}

Name: {analysis.get("name") or "Unknown"}
Date: {analysis.get("date") or "Unknown"}
Time: {analysis.get("time") or "Missing"}
Party: {analysis.get("party_size") or "Unknown"}
Phone: {analysis.get("phone") or "Missing"}

Notes: {analysis.get("notes") or "None"}

Reply draft:
{analysis.get("reply_draft")}

Original:
{body_preview}
""".strip()


def main():
    print("Starting daily reservation agent...")

    service = get_gmail_service()
    email_list = find_unread_booking_emails(service, max_results=MAX_EMAILS_PER_RUN)

    processed_email_ids = load_processed_email_ids()
    chat_id = get_latest_chat_id()

    new_email_data_list = []
    skipped_count = 0

    for email_data in email_list:
        message_id = email_data["id"]

        if message_id in processed_email_ids:
            skipped_count += 1
            print("Skipping already processed email:", email_data.get("subject"))
            continue

        new_email_data_list.append(email_data)

    summary_message = format_summary_message(
        total_found=len(email_list),
        new_count=len(new_email_data_list),
        skipped_count=skipped_count
    )

    send_telegram_message(chat_id, summary_message)

    if not new_email_data_list:
        print("No new unread booking-related emails to process.")
        return

    for index, email_data in enumerate(new_email_data_list, start=1):
        message_id = email_data["id"]

        print("=" * 60)
        print("Processing new unread booking-related email:")
        print("From:", email_data["from"])
        print("Subject:", email_data["subject"])
        print("Date:", email_data["date"])
        print("Message ID:", message_id)

        analysis = analyze_booking_email_with_ai(
            email_body=email_data["body"],
            subject=email_data["subject"] or ""
        )

        alert_message = format_email_alert(
            index=index,
            email_data=email_data,
            analysis=analysis
        )

        send_telegram_message(chat_id, alert_message)

        processed_email_ids.add(message_id)
        save_processed_email_ids(processed_email_ids)

        print("Marked email as processed.")

    print("Daily reservation agent completed.")


if __name__ == "__main__":
    main()
