import streamlit as st
import pandas as pd
import os
import json
import re
import google.generativeai as genai
from google.generativeai import types
from io import BytesIO

service_account_info = st.secrets["gcp"]

with open("temp_service_account.json", "w") as f:
    json.dump(dict(service_account_info), f)

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = "temp_service_account.json"

def clean_json_output(output_text):
    cleaned = re.sub(r"```(?:json)?\n?", "", output_text).strip()
    cleaned = cleaned.replace("```", "").strip()
    return cleaned

@st.cache_data(show_spinner=True)
def extract_invoice_data(file_bytes, mime_type):
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

def main():
    st.title("Invoice Data Extractor using Gemini")

    uploaded_files = st.file_uploader(
        "Upload invoice files (PNG, JPG, JPEG, or PDF)",
        type=["png", "jpg", "jpeg", "pdf"],
        accept_multiple_files=True
    )

    data = []

    if uploaded_files:
        for uploaded_file in uploaded_files:
            with st.spinner(f"Processing {uploaded_file.name}..."):
                file_bytes = uploaded_file.read()
                if uploaded_file.name.lower().endswith(".png"):
                    mime = "image/png"
                elif uploaded_file.name.lower().endswith(".jpg") or uploaded_file.name.lower().endswith(".jpeg"):
                    mime = "image/jpeg"
                elif uploaded_file.name.lower().endswith(".pdf"):
                    mime = "application/pdf"
                else:
                    st.warning(f"Unsupported file type: {uploaded_file.name}")
                    continue

                try:
                    result = extract_invoice_data(file_bytes, mime)
                    data = data + result if isinstance(result, list) else data + [result]
                except Exception as e:
                    st.error(f"Failed to process {uploaded_file.name}: {e}")

        if data:
            df = pd.DataFrame(data)
            st.subheader("Extracted Invoice Data")
            st.dataframe(df)

            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            output.seek(0)

            st.download_button(
                label="Download as Excel",
                data=output,
                file_name="invoice_data.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

if __name__ == "__main__":
    main()
