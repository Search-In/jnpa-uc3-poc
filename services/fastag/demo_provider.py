from datetime import datetime, timezone


def demo_balance(rc_number: str) -> dict:
    return {
        "status": "success",
        "data": {
            "rc_number": rc_number,
            "tag_id": "DEMOFASTAG001",
            "provider_name": "DEMO_BANK",
            "provider_code": "DEMO001",
            "customer_name": "DEMO_USER",
            "available_recharge_limit": "5000.00",
            "available_balance": "850.00",
            "tag_status": "ACTIVE",
            "vehicle_class": "4",
            "vehicle_class_desc": "Car / Jeep / Van",
            "model_name": "DEMO_VEHICLE",
        },
    }


def demo_transactions(rc_number: str) -> dict:
    return {
        "status": "success",
        "data": [
            {
                "seq_no": "DEMO-001",
                "rc_number": rc_number,
                "transaction_date_time": datetime.now(timezone.utc).isoformat(),
                "toll_plaza_name": "JNPA Demo Toll Plaza",
                "toll_plaza_geocode": "18.95,72.95",
                "vehicle_type": "CAR",
                "lane_direction": "ENTRY",
                "bank_name": "DEMO_BANK",
                "status": "SUCCESS",
            }
        ],
    }
