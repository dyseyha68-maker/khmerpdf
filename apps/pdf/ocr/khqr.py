"""
National Bank of Cambodia (NBC) - Bakong KHQR Dynamic Generator & Payment Verifier.
Follows EMVCo QR Code Specification and Bakong OpenAPI guidelines.
"""
import io
import base64
import hashlib
import logging
import requests
import qrcode

logger = logging.getLogger(__name__)


def _format_tlv(tag: str, value: str) -> str:
    """Format Tag-Length-Value according to EMVCo standard."""
    val_bytes = str(value).encode('utf-8')
    return f"{tag}{len(val_bytes):02d}{value}"


def _crc16_ccitt(data: str) -> str:
    """EMVCo Standard CRC16-CCITT (False, poly=0x1021, init=0xFFFF)."""
    crc = 0xFFFF
    for byte in data.encode('utf-8'):
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def generate_bakong_khqr(
    bakong_account_id: str,
    merchant_name: str = "KhmerPDF",
    merchant_city: str = "Phnom Penh",
    amount: int = 2000,
    currency: str = "KHR",
    bill_no: str = "",
    store_label: str = "KhmerPDF"
):
    """
    Generates dynamic Bakong KHQR string, its MD5 hash, and base64 PNG data URL.
    Returns: (khqr_string, md5_hash, qr_data_url)
    """
    bakong_id = (bakong_account_id or 'khmerpdf@aba').strip()
    name = (merchant_name or 'KhmerPDF').strip()[:25]
    city = (merchant_city or 'Phnom Penh').strip()[:15]
    curr_code = "116" if currency.upper() == "KHR" else "840"
    amt_str = str(amount) if currency.upper() == "KHR" else f"{float(amount):.2f}"
    bill_str = str(bill_no)[:25] if bill_no else "VIP1"

    # Tag 29: Merchant Account Information (Individual Bakong Account)
    sub29 = _format_tlv('00', bakong_id)

    # Tag 62: Additional Data Field (Bill Number, Store Label, Terminal)
    sub62 = (
        _format_tlv('01', bill_str) +
        _format_tlv('03', store_label[:20]) +
        _format_tlv('07', 'WEB')
    )

    payload = (
        _format_tlv('00', '01') +      # Payload Format Indicator
        _format_tlv('01', '12') +      # Point of Initiation Method: 12 (Dynamic QR)
        _format_tlv('29', sub29) +     # Merchant Account Info
        _format_tlv('52', '5999') +    # Merchant Category Code
        _format_tlv('53', curr_code) + # Currency (116=KHR, 840=USD)
        _format_tlv('54', amt_str) +   # Amount
        _format_tlv('58', 'KH') +      # Country Code
        _format_tlv('59', name) +      # Merchant Name
        _format_tlv('60', city) +      # Merchant City
        _format_tlv('62', sub62) +     # Additional Data
        '6304'                         # CRC Tag + Length
    )

    crc = _crc16_ccitt(payload)
    khqr_string = payload + crc
    md5_hash = hashlib.md5(khqr_string.encode('utf-8')).hexdigest()

    # Generate QR Image
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(khqr_string)
    qr.make(fit=True)

    img = qr.make_image(fill_color="#18181b", back_color="#ffffff")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    qr_data_url = f"data:image/png;base64,{qr_b64}"

    return khqr_string, md5_hash, qr_data_url


def verify_bakong_transaction(md5_hash: str, api_token: str):
    """
    Checks if a KHQR transaction was completed using NBC Bakong OpenAPI.
    Endpoint: POST https://api-bakong.nbc.gov.kh/v1/check_transaction_by_md5
    Returns: (is_paid: bool, raw_response: dict)
    """
    if not md5_hash:
        return False, {"error": "Missing md5 hash"}

    token = (api_token or "").strip()
    if not token:
        # If no token configured yet, log warning and return pending
        logger.warning("Bakong API Token not configured in OCRSettings.")
        return False, {"status": "no_token", "message": "Bakong API token not configured."}

    url = "https://api-bakong.nbc.gov.kh/v1/check_transaction_by_md5"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"md5": md5_hash}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=6)
        data = response.json() if response.status_code == 200 else {}
        
        # NBC Bakong responseCode == 0 means SUCCESS / PAID
        response_code = data.get("responseCode")
        if response_code == 0:
            logger.info(f"Bakong payment verified for MD5 {md5_hash}")
            return True, data
        else:
            return False, data
    except Exception as e:
        logger.error(f"Bakong transaction check failed: {e}")
        return False, {"error": str(e)}
