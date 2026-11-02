"""Free barcode scanning: local decoding of one or more EAN/UPC codes in a photo (zxing-cpp),
plus an optional free product-name lookup at Open Food Facts.

What this does NOT do: recognise products without a readable barcode, or estimate how much is
left in a package. Quantities are always confirmed by a person.
"""
from __future__ import annotations

import io
import json
import urllib.request

PRODUCT_FORMATS = {"EAN-13", "EAN-8", "UPC-A", "UPC-E", "EAN13", "EAN8", "UPCA", "UPCE"}
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields=product_name,brands,quantity"
USER_AGENT = "Gezinsboodschappen/1.0 (private family app)"


def decode_barcodes(image_bytes):
    """Return the product barcodes found in an image, in reading order, without duplicates."""
    import zxingcpp
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    codes = []
    for result in zxingcpp.read_barcodes(image):
        fmt = str(result.format).replace("BarcodeFormat.", "")
        if fmt in PRODUCT_FORMATS and result.text and result.text not in codes:
            codes.append(result.text)
    return codes


def _http_json(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class LookupUnavailable(RuntimeError):
    """Open Food Facts could not be reached (not cached, so a later try can succeed)."""


def lookup_product(code, fetch=_http_json, timeout=5):
    """{'name', 'brand', 'quantity'} from Open Food Facts, None when the product is unknown.

    Raises LookupUnavailable when the service cannot be reached. The name is a suggestion only;
    it never changes stock by itself.
    """
    if not str(code).isdigit():
        return None
    try:
        data = fetch(OFF_URL.format(code=code), timeout)
    except Exception as exc:
        raise LookupUnavailable(str(exc)) from exc
    product = (data or {}).get("product") or {}
    if (data or {}).get("status") != 1 or not product.get("product_name"):
        return None
    return {"name": str(product.get("product_name")).strip(), "brand": str(product.get("brands") or "").strip(),
            "quantity": str(product.get("quantity") or "").strip()}
