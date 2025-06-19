from fastapi import FastAPI, UploadFile, File, HTTPException, Response
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
import os
import json
import re
import google.generativeai as genai
from google.generativeai import types
from io import BytesIO
from google.cloud import storage
from fastapi import Request
import uuid # To create unique filenames
from dotenv import load_dotenv # To create environment variables from .env file
from pydantic import BaseModel
from typing import List, Dict
from typing import Any
from fastapi import Body
import base64
import tempfile

# Load environment variables from .env file
# if os.environ.get("IS_LOCAL", "false").lower() == "true":
#     load_dotenv()
load_dotenv()

# os.environ["GOOGLE_API_USE_CLIENT_CERTIFICATE"] = "true"

app = FastAPI(
    title="Invoice Data Extractor Backend",
    description="API for extracting invoice data using Gemini and managing Excel exports to GCS."
)

origins = [
    "http://localhost:3000",  
    "http://localhost:8000",
    "https://safiya-ai-829750441427.asia-southeast2.run.app",  
    "https://invoice-extractor-68157002897.asia-southeast2.run.app",
]
 
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  # Explicit methods
    allow_headers=["*"],
    expose_headers=["*"],  # Important for some frontend frameworks
    max_age=600,  # Cache preflight requests for 10 minutes
)

# # --- Authentication configuration ---
# SERVICE_ACCOUNT_KEY_FILE = "sa_key_ads.json"

# # Checking if the service account key file exists
# if not os.path.exists(SERVICE_ACCOUNT_KEY_FILE):
#     raise FileNotFoundError(
#     f"Service account key file '{SERVICE_ACCOUNT_KEY_FILE}' not found. "
#     "Please ensure it's in the same directory as main.py."
#     )

# # Set GOOGLE_APPLICATION_CREDENTIALS
# os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = SERVICE_ACCOUNT_KEY_FILE

# Decode base64 SA_KEY_B64 and write to a temporary file
SA_KEY_B64 = os.getenv("SA_KEY_B64")
if not SA_KEY_B64:
    raise ValueError("SA_KEY_B64 environment variable not set.")

decoded_key = base64.b64decode(SA_KEY_B64)

# Write to a temp file
temp_key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
temp_key_file.write(decoded_key)
temp_key_file.close()

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = temp_key_file.name

# Client initialization for Google Gemini API
try:
    genai.configure() # This will use the GOOGLE_APPLICATION_CREDENTIALS environment variable
    _ = genai.GenerativeModel("gemini-2.5-flash-preview-05-20") # Test the connection by initializing a model
except Exception as e:
    raise RuntimeError(f"Failed to initialize Google Gemini API: {e}. Check your service account permissions.")

# Google Cloud Storage client initialization
try:
    storage_client = storage.Client()
except Exception as e:
    raise RuntimeError(f"Failed to initialize Google Cloud Storage client: {e}. Check your service account permissions.")

# The bucket name for Google Cloud Storage
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
if not GCS_BUCKET_NAME:
    raise ValueError("GCS_BUCKET_NAME environment variable not set. Please create a .env file or set it directly.")

# --- Cleaner function ---
def clean_json_output(output_text: str) -> str:
    """Removes markdown backticks and 'json' tag from the Gemini output."""
    cleaned = re.sub(r"```(?:json)?\n?", "", output_text).strip()
    cleaned = cleaned.replace("```", "").strip()
    return cleaned

def generate_gcs_filename(prefix: str = "invoice_data") -> str:
    """Generates a unique filename for GCS."""
    return f"{prefix}_{uuid.uuid4().hex}.xlsx"

# --- Extract function ---
def _extract_invoice_data_core(file_bytes: bytes, mime_type: str):
    """Core logic to extract invoice data using Gemini."""
    system_instruction = (
        """
        You are an invoice data extractor. You extract the transaction date (the format is dd/mm/yyyy where d is day, m is month, 
        and y is year), total amount (integer) in the form of rupiah, and the vendor name (string), 
        then save it into a list of dictionary format. 
        In some cases, one pdf file may contain more than one invoices, so you have to create a list of dictionaries 
        with extracted data of each invoice.
        """.strip()
    )

    model = genai.GenerativeModel("gemini-2.5-flash-preview-05-20")

    try:
        response = model.generate_content([
            system_instruction,
            {
                "mime_type": mime_type,
                "data": file_bytes
            },
            "extract it"
        ])
        output_text = response.text
        cleaned = clean_json_output(output_text)
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Failed to parse Gemini output as JSON: {e}. Raw output: {output_text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during Gemini content generation: {e}")

# --- Endpoint API ---

@app.post("/extract-invoice")
async def extract_invoice(files: List[UploadFile] = File(...)):
    """
    Extracts invoice data from multiple uploaded files (PNG, JPG, JPEG, or PDF),
    and returns the extracted data for each file.
    """
    allowed_mimes = ["image/png", "image/jpeg", "application/pdf"]

    if not files or len(files) == 0:
        raise HTTPException(status_code=400, detail="Please upload at least one file.")

    results = []

    for file in files:
        # Validasi tipe file
        if file.content_type not in allowed_mimes:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: {file.content_type}. Allowed types: {', '.join(allowed_mimes)}"
            )

        try:
            file_bytes = await file.read()
            mime_type = file.content_type

            # Proses ekstraksi
            extracted_data_list = _extract_invoice_data_core(file_bytes, mime_type)

            if extracted_data_list:
                results.append({
                    "filename": file.filename,
                    "extracted_data": extracted_data_list
                })
            else:
                results.append({
                    "filename": file.filename,
                    "extracted_data": [],
                    "warning": "No data extracted from this invoice file."
                })

        except Exception as e:
            results.append({
                "filename": file.filename,
                "error": f"Error processing file: {str(e)}"
            })

    return {
        "message": "Invoice extraction completed.",
        "results": results
    }

class ExtractedInvoiceRequest(BaseModel):
    extracted_data: List[Dict[str, str]]  # Or change `str` to appropriate types if known

@app.post("/json-to-excel")
async def json_to_excel_and_save(data: dict = Body(...)):
    try:
        extracted_data = data.get("extracted_data")

        if not extracted_data or not isinstance(extracted_data, list):
            raise HTTPException(status_code=400, detail="Missing or invalid 'extracted_data' in request body.")

        # Convert to DataFrame
        df = pd.DataFrame(extracted_data)
        output_excel = BytesIO()
        with pd.ExcelWriter(output_excel, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
        output_excel.seek(0)

        # Save to GCS
        gcs_file_name = generate_gcs_filename("invoice_data")
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        blob = bucket.blob(gcs_file_name)
        blob.upload_from_file(output_excel, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        gcs_file_path = f"gs://{GCS_BUCKET_NAME}/{gcs_file_name}"

        # Prepare file for download
        output_excel.seek(0)
        return Response(
            content=output_excel.read(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f"attachment; filename={gcs_file_name}",
                "X-GCS-Path": gcs_file_path
            }
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error converting JSON to Excel or saving to GCS: {e}")

# response.map("extracted_data", lambda x: x if isinstance(x, list) else [x])