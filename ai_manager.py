import json
import os
from google import genai
import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = """
Kamu adalah asisten AI operasional untuk PT Renjani Pijar Mustika (jasa maklon skincare khusus haji & umroh dengan MOQ mulai 100 pcs, serta produk ritel trial Kiswah Veil yang berisi facial wash 60ml, sunscreen spray 60ml, moisturizer 10gr, dan lip balm 10gr).
Tugasmu menganalisis pesan prospek dari kanal Website, IG_DM, IG_Comment, atau WhatsApp dan mengembalikan HANYA JSON valid dengan format:
{
  "jalur": "Maklon_Travel | Trial_Kiswah_Veil | Perlu_Klarifikasi",
  "nama": "string atau '-'",
  "kontak": "string atau '-'",
  "instansi_atau_brand": "string atau '-'",
  "estimasi_qty": "string atau '-'",
  "prioritas": "Tinggi | Sedang | Rendah",
  "ringkasan": "ringkasan singkat kebutuhan prospek",
  "perlu_eskalasi_manusia": true atau false
}
Jangan mengarang nomor sertifikat, harga resmi, atau klaim hukum yang tidak ada di input.
"""


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()


def classify_lead(channel: str, message: str) -> dict:
    prompt = f"Channel: {channel}\nPesan: {message}"
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config={"system_instruction": SYSTEM_INSTRUCTION},
    )
    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").replace("json", "", 1).strip()
    return json.loads(raw)


if __name__ == "__main__":
    sample_channel = os.getenv("LEAD_CHANNEL", "WhatsApp")
    sample_message = os.getenv(
        "LEAD_MESSAGE",
        "Assalamualaikum, saya admin travel umroh di Semarang, butuh info maklon/co-branding paket skincare jamaah sekitar 300 pax untuk keberangkatan awal tahun depan.",
    )
    result = classify_lead(sample_channel, sample_message)

    notif = (
        f"[Renjani AI - {sample_channel}]\n"
        f"Jalur: {result['jalur']} | Prioritas: {result['prioritas']}\n"
        f"Nama: {result['nama']} ({result['instansi_atau_brand']})\n"
        f"Kontak: {result['kontak']} | Qty: {result['estimasi_qty']}\n"
        f"Ringkasan: {result['ringkasan']}\n"
        f"Eskalasi ke Anda: {'YA' if result['perlu_eskalasi_manusia'] else 'TIDAK'}"
    )
    send_telegram(notif)
    print(json.dumps(result, indent=2, ensure_ascii=False))
