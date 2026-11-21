from datetime import datetime, timedelta, timezone
import json
import os
from google import genai
from google.genai.errors import APIError
import gspread
import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
GSHEET_ID = os.environ["GSHEET_ID"].strip()
GCP_SERVICE_ACCOUNT_JSON = os.environ["GCP_SERVICE_ACCOUNT_JSON"].strip()

client = genai.Client(api_key=GEMINI_API_KEY)

MODEL_CANDIDATES = [
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
]

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
  print("Telegram status:", resp.status_code, resp.text)
  resp.raise_for_status()


def classify_lead(channel: str, message: str) -> dict:
  prompt = f"Channel: {channel}\nPesan: {message}"
  last_error = None

  for model_name in MODEL_CANDIDATES:
    try:
      print("Trying model:", model_name)
      response = client.models.generate_content(
          model=model_name,
          contents=prompt,
          config={"system_instruction": SYSTEM_INSTRUCTION},
      )
      raw = response.text.strip()
      if raw.startswith("```"):
        raw = raw.strip("`").replace("json", "", 1).strip()
      return json.loads(raw)
    except APIError as e:
      last_error = e
      print(f"Model {model_name} failed with {e.code}: {e.message}")
      if e.code in (429, 500, 503, 404):
        continue
      raise

  raise last_error


def get_spreadsheet():
  creds_dict = json.loads(GCP_SERVICE_ACCOUNT_JSON)
  gc = gspread.service_account_from_dict(creds_dict)
  return gc.open_by_key(GSHEET_ID)


def _find_next_empty_row_col_a(ws, start_row: int = 2) -> int:
  col_a_values = ws.col_values(1)
  for row_idx in range(start_row, len(col_a_values) + 1):
    val = (
        col_a_values[row_idx - 1].strip()
        if row_idx - 1 < len(col_a_values)
        else ""
    )
    if not val:
      return row_idx
  return max(start_row, len(col_a_values) + 1)


def save_to_sheets(channel: str, result: dict) -> str:
  sh = get_spreadsheet()
  print("Spreadsheet title:", sh.title)
  print("Available worksheets:", [ws.title for ws in sh.worksheets()])

  now_str = get_wib_date()
  today_only = now_str.split(" ")[0]
  jalur = result.get("jalur", "Perlu_Klarifikasi")

  target_tab = (
      "Order_Trial_Kiswah" if jalur == "Trial_Kiswah_Veil" else "Leads_Maklon"
  )
  ws_target = sh.worksheet(target_tab)

  if target_tab == "Leads_Maklon":
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
    end_col = "I"
  else:
    row = [
        now_str,
        channel,
        result.get("nama", "-"),
        result.get("kontak", "-"),
        result.get("detail_paket_trial", "Kiswah Veil Trial Kit"),
        result.get("ringkasan", "-"),
        "Baru",
    ]
    end_col = "G"

  next_row = _find_next_empty_row_col_a(ws_target, start_row=2)
  ws_target.update(
      f"A{next_row}:{end_col}{next_row}",
      [row],
      value_input_option="USER_ENTERED",
  )
  print(f"Wrote to {target_tab} row {next_row}:", row)

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
    if row_vals and row_vals[0].strip().startswith(today_only):
      total_prev = (
          int(row_vals[1])
          if len(row_vals) > 1 and row_vals[1].strip().isdigit()
          else 0
      )
      maklon_prev = (
          int(row_vals[2])
          if len(row_vals) > 2 and row_vals[2].strip().isdigit()
          else 0
      )
      trial_prev = (
          int(row_vals[3])
          if len(row_vals) > 3 and row_vals[3].strip().isdigit()
          else 0
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
          value_input_option="USER_ENTERED",
      )
      updated_existing = True
      break

  if not updated_existing:
    next_log_row = _find_next_empty_row_col_a(ws_log, start_row=2)
    ws_log.update(
        f"A{next_log_row}:F{next_log_row}",
        [[today_only, 1, maklon_inc, trial_inc, highlight, gap_sop]],
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
  print("AI Result:", json.dumps(result, ensure_ascii=False))
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
