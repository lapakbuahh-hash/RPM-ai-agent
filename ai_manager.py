from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
from google import genai
from google.genai.errors import APIError
import gspread
import requests

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
GEMINI_API_KEY_2 = os.environ.get("GEMINI_API_KEY_2", "").strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()
GSHEET_ID = os.environ["GSHEET_ID"].strip()
GCP_SERVICE_ACCOUNT_JSON = os.environ["GCP_SERVICE_ACCOUNT_JSON"].strip()

API_KEYS = [k for k in [GEMINI_API_KEY, GEMINI_API_KEY_2] if k]

MODEL_CANDIDATES = [
    "gemini-3.5-flash",
]

ALLOWED_JALUR = {"Maklon_Travel", "Trial_Kiswah_Veil", "Perlu_Klarifikasi"}
ALLOWED_PRIORITAS = {"Tinggi", "Sedang", "Rendah"}

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


def send_telegram(text: str, raise_on_error: bool = True) -> None:
  url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
  payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000]}
  try:
    resp = requests.post(url, json=payload, timeout=20)
    print("Telegram status:", resp.status_code, resp.text)
    if raise_on_error:
      resp.raise_for_status()
  except Exception as e:
    print("Telegram send error:", str(e))
    if raise_on_error:
      raise


def split_messages(raw_message: str) -> list[str]:
  # Mendukung pemisah '---' pada baris tersendiri atau blok kosong ganda '\n\n'
  if re.search(r"(?m)^\s*---\s*$", raw_message):
    parts = re.split(r"(?m)^\s*---\s*$", raw_message)
  else:
    parts = re.split(r"\n\s*\n", raw_message)
  return [p.strip() for p in parts if p and p.strip()]


def _extract_json_object(raw_text: str) -> dict:
  cleaned = raw_text.strip()
  cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
  cleaned = re.sub(r"\s*```$", "", cleaned)
  try:
    parsed = json.loads(cleaned)
  except json.JSONDecodeError:
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
      raise
    parsed = json.loads(match.group(0))

  if not isinstance(parsed, dict):
    raise ValueError("Model output is not a JSON object")
  return parsed


def _clean_str(val, default: str = "-") -> str:
  if val is None:
    return default
  s = str(val).strip()
  return s if s else default


def _clean_bool(val, default: bool = False) -> bool:
  if isinstance(val, bool):
    return val
  if isinstance(val, str):
    low = val.strip().lower()
    if low in ("true", "ya", "yes", "1"):
      return True
    if low in ("false", "tidak", "no", "0"):
      return False
  return default


def normalize_classification(raw_dict: dict, message: str) -> dict:
  jalur = _clean_str(raw_dict.get("jalur"), "Perlu_Klarifikasi")
  if jalur not in ALLOWED_JALUR:
    jalur = "Perlu_Klarifikasi"

  prioritas = _clean_str(raw_dict.get("prioritas"), "Sedang")
  if prioritas not in ALLOWED_PRIORITAS:
    prioritas = "Sedang"

  ringkasan = _clean_str(raw_dict.get("ringkasan"), "")
  if not ringkasan or ringkasan == "-":
    ringkasan = message[:180].strip() or "-"

  return {
      "jalur": jalur,
      "nama": _clean_str(raw_dict.get("nama"), "-"),
      "kontak": _clean_str(raw_dict.get("kontak"), "-"),
      "instansi_atau_brand": _clean_str(
          raw_dict.get("instansi_atau_brand"), "-"
      ),
      "minat_produk": _clean_str(raw_dict.get("minat_produk"), jalur),
      "estimasi_qty": _clean_str(raw_dict.get("estimasi_qty"), "-"),
      "detail_paket_trial": _clean_str(
          raw_dict.get("detail_paket_trial"), "-"
      ),
      "prioritas": prioritas,
      "ringkasan": ringkasan,
      "gap_sop": _clean_str(raw_dict.get("gap_sop"), "-"),
      "perlu_eskalasi_manusia": _clean_bool(
          raw_dict.get("perlu_eskalasi_manusia"), default=False
      ),
  }


