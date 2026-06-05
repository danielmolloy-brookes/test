"""
QR Code generation service.
Each attendee gets a unique PNG QR code stored in static/qr_codes/.
"""
import io
import logging
import os
from pathlib import Path
from typing import Optional

import qrcode
from qrcode.image.styledpil import StyledPilImage
from PIL import Image, ImageDraw, ImageFont

from app.config import settings

logger = logging.getLogger(__name__)


def get_qr_path(ticket_id: str) -> Path:
    return Path(settings.QR_CODE_DIR) / f"{ticket_id}.png"


def get_qr_url(ticket_id: str) -> str:
    if settings.QR_CODE_DIR.startswith("static"):
        return f"{settings.BASE_URL}/static/qr_codes/{ticket_id}.png"
    return f"{settings.BASE_URL}/qr_codes/{ticket_id}.png"


def generate_qr_code(
    ticket_id: str,
    attendee_name: Optional[str] = None,
    event_name: Optional[str] = None,
) -> str:
    """
    Generate a QR code PNG for the given ticket_id.
    Returns the file path (relative to project root).
    """
    os.makedirs(settings.QR_CODE_DIR, exist_ok=True)
    output_path = get_qr_path(ticket_id)

    # QR data — full profile URL so phone cameras open the profile page directly
    qr_data = f"{settings.BASE_URL}/{ticket_id}"

    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=3,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)

    # Generate base QR image
    qr_img = qr.make_image(fill_color="#1e293b", back_color="white")
    qr_img = qr_img.convert("RGB")

    # Add branding label below QR code
    label_height = 80
    total_height = qr_img.size[1] + label_height
    final_img = Image.new("RGB", (qr_img.size[0], total_height), "white")
    final_img.paste(qr_img, (0, 0))

    draw = ImageDraw.Draw(final_img)

    # Try to load a nicer font, fall back to default
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except Exception:
        font_large = ImageFont.load_default()
        font_small = font_large

    img_width = final_img.size[0]

    # Event name (top of label area)
    if event_name:
        text = event_name[:40]
        bbox = draw.textbbox((0, 0), text, font=font_large)
        tw = bbox[2] - bbox[0]
        draw.text(((img_width - tw) / 2, qr_img.size[1] + 8), text, fill="#1e293b", font=font_large)

    # Attendee name (middle of label)
    if attendee_name:
        name_text = attendee_name[:40]
        bbox = draw.textbbox((0, 0), name_text, font=font_small)
        tw = bbox[2] - bbox[0]
        draw.text(((img_width - tw) / 2, qr_img.size[1] + 28), name_text, fill="#64748b", font=font_small)

    # Ticket ID (bottom — truncated)
    tid_text = f"ID: {ticket_id[:8].upper()}…"
    bbox = draw.textbbox((0, 0), tid_text, font=font_small)
    tw = bbox[2] - bbox[0]
    draw.text(((img_width - tw) / 2, qr_img.size[1] + 50), tid_text, fill="#94a3b8", font=font_small)

    final_img.save(str(output_path), "PNG", optimize=True)
    logger.info(f"QR code generated: {output_path}")
    return str(output_path)


def delete_qr_code(ticket_id: str) -> bool:
    """Delete a QR code file."""
    path = get_qr_path(ticket_id)
    try:
        if path.exists():
            path.unlink()
        return True
    except Exception as e:
        logger.error(f"Failed to delete QR code {ticket_id}: {e}")
        return False


def generate_qr_bytes(ticket_id: str) -> bytes:
    """Return QR code PNG as bytes (for inline use)."""
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=3,
    )
    qr.add_data(f"{settings.BASE_URL}/{ticket_id}")
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1e293b", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()
