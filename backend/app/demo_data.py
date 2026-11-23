"""Local-only fake customer and support-case records."""

MOCK_CUSTOMERS = {
    "alex@example.com": {
        "id": "cus_1001",
        "name": "Alex Morgan",
        "email": "alex@example.com",
        "tier": "Plus",
        "contract_expiry_date": "2027-06-30",
        "password_hash": "pbkdf2_sha256$600000$y6gmRXW-via9w6zZYPnzeA$nC5nrooMwqSOZHGmwkDQtIe_2Gq8V6RrYfns6HPaHd4",
    },
    "sam@example.com": {
        "id": "cus_1002",
        "name": "Sam Lee",
        "email": "sam@example.com",
        "tier": "Standard",
        "contract_expiry_date": "2025-09-30",
        "password_hash": "pbkdf2_sha256$600000$lK_j-1Lz3ltWq3iEhzlMig$hpJw-fY0ahjUGLxldcmxhahy4V40Zwu1fyC6ox-5rM0",
    },
    "taylor@example.com": {
        "id": "cus_1003",
        "name": "Taylor Reed",
        "email": "taylor@example.com",
        "tier": "Standard",
        "contract_expiry_date": "2027-12-31",
        "password_hash": "pbkdf2_sha256$600000$PwE8yMgzYyprCrt188OiBA$SGsAaUTKvjP979ilnniyoILaJ_HHDrNUPvqwlTdgbL0",
    },
}

MOCK_CASES = [
    {"id": "case_203", "customer_id": "cus_1001", "subject": "Delayed shipment", "status": "open", "updated_at": "2026-11-19"},
    {"id": "case_188", "customer_id": "cus_1002", "subject": "Update billing address", "status": "resolved", "updated_at": "2026-11-15"},
]

MOCK_ORDERS = [
    {
        "order_id": "ord_3001",
        "ordered_at": "2026-11-04T10:30:00+00:00",
        "customer_id": "cus_1001",
        "items": [
            {"item": "ABC Pro Laptop", "price": 1499.00, "quantity": 1},
            {"item": "USB-C Dock", "price": 129.00, "quantity": 1},
        ],
        "total_amount": 1628.00,
    },
    {
        "order_id": "ord_3002",
        "ordered_at": "2026-11-12T14:15:00+00:00",
        "customer_id": "cus_1001",
        "items": [
            {"item": "Mirrorless Camera", "price": 1299.00, "quantity": 1},
            {"item": "Camera Bag", "price": 89.00, "quantity": 2},
        ],
        "total_amount": 1477.00,
    },
    {
        "order_id": "ord_4001",
        "ordered_at": "2026-10-28T09:45:00+00:00",
        "customer_id": "cus_1002",
        "items": [
            {"item": "OLED Television 65-inch", "price": 1199.00, "quantity": 1},
            {"item": "HDMI Cable", "price": 24.00, "quantity": 2},
        ],
        "total_amount": 1247.00,
    },
    {
        "order_id": "ord_4002",
        "ordered_at": "2026-11-09T16:20:00+00:00",
        "customer_id": "cus_1002",
        "items": [
            {"item": "Home Espresso Machine", "price": 1399.00, "quantity": 1},
            {"item": "Coffee Grinder", "price": 249.00, "quantity": 1},
        ],
        "total_amount": 1648.00,
    },
    {
        "order_id": "ord_5001",
        "ordered_at": "2026-11-17T11:00:00+00:00",
        "customer_id": "cus_1003",
        "items": [
            {"item": "Cobalt Ultra Tablet", "price": 1299.00, "quantity": 1},
            {"item": "Protective Case", "price": 79.00, "quantity": 1},
        ],
        "total_amount": 1378.00,
    },
]
