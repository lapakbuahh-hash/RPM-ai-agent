from datetime import datetime, timedelta, timezone
import json
import os
import time
from google import genai
from google.genai.errors import ServerError
from google.oauth2.service_account import Credentials
import gspread
import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
GSHEET_ID = os.environ["GSHEET_ID"].strip()
GCP_SERVICE_ACCOUNT_JSON = os.environ["GCP_SERVICE_ACCOUNT_JSON"].strip()

client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_INSTRUCTION = """
Kamu adalah asisten AI operasional untuk PT Renjani Pijar Mustika (jasa maklon skincare khusus haji & umroh dengan MOQ mulai 100 pcs, serta produk ritel trial Kiswah Veil yang berisi facial wash 60ml, sunscreen spray 60ml, moisturizer 10gr, dan lip balm 10gr).
Tugasmu menganalisis pesan prospek dari kanal Website, IG_DM, IG_Comment, atau WhatsApp dan mengembalikan HANYA JSON valid dengan format:
{
  "jalur": "Maklon_Travel | Trial_Kiswah_Veil | Perlu_Klarifikasi",
  "nama": "string atau '-'",
  "kontak": "string atau '-'",
  "instansi_atau_brand": "string atau '-'",
  "minat_produk": "string atau '-'",
  "estimasi_qty": "string atau '-'",
  "detail_paket_trial": "string atau '-'",
  "prioritas": "Tinggi | Sedang | Rendah",
  "ringkasan": "ringkasan singkat kebutuhan prospek",
  "gap_sop": "string atau '-'",
  "perlu_eskalasi_manusia": true atau false
}
Jangan mengarang nomor sertifikat, harga resmi, atau klaim hukum yang tidak ada di input.
"""


def get_wib_date() -> str:
  wib = timezone(timedelta(hours=7))
  return datetime.now(wib).strftime("%Y-%m-%d %H:%M")


def send_telegram(text: str) -> None:
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
  resp = requests.post(url, json=payload, timeout=20)
  resp.raise_for_status()


def classify_lead(channel: str, message: str) -> dict:
  prompt = f"Channel: {channel}\nPesan: {message}"
  models_to_try = ["gemini-3.8-flash", "gemini-2.5-flash"]

  last_error = None
  for model_name in models_to_try:
    for attempt in range(1, 4):
      try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={"system_instruction": SYSTEM_INSTRUCTION},
        )
        raw = response.text.strip()
        if raw.startswith("```"):
          raw = raw.strip("`").replace("json", "", 1).strip()
        return json.loads(raw)
      except ServerError as e:
        last_error = e
        if e.code == 503 and attempt < 3:
          time.sleep(2 * attempt)
          continue
        break

  raise last_error


def get_spreadsheet():
  creds_dict = json.loads(GCP_SERVICE_ACCOUNT_JSON)
  scopes = [
      "[https://www.googleapis.com/auth/spreadsheets](https://www.googleapis.com/auth/spreadsheets)",
      "[https://www.googleapis.com/auth/drive](https://www.googleapis.com/auth/drive)",
  ]
  creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
  gc = gspread.authorize(creds)
  return gc.open_by_key(GSHEET_ID)