def _rule_based_fallback(message: str, reason: str) -> dict:
  low = message.lower()
  maklon_keywords = [
      "maklon",
      "co-branding",
      "cobranding",
      "travel",
      "umroh",
      "umrah",
      "haji",
      "jamaah",
      "pax",
      "moq",
      "pabrik",
      "produksi",
  ]
  trial_keywords = [
      "kiswah",
      "trial",
      "sunscreen",
      "lip balm",
      "moisturizer",
      "facial wash",
      "sample",
      "ecer",
      "ritel",
      "retail",
  ]

  if any(k in low for k in maklon_keywords):
    jalur = "Maklon_Travel"
    prioritas = "Tinggi"
  elif any(k in low for k in trial_keywords):
    jalur = "Trial_Kiswah_Veil"
    prioritas = "Sedang"
  else:
    jalur = "Perlu_Klarifikasi"
    prioritas = "Sedang"

  phone_match = re.search(r"(\+?62|0)8\d{7,12}", message)
  kontak = phone_match.group(0) if phone_match else "-"

  qty_match = re.search(r"\b(\d+\s*(?:pcs|pax|box|paket|unit|botol))\b", low)
  estimasi_qty = qty_match.group(1) if qty_match else "-"

  return {
      "jalur": jalur,
      "nama": "-",
      "kontak": kontak,
      "instansi_atau_brand": "-",
      "minat_produk": jalur,
      "estimasi_qty": estimasi_qty,
      "detail_paket_trial": (
          "Kiswah Veil Trial Kit" if jalur == "Trial_Kiswah_Veil" else "-"
      ),
      "prioritas": prioritas,
      "ringkasan": message[:200].strip() or "-",
      "gap_sop": f"[Fallback-Rule] {reason[:120]}",
      "perlu_eskalasi_manusia": True,
  }


def classify_lead(channel: str, message: str) -> dict:
  prompt = f"Channel: {channel}\nPesan: {message}"
  errors = []

  for key_idx, api_key in enumerate(API_KEYS, start=1):
    client = genai.Client(api_key=api_key)
    for model_name in MODEL_CANDIDATES:
      try:
        print(f"Trying key #{key_idx} with model: {model_name}")
        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config={"system_instruction": SYSTEM_INSTRUCTION},
        )
        raw_text = response.text or ""
        parsed = _extract_json_object(raw_text)
        print(f"Succeeded with key #{key_idx} and model: {model_name}")
        return normalize_classification(parsed, message)
      except APIError as e:
        msg = f"key#{key_idx}/{model_name} [{e.code}]: {e.message}"
        print("Model error:", msg)
        errors.append(msg)
        if e.code in (429, 500, 503):
          continue
        raise
      except (json.JSONDecodeError, ValueError) as e:
        msg = f"key#{key_idx}/{model_name} [JSON]: {str(e)}"
        print("Model JSON error:", msg)
        errors.append(msg)
        continue

  summary_err = " | ".join(errors) if errors else "Unknown AI error"
  print("Using rule-based fallback due to:", summary_err)
  return _rule_based_fallback(message, summary_err)


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
  sample_channel = os.getenv("LEAD_CHANNEL", "WhatsApp").strip() or "WhatsApp"
  raw_message = os.getenv(
      "LEAD_MESSAGE",
      (
          "Assalamualaikum, saya admin travel umroh di Semarang, butuh info"
          " maklon/co-branding paket skincare jamaah sekitar 300 pax untuk"
          " keberangkatan awal tahun depan."
      ),
  ).strip()

  messages = split_messages(raw_message)
  if not messages:
    raise ValueError("LEAD_MESSAGE is empty")

  print(f"Total messages to process: {len(messages)}")

  try:
    for idx, msg in enumerate(messages, start=1):
      print(f"--- Processing message #{idx}/{len(messages)} ---")
      result = classify_lead(sample_channel, msg)
      print("AI Result:", json.dumps(result, ensure_ascii=False))
      saved_tab = save_to_sheets(sample_channel, result)

      notif = (
          f"[Renjani AI - {sample_channel}] ({idx}/{len(messages)})\n"
          f"Tersimpan di Tab: {saved_tab}\n"
          f"Jalur: {result['jalur']} | Prioritas: {result['prioritas']}\n"
          f"Nama: {result['nama']} ({result['instansi_atau_brand']})\n"
          f"Kontak: {result['kontak']} | Qty: {result['estimasi_qty']}\n"
          f"Ringkasan: {result['ringkasan']}\n"
          f"Eskalasi ke Anda: {'YA' if result['perlu_eskalasi_manusia'] else 'TIDAK'}"
      )
      send_telegram(notif, raise_on_error=True)

      if idx < len(messages):
        time.sleep(1)
  except Exception as e:
    err_notif = (
        f"[Renjani AI - ERROR]\n"
        f"Channel: {sample_channel}\n"
        f"Error: {str(e)[:500]}"
    )
    send_telegram(err_notif, raise_on_error=False)
    raise
