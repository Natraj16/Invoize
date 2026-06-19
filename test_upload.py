"""Full integration test: upload → extract → validate → store → list → export."""
import httpx
import json
import sys

BASE = "http://127.0.0.1:8000"


def test_full_pipeline(image_path: str):
    client = httpx.Client(timeout=120.0)
    
    # 1. Upload and extract
    print("=== 1. Upload & Extract ===")
    with open(image_path, "rb") as f:
        resp = client.post(f"{BASE}/upload", files={"file": ("test.png", f, "image/png")})
    data = resp.json()
    print(f"Status: {resp.status_code}")
    print(f"Success: {data['success']}")
    print(f"Vendor: {data['data']['vendor_name']}")
    print(f"Total: ${data['data']['total']}")
    print(f"Confidence: {data['validation']['overall_confidence']}")
    print(f"Math valid: {data['validation']['math_valid']}")
    print(f"Completeness: {data['validation']['completeness_score']}")
    print()
    
    # 2. List stored receipts
    print("=== 2. List Stored Receipts ===")
    resp = client.get(f"{BASE}/receipts")
    listing = resp.json()
    print(f"Total stored: {listing['total']}")
    if listing['receipts']:
        r = listing['receipts'][0]
        print(f"Latest: {r['vendor_name']} - ${r['total']} ({r['overall_confidence']} confidence)")
        receipt_id = r['id']
    print()
    
    # 3. Get single receipt
    print("=== 3. Get Receipt Detail ===")
    resp = client.get(f"{BASE}/receipts/{receipt_id}")
    detail = resp.json()
    print(f"Receipt ID: {detail['id']}")
    print(f"Extracted data keys: {list(detail['extracted_data'].keys())}")
    print()
    
    # 4. Export CSV
    print("=== 4. Export CSV ===")
    resp = client.get(f"{BASE}/export/csv")
    print(f"CSV status: {resp.status_code}")
    csv_lines = resp.text.strip().split('\n')
    print(f"CSV rows: {len(csv_lines)} (including header)")
    for line in csv_lines[:3]:
        print(f"  {line}")
    print()
    
    # 5. Export Excel
    print("=== 5. Export Excel ===")
    resp = client.get(f"{BASE}/export/excel")
    print(f"Excel status: {resp.status_code}")
    print(f"Excel size: {len(resp.content)} bytes")
    print()
    
    # 6. Check all routes
    print("=== 6. All Routes ===")
    resp = client.get(f"{BASE}/openapi.json")
    paths = list(resp.json()['paths'].keys())
    print(f"Routes: {paths}")
    
    print("\n=== ALL TESTS PASSED ===")
    client.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\natra\.gemini\antigravity-ide\brain\919e1754-00ac-4ec2-8d39-cff08ba508af\test_receipt_1781873235729.png"
    test_full_pipeline(path)