def save_to_sheets(channel: str, result: dict) -> str:
  sh = get_spreadsheet()
  now_str = get_wib_date()
  today_only = now_str.split(" ")[0]
  jalur = result.get("jalur", "Perlu_Klarifikasi")

  target_tab = (
      "Order_Trial_Kiswah" if jalur == "Trial_Kiswah_Veil" else "Leads_Maklon"
  )
  ws_target = sh.worksheet(target_tab)

  if target_tab == "Leads_Maklon":
    # Kolom A-I: Tanggal | Channel | Nama | Kontak | Instansi_Travel_Brand | Minat_Produk | Estimasi_Qty | Prioritas | Status
    row = [
        now_str,
        channel,
        result.get("nama", "-"),
        result.get("kontak", "-"),
        result.get("instansi_atau_brand", "-"),
        result.get("minat_produk", jalur),
        result.get("estimasi_qty", "-"),
        result.get("prioritas", "Sedang"),
        "Baru",
    ]
  else:
    # Kolom A-G: Tanggal | Channel | Nama | Kontak | Detail_Paket_Trial | Catatan | Status
    row = [
        now_str,
        channel,
        result.get("nama", "-"),
        result.get("kontak", "-"),
        result.get("detail_paket_trial", "Kiswah Veil Trial Kit"),
        result.get("ringkasan", "-"),
        "Baru",
    ]

  ws_target.append_row(row, value_input_option="USER_ENTERED")

  # Update Daily_Log (Kolom A-F: Tanggal | Total_Masuk | Jumlah_Maklon | Jumlah_Trial | Highlight_Prioritas | Gap_SOP)
  ws_log = sh.worksheet("Daily_Log")
  log_records = ws_log.get_all_values()

  maklon_inc = 1 if target_tab == "Leads_Maklon" else 0
  trial_inc = 1 if target_tab == "Order_Trial_Kiswah" else 0
  highlight = (
      f"{result.get('nama', '-')} ({jalur} - {result.get('prioritas', 'Sedang')})"
  )
  gap_sop = result.get("gap_sop", "-")

  updated_existing = False
  for idx in range(1, len(log_records)):
    row_vals = log_records[idx]
    if row_vals and row_vals[0].startswith(today_only):
      total_prev = (
          int(row_vals[1]) if len(row_vals) > 1 and row_vals[1].isdigit() else 0
      )
      maklon_prev = (
          int(row_vals[2]) if len(row_vals) > 2 and row_vals[2].isdigit() else 0
      )
      trial_prev = (
          int(row_vals[3]) if len(row_vals) > 3 and row_vals[3].isdigit() else 0
      )
      prev_highlight = row_vals[4] if len(row_vals) > 4 and row_vals[4] else ""
      prev_gap = row_vals[5] if len(row_vals) > 5 and row_vals[5] else "-"

      new_highlight = (
          f"{prev_highlight}; {highlight}" if prev_highlight else highlight
      )
      new_gap = (
          gap_sop
          if prev_gap in ("", "-")
          else (f"{prev_gap}; {gap_sop}" if gap_sop != "-" else prev_gap)
      )

      ws_log.update(
          f"A{idx + 1}:F{idx + 1}",
          [[
              today_only,
              total_prev + 1,
              maklon_prev + maklon_inc,
              trial_prev + trial_inc,
              new_highlight,
              new_gap,
          ]],
      )
      updated_existing = True
      break

  if not updated_existing:
    ws_log.append_row(
        [today_only, 1, maklon_inc, trial_inc, highlight, gap_sop],
        value_input_option="USER_ENTERED",
    )

  return target_tab


if __name__ == "__main__":
  sample_channel = os.getenv("LEAD_CHANNEL", "WhatsApp").strip()
  sample_message = os.getenv(
      "LEAD_MESSAGE",
      (
          "Assalamualaikum, saya admin travel umroh di Semarang, butuh info"
          " maklon/co-branding paket skincare jamaah sekitar 300 pax untuk"
          " keberangkatan awal tahun depan."
      ),
  ).strip()

  result = classify_lead(sample_channel, sample_message)
  saved_tab = save_to_sheets(sample_channel, result)

  notif = (
      f"[Renjani AI - {sample_channel}]\n"
      f"Tersimpan di Tab: {saved_tab}\n"
      f"Jalur: {result['jalur']} | Prioritas: {result['prioritas']}\n"
      f"Nama: {result['nama']} ({result['instansi_atau_brand']})\n"
      f"Kontak: {result['kontak']} | Qty: {result['estimasi_qty']}\n"
      f"Ringkasan: {result['ringkasan']}\n"
      f"Eskalasi ke Anda: {'YA' if result['perlu_eskalasi_manusia'] else 'TIDAK'}"
  )
  send_telegram(notif)
  print(json.dumps(result, indent=2, ensure_ascii=False))
